#!/usr/bin/env bash
# Verify the deployed 3Division airsoft portal/OpenTAKServer stack.
#
# This script is read-only. It does not rotate credentials, change users,
# restart services, or print secrets/tokens.
set -u
set -o pipefail

PORTAL_ROOT="${PORTAL_ROOT:-/opt/opentak-onboarding-portal}"
PORTAL_USER="${PORTAL_USER:-opentak-portal}"
PORTAL_URL="${PORTAL_URL:-https://atak.3division.eu/game/}"
ENV_FILE="${PORTAL_ENV_FILE:-${PORTAL_ROOT}/.env}"
OTS_PYTHON="${OTS_PYTHON:-/home/team3d/.opentakserver_venv_1713/bin/python}"
OTS_DATA_FOLDER="${OTS_DATA_FOLDER:-/home/team3d/ots-1713}"
OTS_RUN_USER="${OTS_RUN_USER:-team3d}"
OTS_HOME="${OTS_HOME:-/home/team3d}"

PASS_COUNT=0
WARN_COUNT=0
FAIL_COUNT=0

pass() {
  printf 'PASS  %s\n' "$*"
  PASS_COUNT=$((PASS_COUNT + 1))
}

warn() {
  printf 'WARN  %s\n' "$*"
  WARN_COUNT=$((WARN_COUNT + 1))
}

fail() {
  printf 'FAIL  %s\n' "$*"
  FAIL_COUNT=$((FAIL_COUNT + 1))
}

section() {
  printf '\n========== %s ==========\n' "$*"
}

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this verifier with sudo/root." >&2
  exit 2
fi

for cmd in grep stat curl systemctl sudo node; do
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    echo "Required command missing: ${cmd}" >&2
    exit 2
  fi
done

if [[ ! -d "${PORTAL_ROOT}" || ! -f "${ENV_FILE}" ]]; then
  echo "Portal deployment or .env not found." >&2
  exit 2
fi

env_value() {
  local key="$1"
  awk -F= -v wanted="${key}" '
    $1 == wanted {
      sub(/^[^=]*=/, "")
      print
      exit
    }
  ' "${ENV_FILE}"
}

check_env() {
  local key="$1"
  local expected="$2"
  local actual
  actual="$(env_value "${key}")"
  if [[ "${actual}" == "${expected}" ]]; then
    pass "${key}=${expected}"
  else
    fail "${key} has unexpected value"
  fi
}

check_marker() {
  local file="$1"
  local marker="$2"
  local label="$3"
  if grep -Fq "${marker}" "${file}"; then
    pass "${label}"
  else
    fail "${label} marker missing"
  fi
}

check_service() {
  local service="$1"
  if systemctl is-active --quiet "${service}"; then
    pass "service ${service} active"
  else
    fail "service ${service} is not active"
  fi
}

check_mode_600_if_present() {
  local file="$1"
  local label="$2"
  if [[ ! -e "${file}" ]]; then
    warn "${label} does not exist yet"
    return
  fi
  local mode
  mode="$(stat -c '%a' "${file}" 2>/dev/null || true)"
  if [[ "${mode}" == "600" ]]; then
    pass "${label} permissions 600"
  else
    warn "${label} permissions are ${mode:-unknown}, expected 600"
  fi
}

section "REPOSITORY"
if [[ -d "${PORTAL_ROOT}/.git" ]]; then
  head="$(sudo -u "${PORTAL_USER}" git -C "${PORTAL_ROOT}" rev-parse --short HEAD 2>/dev/null || true)"
  branch="$(sudo -u "${PORTAL_USER}" git -C "${PORTAL_ROOT}" branch --show-current 2>/dev/null || true)"
  if [[ -n "${head}" ]]; then
    pass "repository readable: ${branch:-detached} @ ${head}"
  else
    fail "repository HEAD could not be read"
  fi
else
  fail "repository .git directory missing"
fi

section "PRODUCTION CONFIG"
check_env "GAME_PERMANENT_USERS_RED" "CORood"
check_env "GAME_PERMANENT_USERS_BLUE" "COBLauw"
check_env "GAME_CO_PORTAL_USERNAME_RED" "CORood"
check_env "GAME_CO_PORTAL_USERNAME_BLUE" "COBlauw"
check_env "GAME_PUBLIC_TAK_HOST" "atak.3division.eu"
check_env "GAME_TAK_STREAM_PORT" "8089"
check_env "CORS_ORIGINS" "https://atak.3division.eu"
check_env "SESSION_COOKIE_SECURE" "True"
check_env "SESSION_COOKIE_SAMESITE" "Lax"

env_mode="$(stat -c '%a' "${ENV_FILE}" 2>/dev/null || true)"
if [[ "${env_mode}" == "600" ]]; then
  pass ".env permissions 600"
else
  warn ".env permissions are ${env_mode:-unknown}; verify only the portal service account can read it"
fi

section "PORTAL PYTHON"
PORTAL_PYTHON="${PORTAL_ROOT}/venv/bin/python"
if sudo -u "${PORTAL_USER}" env HOME="${PORTAL_ROOT}" "${PORTAL_PYTHON}" -m py_compile   "${PORTAL_ROOT}/app/__init__.py"   "${PORTAL_ROOT}/app/settings.py"   "${PORTAL_ROOT}/app/ots.py"   "${PORTAL_ROOT}/app/api_v1/__init__.py"   "${PORTAL_ROOT}/app/api_v1/game.py"   "${PORTAL_ROOT}/app/api_v1/announcements.py"; then
  pass "portal Python compile"
else
  fail "portal Python compile"
fi

GAME_PY="${PORTAL_ROOT}/app/api_v1/game.py"
APP_INIT="${PORTAL_ROOT}/app/__init__.py"
if grep -Fq 'id="airsoft_game_credential_cutoff"' "${GAME_PY}"   && grep -Fq 'hour=23' "${GAME_PY}"   && grep -Fq 'timezone="Europe/Amsterdam"' "${GAME_PY}"; then
  pass "23:00 Europe/Amsterdam game cutoff configured"
else
  fail "23:00 game cutoff configuration missing"
fi

if grep -Fq '/game/admin/co-devices' "${GAME_PY}"; then
  pass "admin CO device API present"
else
  fail "admin CO device API missing"
fi

if grep -Fq 'Content-Security-Policy' "${APP_INIT}"   && grep -Fq 'Strict-Transport-Security' "${APP_INIT}"; then
  pass "portal security-header code present"
else
  fail "portal security-header code missing"
fi

section "FRONTEND SOURCE"
FRONTEND_SRC="${PORTAL_ROOT}/frontend/src"
if grep -R -n -F 'dangerouslySetInnerHTML' "${FRONTEND_SRC}" >/tmp/airsoft-verify-raw-html.txt 2>&1; then
  fail "dangerouslySetInnerHTML still present"
  sed -n '1,10p' /tmp/airsoft-verify-raw-html.txt
else
  pass "no dangerouslySetInnerHTML"
fi

if grep -R -n -E 'localStorage\.(getItem|setItem)\([^)]*(access_token|refresh_token)' "${FRONTEND_SRC}" >/tmp/airsoft-verify-jwt-storage.txt 2>&1; then
  fail "JWT access/refresh token still read from or written to localStorage"
  sed -n '1,20p' /tmp/airsoft-verify-jwt-storage.txt
else
  pass "no JWT access/refresh token use in localStorage"
fi

if [[ -f "${PORTAL_ROOT}/frontend/src/components/SafeHtml.jsx" ]]; then
  pass "SafeHtml announcement renderer present"
else
  fail "SafeHtml announcement renderer missing"
fi

section "FRONTEND DEPENDENCIES"
cd "${PORTAL_ROOT}/frontend" || exit 2
check_node_version() {
  local package="$1"
  local expected="$2"
  local actual
  actual="$(sudo -u "${PORTAL_USER}" node -e "try{process.stdout.write(require('./node_modules/${package}/package.json').version)}catch(e){process.exit(2)}" 2>/dev/null || true)"
  if [[ "${actual}" == "${expected}" ]]; then
    pass "${package}@${expected}"
  else
    fail "${package} version ${actual:-missing}; expected ${expected}"
  fi
}

check_node_version "axios" "1.20.0"
check_node_version "follow-redirects" "1.16.0"
check_node_version "form-data" "4.0.6"
check_node_version "lodash-es" "4.18.1"
check_node_version "quill" "2.0.2"
check_node_version "react-router" "7.18.4"
check_node_version "react-router-dom" "7.18.4"

AUDIT_FILE="$(mktemp)"
if sudo -u "${PORTAL_USER}" npm audit --omit=dev --audit-level=low >"${AUDIT_FILE}" 2>&1; then
  pass "npm production audit reports no vulnerabilities"
else
  if grep -Eq '[1-9][0-9]* vulnerabilities|Severity: (low|moderate|high|critical)' "${AUDIT_FILE}"; then
    fail "npm production audit reports vulnerabilities"
    sed -n '1,80p' "${AUDIT_FILE}"
  else
    warn "npm production audit could not complete (network/registry unavailable)"
  fi
fi
rm -f "${AUDIT_FILE}"

section "OPENTAKSERVER PATCHES"
OTS_PACKAGE="$(sudo -u "${OTS_RUN_USER}" env HOME="${OTS_HOME}" OTS_DATA_FOLDER="${OTS_DATA_FOLDER}" "${OTS_PYTHON}" - <<'PY' 2>/dev/null
import pathlib
import opentakserver
print(pathlib.Path(opentakserver.__file__).resolve().parent)
PY
)"
if [[ -z "${OTS_PACKAGE}" || ! -d "${OTS_PACKAGE}" ]]; then
  fail "OpenTAKServer package path could not be resolved"
else
  pass "OpenTAKServer package found"

  TOKEN_PY="${OTS_PACKAGE}/models/Token.py"
  ENROLL_PY="${OTS_PACKAGE}/blueprints/marti_api/certificate_enrollment_api.py"
  EUD_PY="${OTS_PACKAGE}/eud_handler/EudHandler.py"
  MISSION_PY="${OTS_PACKAGE}/blueprints/marti_api/mission_marti_api.py"

  check_marker "${TOKEN_PY}" "AIRSOFT_ENROLLMENT_TOKEN_SUBJECT_BINDING_V1" "enrollment token subject binding"
  check_marker "${ENROLL_PY}" "AIRSOFT_CERT_ENROLLMENT_IDENTITY_BINDING_V1" "certificate identity binding"
  check_marker "${ENROLL_PY}" "AIRSOFT_CO_HQ_PROFILE_ON_ENROLLMENT_V1" "CO HQ enrollment profile"
  check_marker "${ENROLL_PY}" "AIRSOFT_CO_DEVICE_HANDOVER_V2" "CO device handover"
  check_marker "${ENROLL_PY}" "AIRSOFT_CO_DEVICE_RESTORE_CLEANUP_V1" "CO restore profile cleanup"
  check_marker "${ENROLL_PY}" "AIRSOFT_CO_DEVICE_RESTORE_HISTORY_V1" "legacy CO restore history"
  check_marker "${ENROLL_PY}" "AIRSOFT_AUTHENTICATED_EUD_SWITCH_V1" "authenticated EUD switching"
  check_marker "${ENROLL_PY}" "AIRSOFT_CO_DAILY_CERT_WINDOW_V1" "daily CO certificate window"
  check_marker "${ENROLL_PY}" "AIRSOFT_EUD_AUDIT_LOG_V2" "fail-safe EUD switch audit log"
  check_marker "${EUD_PY}" "AIRSOFT_CO_DEVICE_OWNER_GUARD_V1" "CO EUD owner guard"
  check_marker "${EUD_PY}" "AIRSOFT_AUTHENTICATED_EUD_STREAM_GUARD_V1" "stale stream owner guard"
  check_marker "${EUD_PY}" "AIRSOFT_CO_DAILY_STREAM_GUARD_V1" "daily CO stream guard"
  check_marker "${MISSION_PY}" "AIRSOFT_GAME_MISSION_ISOLATION_V1" "mission isolation patch"

  if sudo -u "${OTS_RUN_USER}" env HOME="${OTS_HOME}" OTS_DATA_FOLDER="${OTS_DATA_FOLDER}" "${OTS_PYTHON}" -m py_compile "${TOKEN_PY}" "${ENROLL_PY}" "${EUD_PY}" "${MISSION_PY}"; then
    pass "OpenTAKServer patched modules compile"
  else
    fail "OpenTAKServer patched modules compile"
  fi
fi

section "GAME MEMBERSHIP AND MISSIONS"
MEMBERSHIP_OUT="$(mktemp)"
if sudo -u "${OTS_RUN_USER}" env HOME="${OTS_HOME}" OTS_DATA_FOLDER="${OTS_DATA_FOLDER}" "${OTS_PYTHON}" - <<'PY' >"${MEMBERSHIP_OUT}" 2>&1
from opentakserver.app import create_app
from opentakserver.extensions import db
from opentakserver.models.GroupUser import GroupUser

EXPECTED = {
    "CORood": {("GameRed", "IN"), ("GameRed", "OUT")},
    "COBLauw": {("GameBlue", "IN"), ("GameBlue", "OUT")},
    "3DAdmin": {("GameRed", "OUT"), ("GameBlue", "OUT")},
    "3DivisionGameMaster": {("GameRed", "OUT"), ("GameBlue", "OUT")},
}

app = create_app(cli=True)
ok = True
with app.app_context():
    for username, expected in EXPECTED.items():
        user = app.security.datastore.find_user(username=username)
        if not user:
            print(f"FAIL {username}: missing")
            ok = False
            continue
        actual = {
            (membership.group.name, str(membership.direction))
            for membership in db.session.execute(
                db.session.query(GroupUser).filter_by(
                    user_id=user.id,
                    enabled=True,
                )
            ).scalars()
            if membership.group and membership.group.name in ("GameRed", "GameBlue")
        }
        if actual == expected:
            print(f"PASS {username}: {sorted(actual)}")
        else:
            print(f"FAIL {username}: actual={sorted(actual)} expected={sorted(expected)}")
            ok = False

raise SystemExit(0 if ok else 1)
PY
then
  pass "exact GameRed/GameBlue membership directions"
else
  fail "GameRed/GameBlue membership directions"
  echo "--- membership verifier output ---"
  cat "${MEMBERSHIP_OUT}"
  echo "--- end membership verifier output ---"
fi
rm -f "${MEMBERSHIP_OUT}"

MISSION_OUT="$(mktemp)"
if sudo -u "${OTS_RUN_USER}" env HOME="${OTS_HOME}" OTS_DATA_FOLDER="${OTS_DATA_FOLDER}" "${OTS_PYTHON}" "${PORTAL_ROOT}/scripts/installers/verify_ots_1713_game_mission_isolation.py" >"${MISSION_OUT}" 2>&1; then
  pass "strict mission isolation verifier"
else
  fail "strict mission isolation verifier"
  echo "--- mission verifier output ---"
  cat "${MISSION_OUT}"
  echo "--- end mission verifier output ---"
fi
rm -f "${MISSION_OUT}"

section "STATE AND AUDIT FILES"
check_mode_600_if_present "${PORTAL_ROOT}/instance/game_state.json" "game state"
check_mode_600_if_present "${OTS_DATA_FOLDER}/logs/airsoft_eud_audit.jsonl" "EUD audit log"

section "SERVICES"
check_service "opentak-portal"
check_service "opentakserver-1713.service"
check_service "rabbitmq-server"
check_service "nginx"

section "PUBLIC HTTPS HEADERS"
HEADERS_FILE="$(mktemp)"
if curl -fsSI --max-time 15 "${PORTAL_URL}" >"${HEADERS_FILE}" 2>&1; then
  check_header() {
    local name="$1"
    local expected="$2"
    local label="$3"
    if grep -i "^${name}:" "${HEADERS_FILE}" | grep -Fqi "${expected}"; then
      pass "${label}"
    else
      fail "${label}"
    fi
  }

  check_header "X-Content-Type-Options" "nosniff" "X-Content-Type-Options"
  check_header "X-Frame-Options" "DENY" "X-Frame-Options"
  check_header "Referrer-Policy" "no-referrer" "Referrer-Policy"
  check_header "Cross-Origin-Opener-Policy" "same-origin" "Cross-Origin-Opener-Policy"
  check_header "Cross-Origin-Resource-Policy" "same-origin" "Cross-Origin-Resource-Policy"
  check_header "Content-Security-Policy" "default-src 'self'" "CSP default-src"
  check_header "Content-Security-Policy" "object-src 'none'" "CSP object-src"
  check_header "Content-Security-Policy" "frame-ancestors 'none'" "CSP frame-ancestors"
  check_header "Strict-Transport-Security" "max-age=31536000" "HSTS"
else
  fail "public portal HTTPS request"
fi
rm -f "${HEADERS_FILE}"

API_HEADERS="$(mktemp)"
if curl -fsSI --max-time 15 "${PORTAL_URL%/}/api/v1/settings" >"${API_HEADERS}" 2>&1; then
  if grep -i '^Cache-Control:' "${API_HEADERS}" | grep -Fqi 'no-store'; then
    pass "API Cache-Control no-store"
  else
    fail "API Cache-Control no-store missing"
  fi
else
  warn "could not verify API Cache-Control header"
fi
rm -f "${API_HEADERS}"

CORS_BAD="$(mktemp)"
if curl -fsSI --max-time 15 -H 'Origin: https://invalid.example'   "${PORTAL_URL%/}/api/v1/settings" >"${CORS_BAD}" 2>&1; then
  if grep -qi '^Access-Control-Allow-Origin:.*invalid.example' "${CORS_BAD}"     || grep -qi '^Access-Control-Allow-Origin:[[:space:]]*\*' "${CORS_BAD}"; then
    fail "CORS accepted an untrusted origin"
  else
    pass "CORS rejects untrusted origin"
  fi
else
  warn "could not verify untrusted-origin CORS response"
fi
rm -f "${CORS_BAD}"

CORS_GOOD="$(mktemp)"
if curl -fsSI --max-time 15 -H 'Origin: https://atak.3division.eu'   "${PORTAL_URL%/}/api/v1/settings" >"${CORS_GOOD}" 2>&1; then
  if grep -qi '^Access-Control-Allow-Origin:[[:space:]]*https://atak\.3division\.eu' "${CORS_GOOD}"; then
    pass "CORS allows production origin"
  else
    fail "CORS production origin header missing"
  fi
else
  warn "could not verify production-origin CORS response"
fi
rm -f "${CORS_GOOD}"

rm -f /tmp/airsoft-verify-raw-html.txt /tmp/airsoft-verify-jwt-storage.txt

section "RESULT"
printf 'PASS=%d  WARN=%d  FAIL=%d\n' "${PASS_COUNT}" "${WARN_COUNT}" "${FAIL_COUNT}"

if [[ "${FAIL_COUNT}" -eq 0 ]]; then
  echo "RESULT: PASS"
  if [[ "${WARN_COUNT}" -gt 0 ]]; then
    echo "Warnings are informational and should be reviewed."
  fi
  exit 0
fi

echo "RESULT: FAIL"
exit 1

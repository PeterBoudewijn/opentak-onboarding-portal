#!/usr/bin/env bash
set -euo pipefail

PORTAL_ROOT="${PORTAL_ROOT:-/opt/opentak-onboarding-portal}"
PORTAL_USER="${PORTAL_USER:-opentak-portal}"
PORTAL_SERVICE="${PORTAL_SERVICE:-opentak-portal}"
OTS_SERVICE="${OTS_SERVICE:-opentakserver-1713.service}"
OTS_PYTHON="${OTS_PYTHON:-/home/team3d/.opentakserver_venv_1713/bin/python}"
ENV_FILE="${PORTAL_ENV_FILE:-${PORTAL_ROOT}/.env}"
VERIFY_AFTER_DEPLOY="${VERIFY_AFTER_DEPLOY:-1}"
AIRSOFT_VERIFIER="${AIRSOFT_VERIFIER:-${PORTAL_ROOT}/scripts/installers/verify_3division_airsoft_stack.sh}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this deployment script with sudo/root." >&2
  exit 1
fi

for cmd in sudo systemctl python3 npm; do
  command -v "${cmd}" >/dev/null 2>&1 || {
    echo "Required command not found: ${cmd}" >&2
    exit 1
  }
done

[[ -d "${PORTAL_ROOT}" ]] || {
  echo "Portal directory not found: ${PORTAL_ROOT}" >&2
  exit 1
}

[[ -f "${ENV_FILE}" ]] || {
  echo "Portal .env not found: ${ENV_FILE}" >&2
  exit 1
}

set_env_value() {
  local key="$1"
  local value="$2"

  if grep -qE "^[[:space:]]*${key}=" "${ENV_FILE}"; then
    sed -i -E "s|^[[:space:]]*${key}=.*$|${key}=${value}|" "${ENV_FILE}"
  else
    printf '\n%s=%s\n' "${key}" "${value}" >> "${ENV_FILE}"
  fi
}

timestamp="$(date +%Y%m%d%H%M%S)"
cp -a "${ENV_FILE}" "${ENV_FILE}.bak.${timestamp}"

# Permanent CO users send + receive in their own team.
set_env_value "GAME_PERMANENT_USERS_RED" "CORood"
# The existing OTS account is historically spelled COBLauw. Keep that internal
# identity stable so current certificates remain valid; the portal/callsign is COBlauw.
set_env_value "GAME_PERMANENT_USERS_BLUE" "COBLauw"
set_env_value "GAME_CO_PORTAL_USERNAME_RED" "CORood"
set_env_value "GAME_CO_PORTAL_USERNAME_BLUE" "COBlauw"
set_env_value "GAME_PUBLIC_TAK_HOST" "atak.3division.eu"
set_env_value "GAME_TAK_STREAM_PORT" "8089"

# Production browser/session hardening. The portal is served only from this
# HTTPS origin; do not allow arbitrary cross-origin credentialed API access.
set_env_value "CORS_ORIGINS" "https://atak.3division.eu"
set_env_value "SESSION_COOKIE_SECURE" "True"
set_env_value "SESSION_COOKIE_SAMESITE" "Lax"

# Watch-only users receive both teams but never get IN membership, so their own
# PLI is not routed into GameRed/GameBlue.
set_env_value "GAME_PERMANENT_OBSERVERS_RED" "3DAdmin,3DivisionGameMaster"
set_env_value "GAME_PERMANENT_OBSERVERS_BLUE" "3DAdmin,3DivisionGameMaster"

# CO login QR codes are intentionally created/reset from the administrator portal.
# Existing legacy .env tokens, if present, are left untouched for compatibility.

if [[ ! -x "${OTS_PYTHON}" ]]; then
  echo "OpenTAKServer Python not found/executable: ${OTS_PYTHON}" >&2
  exit 1
fi

echo "Applying OpenTAKServer airsoft patches..."
"${OTS_PYTHON}" "${PORTAL_ROOT}/scripts/installers/patch_ots_1713_game_mission_isolation.py"
"${OTS_PYTHON}" "${PORTAL_ROOT}/scripts/installers/patch_ots_1713_certificate_enrollment_security.py"
"${OTS_PYTHON}" "${PORTAL_ROOT}/scripts/installers/patch_ots_1713_co_hq_profiles.py"
"${OTS_PYTHON}" "${PORTAL_ROOT}/scripts/installers/patch_ots_1713_co_device_handover.py"
"${OTS_PYTHON}" "${PORTAL_ROOT}/scripts/installers/patch_ots_1713_co_device_restore_cleanup.py"
"${OTS_PYTHON}" "${PORTAL_ROOT}/scripts/installers/patch_ots_1713_co_device_restore_history.py"
"${OTS_PYTHON}" "${PORTAL_ROOT}/scripts/installers/patch_ots_1713_authenticated_eud_switching.py"
"${OTS_PYTHON}" "${PORTAL_ROOT}/scripts/installers/patch_ots_1713_co_daily_access.py"
"${OTS_PYTHON}" "${PORTAL_ROOT}/scripts/installers/patch_ots_1713_eud_audit_log.py"

echo "Building frontend..."
cd "${PORTAL_ROOT}/frontend"
sudo -u "${PORTAL_USER}" npm ci
sudo -u "${PORTAL_USER}" npm run build

echo "Restarting ${PORTAL_SERVICE}..."
systemctl restart "${PORTAL_SERVICE}"
systemctl is-active --quiet "${PORTAL_SERVICE}"

echo "Restarting ${OTS_SERVICE}..."
systemctl restart "${OTS_SERVICE}"
systemctl is-active --quiet "${OTS_SERVICE}"

echo
echo "Airsoft game portal deployment completed."
echo "GameRed send+receive: CORood"
echo "GameBlue send+receive: COBlauw (OTS identity: COBLauw)"
echo "Watch-only on both teams (OUT only): 3DAdmin, 3DivisionGameMaster"
echo "The portal automatically removes IN membership from watch-only observers."
echo "CO login QR codes are created and reset by an administrator in the portal."
echo "Fresh authenticated ATAK/iTAK registration is authoritative for EUD ownership."
echo "Users can switch accounts by registering with valid credentials; CO buttons use bounded enrollment tokens."
echo "CO TAK certificate access is enforced per EUD until the daily 23:00 cutoff."
echo "Authenticated EUD account switches are written to a credential-free JSONL audit log."

if [[ "${VERIFY_AFTER_DEPLOY}" == "1" ]]; then
  if [[ ! -f "${AIRSOFT_VERIFIER}" ]]; then
    echo "Airsoft verifier not found: ${AIRSOFT_VERIFIER}" >&2
    exit 1
  fi

  echo
  echo "Running complete post-deploy verifier..."
  bash "${AIRSOFT_VERIFIER}"
  echo "Post-deploy verifier passed."
else
  echo
  echo "Post-deploy verifier skipped because VERIFY_AFTER_DEPLOY=${VERIFY_AFTER_DEPLOY}."
fi

#!/usr/bin/env bash
# Non-destructively verify a 3Division airsoft disaster-recovery backup.
set -u
set -o pipefail
umask 077

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this verifier with sudo/root." >&2
  exit 2
fi

archive="${1:-}"
if [[ -z "${archive}" || ! -f "${archive}" ]]; then
  echo "Usage: sudo bash $0 /path/to/3division-airsoft-YYYYMMDD-HHMMSS.tar.gz" >&2
  exit 2
fi

for cmd in tar sha256sum mktemp git pg_restore; do
  command -v "${cmd}" >/dev/null 2>&1 || {
    echo "Required command missing: ${cmd}" >&2
    exit 2
  }
done

PASS_COUNT=0
WARN_COUNT=0
FAIL_COUNT=0
pass(){ printf 'PASS  %s\n' "$*"; PASS_COUNT=$((PASS_COUNT+1)); }
warn(){ printf 'WARN  %s\n' "$*"; WARN_COUNT=$((WARN_COUNT+1)); }
fail(){ printf 'FAIL  %s\n' "$*"; FAIL_COUNT=$((FAIL_COUNT+1)); }

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

echo "========== BACKUP ARCHIVE =========="
if tar -xzf "${archive}" -C "${tmp}"; then
  pass "outer archive extracts"
else
  fail "outer archive could not be extracted"
fi

if [[ -f "${tmp}/manifest.txt" ]]; then
  pass "manifest present"
else
  fail "manifest missing"
fi

if [[ -f "${tmp}/SHA256SUMS" ]]; then
  if (cd "${tmp}" && sha256sum -c SHA256SUMS >/dev/null); then
    pass "all internal checksums match"
  else
    fail "internal checksum mismatch"
  fi
else
  fail "SHA256SUMS missing"
fi

manifest_value() {
  local key="$1"
  awk -F= -v wanted="${key}" '$1 == wanted {sub(/^[^=]*=/, ""); print; exit}' "${tmp}/manifest.txt" 2>/dev/null
}

portal_commit="$(manifest_value portal_commit)"
portal_db_backend="$(manifest_value portal_db_backend)"
ots_db_backend="$(manifest_value ots_db_backend)"
ots_data_folder="$(manifest_value ots_data_folder)"

echo
echo "========== PORTAL SOURCE =========="
if [[ -f "${tmp}/portal/portal.bundle" ]]; then
  clone_dir="${tmp}/portal-restore-test"
  if git clone -q "${tmp}/portal/portal.bundle" "${clone_dir}" >/dev/null 2>&1; then
    pass "portal Git bundle can be cloned"
    if [[ -n "${portal_commit}" ]] && git -C "${clone_dir}" cat-file -e "${portal_commit}^{commit}" 2>/dev/null; then
      pass "recorded portal commit exists in bundle"
    else
      fail "recorded portal commit missing from bundle"
    fi
  else
    fail "portal Git bundle cannot be cloned"
  fi
else
  fail "portal Git bundle missing"
fi

if [[ -f "${tmp}/portal/.env" ]]; then
  mode="$(stat -c '%a' "${tmp}/portal/.env" 2>/dev/null || true)"
  if [[ "${mode}" == "600" ]]; then
    pass "portal .env backup present with mode 600"
  else
    warn "portal .env backup present but extracted mode is ${mode:-unknown}"
  fi
else
  fail "portal .env backup missing"
fi

if [[ -f "${tmp}/portal/runtime-data.tar.gz" ]] && tar -tzf "${tmp}/portal/runtime-data.tar.gz" >/dev/null; then
  pass "portal runtime archive readable"
else
  fail "portal runtime archive unreadable/missing"
fi

echo
echo "========== DATABASES =========="
verify_db_backup() {
  local label="$1"
  local backend="$2"
  local dump_file="$3"
  local sqlite_file="$4"

  case "${backend}" in
    postgresql|postgres)
      if [[ -f "${dump_file}" ]] && pg_restore --list "${dump_file}" >/dev/null 2>&1; then
        pass "${label} PostgreSQL dump readable"
      else
        fail "${label} PostgreSQL dump unreadable/missing"
      fi
      ;;
    sqlite)
      if ! command -v sqlite3 >/dev/null 2>&1; then
        warn "sqlite3 unavailable; cannot integrity-check ${label}"
      elif [[ -f "${sqlite_file}" ]] && [[ "$(sqlite3 "${sqlite_file}" 'PRAGMA integrity_check;' 2>/dev/null)" == "ok" ]]; then
        pass "${label} SQLite integrity check"
      else
        fail "${label} SQLite backup failed integrity check"
      fi
      ;;
    *)
      fail "${label} backend missing/unsupported in manifest"
      ;;
  esac
}

verify_db_backup "portal DB" "${portal_db_backend}" "${tmp}/database/portal.dump" "${tmp}/database/portal.sqlite"
verify_db_backup "OTS DB" "${ots_db_backend}" "${tmp}/database/ots.dump" "${tmp}/database/ots.sqlite"

echo
echo "========== OTS DATA / CA =========="
OTS_LIST="${tmp}/ots-data-list.txt"
if [[ -f "${tmp}/ots/ots-data.tar.gz" ]] && tar -tzf "${tmp}/ots/ots-data.tar.gz" >"${OTS_LIST}"; then
  pass "OTS data archive readable"
  ots_base="$(basename "${ots_data_folder:-ots-1713}")"

  # Do not pipe tar directly into grep while pipefail is active: grep -q exits
  # after the first match and tar can then receive SIGPIPE, producing a false
  # negative even though the requested path is present.
  if grep -Fq "${ots_base}/ca/" "${OTS_LIST}"; then
    pass "OTS CA directory present in backup"
  else
    fail "OTS CA directory missing from backup"
  fi

  if grep -Fq "${ots_base}/ca/truststore-root.p12" "${OTS_LIST}"; then
    pass "OTS root truststore present"
  else
    warn "truststore-root.p12 not found at expected path"
  fi
else
  fail "OTS data archive unreadable/missing"
fi

echo
echo "========== SYSTEM CONFIG =========="
if [[ -f "${tmp}/system/system-config.tar.gz" ]] && tar -tzf "${tmp}/system/system-config.tar.gz" >/dev/null; then
  pass "system/Nginx config archive readable"
else
  fail "system/Nginx config archive unreadable/missing"
fi

for file in   "${tmp}/versions/portal-pip-freeze.txt"   "${tmp}/versions/ots-pip-freeze.txt"   "${tmp}/versions/frontend-npm.txt"
do
  if [[ -f "${file}" ]]; then
    pass "version metadata: $(basename "${file}")"
  else
    warn "version metadata missing: $(basename "${file}")"
  fi
done

echo
echo "========== RESULT =========="
printf 'PASS=%d  WARN=%d  FAIL=%d\n' "${PASS_COUNT}" "${WARN_COUNT}" "${FAIL_COUNT}"
if [[ "${FAIL_COUNT}" -eq 0 ]]; then
  echo "RESULT: PASS"
  exit 0
fi
echo "RESULT: FAIL"
exit 1

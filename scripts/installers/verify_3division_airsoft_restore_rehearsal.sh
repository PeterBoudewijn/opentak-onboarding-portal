#!/usr/bin/env bash
# Non-destructive disaster-recovery rehearsal for a 3Division airsoft backup.
#
# Restores the portal Git bundle and runtime state under /var/tmp, opens a copy
# of the portal database, restores the OTS PostgreSQL dump into a temporary
# database, and extracts OTS CA/system data without touching production.
set -euo pipefail
umask 077

PORTAL_ROOT="${PORTAL_ROOT:-/opt/opentak-onboarding-portal}"
BACKUP_VERIFIER="${BACKUP_VERIFIER:-${PORTAL_ROOT}/scripts/installers/verify_3division_airsoft_backup.sh}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this rehearsal with sudo/root." >&2
  exit 2
fi

archive="${1:-}"
if [[ -z "${archive}" || ! -f "${archive}" ]]; then
  echo "Usage: sudo bash $0 /path/to/3division-airsoft-YYYYMMDD-HHMMSS.tar.gz" >&2
  exit 2
fi

for cmd in tar git python3 sqlite3 sudo psql createdb dropdb pg_restore cmp find awk; do
  command -v "${cmd}" >/dev/null 2>&1 || {
    echo "Required command missing: ${cmd}" >&2
    exit 2
  }
done

[[ -f "${BACKUP_VERIFIER}" ]] || {
  echo "Backup verifier not found: ${BACKUP_VERIFIER}" >&2
  exit 2
}

WORK="$(mktemp -d /var/tmp/3division-airsoft-restore-rehearsal.XXXXXX)"
TESTDB="airsoft_restore_test_$(date +%Y%m%d%H%M%S)_$$"

cleanup() {
  if [[ "${TESTDB}" == airsoft_restore_test_* ]]; then
    sudo -u postgres dropdb --if-exists "${TESTDB}" >/dev/null 2>&1 || true
  fi
  rm -rf "${WORK}"
}
trap cleanup EXIT

manifest_value() {
  local key="$1"
  awk -F= -v wanted="${key}" '
    $1 == wanted {
      sub(/^[^=]*=/, "")
      print
      exit
    }
  ' "${WORK}/manifest.txt"
}

echo "========== 1. BACKUP OPNIEUW VERIFIEREN =========="
bash "${BACKUP_VERIFIER}" "${archive}"

echo
echo "========== 2. BACKUP UITPAKKEN =========="
tar -xzf "${archive}" -C "${WORK}"

for required in   "${WORK}/manifest.txt"   "${WORK}/portal/portal.bundle"   "${WORK}/portal/runtime-data.tar.gz"   "${WORK}/database/ots.dump"   "${WORK}/ots/ots-data.tar.gz"
do
  [[ -f "${required}" ]] || {
    echo "FAIL: required backup component missing: ${required}" >&2
    exit 1
  }
done
echo "PASS: hoofdbackup uitgepakt"

PORTAL_COMMIT="$(manifest_value portal_commit)"
PORTAL_DB_BACKEND="$(manifest_value portal_db_backend)"
OTS_DB_BACKEND="$(manifest_value ots_db_backend)"
OTS_DATA_FOLDER="$(manifest_value ots_data_folder)"

echo
echo "========== 3. PORTAL GIT RESTORE =========="
git clone -q "${WORK}/portal/portal.bundle" "${WORK}/portal-restored"
git -C "${WORK}/portal-restored" checkout -q --detach "${PORTAL_COMMIT}"
git -C "${WORK}/portal-restored" cat-file -e "${PORTAL_COMMIT}^{commit}"
echo "PASS: portal Git restore naar ${PORTAL_COMMIT:0:12}"

echo
echo "========== 4. PORTAL RUNTIME RESTORE =========="
mkdir -p "${WORK}/runtime-restored"
tar -xzf "${WORK}/portal/runtime-data.tar.gz" -C "${WORK}/runtime-restored"

if [[ -f "${WORK}/runtime-restored/instance/game_state.json" ]]; then
  python3 -m json.tool "${WORK}/runtime-restored/instance/game_state.json" >/dev/null
  echo "PASS: game_state.json is geldige JSON"
else
  echo "INFO: geen game_state.json in deze backup"
fi

echo
echo "========== 5. PORTAL DATABASE RESTORE =========="
case "${PORTAL_DB_BACKEND}" in
  sqlite)
    [[ -f "${WORK}/database/portal.sqlite" ]] || {
      echo "FAIL: portal SQLite backup ontbreekt" >&2
      exit 1
    }
    cp "${WORK}/database/portal.sqlite" "${WORK}/portal-restored.sqlite"
    result="$(sqlite3 "${WORK}/portal-restored.sqlite" 'PRAGMA integrity_check;')"
    [[ "${result}" == "ok" ]] || {
      echo "FAIL: portal SQLite integrity check: ${result}" >&2
      exit 1
    }
    tables="$(sqlite3 "${WORK}/portal-restored.sqlite" "SELECT COUNT(*) FROM sqlite_master WHERE type='table';")"
    [[ "${tables}" =~ ^[0-9]+$ && "${tables}" -gt 0 ]] || {
      echo "FAIL: geen tabellen gevonden in herstelde portal database" >&2
      exit 1
    }
    echo "PASS: portal SQLite daadwerkelijk hersteld"
    echo "PASS: ${tables} tabellen gevonden"
    ;;
  postgresql|postgres)
    echo "FAIL: portal PostgreSQL rehearsal is nog niet geïmplementeerd" >&2
    exit 1
    ;;
  *)
    echo "FAIL: onbekende portal database backend: ${PORTAL_DB_BACKEND}" >&2
    exit 1
    ;;
esac

echo
echo "========== 6. OTS POSTGRESQL RESTORE =========="
if [[ "${OTS_DB_BACKEND}" != "postgresql" && "${OTS_DB_BACKEND}" != "postgres" ]]; then
  echo "FAIL: onverwachte OTS database backend: ${OTS_DB_BACKEND}" >&2
  exit 1
fi

sudo -u postgres createdb "${TESTDB}"

# WORK is intentionally root-only. Let the root shell open the archive and feed
# it through stdin, so the postgres account never needs directory permissions.
sudo -u postgres pg_restore   --exit-on-error   --no-owner   --no-privileges   --dbname="${TESTDB}"   < "${WORK}/database/ots.dump"

OTS_TABLES="$(
  sudo -u postgres psql     --dbname="${TESTDB}"     -Atc "SELECT COUNT(*) FROM pg_catalog.pg_tables WHERE schemaname='public';"
)"

[[ "${OTS_TABLES}" =~ ^[0-9]+$ && "${OTS_TABLES}" -gt 0 ]] || {
  echo "FAIL: geen OTS tabellen gevonden in testdatabase" >&2
  exit 1
}

echo "PASS: OTS PostgreSQL dump daadwerkelijk hersteld"
echo "PASS: ${OTS_TABLES} OTS tabellen gevonden"

echo
echo "========== 7. OTS DATA EN CA RESTORE =========="
mkdir -p "${WORK}/ots-restored"
tar -xzf "${WORK}/ots/ots-data.tar.gz" -C "${WORK}/ots-restored"

OTS_BASE="$(basename "${OTS_DATA_FOLDER:-ots-1713}")"
RESTORED_CA="${WORK}/ots-restored/${OTS_BASE}/ca"
RESTORED_TRUSTSTORE="${RESTORED_CA}/truststore-root.p12"
LIVE_TRUSTSTORE="${OTS_DATA_FOLDER}/ca/truststore-root.p12"

[[ -d "${RESTORED_CA}" ]] || {
  echo "FAIL: herstelde OTS CA-map ontbreekt" >&2
  exit 1
}
[[ -s "${RESTORED_TRUSTSTORE}" ]] || {
  echo "FAIL: herstelde root truststore ontbreekt/leeg" >&2
  exit 1
}

CA_FILES="$(find "${RESTORED_CA}" -type f | wc -l)"
[[ "${CA_FILES}" -gt 0 ]] || {
  echo "FAIL: herstelde CA-map bevat geen bestanden" >&2
  exit 1
}

echo "PASS: OTS CA daadwerkelijk uit backup hersteld"
echo "PASS: ${CA_FILES} bestanden in herstelde CA-map"

if [[ -f "${LIVE_TRUSTSTORE}" ]] && cmp -s "${RESTORED_TRUSTSTORE}" "${LIVE_TRUSTSTORE}"; then
  echo "PASS: herstelde root truststore is byte-identiek aan productie"
else
  echo "WARN: truststore verschilt van huidige productie"
fi

echo
echo "========== 8. SYSTEM CONFIG RESTORE =========="
mkdir -p "${WORK}/system-restored"
tar -xzf "${WORK}/system/system-config.tar.gz" -C "${WORK}/system-restored"

SYSTEM_FILES="$(find "${WORK}/system-restored" -type f | wc -l)"
[[ "${SYSTEM_FILES}" -gt 0 ]] || {
  echo "FAIL: geen systeemconfigbestanden gevonden" >&2
  exit 1
}

echo "PASS: systeemconfig daadwerkelijk uitgepakt"
echo "PASS: ${SYSTEM_FILES} configuratiebestanden gevonden"

echo
echo "============================================="
echo "RESTORE REHEARSAL: PASS"
echo "============================================="
echo
echo "Productie is NIET gewijzigd."
echo "Tijdelijke PostgreSQL database wordt verwijderd."
echo "Tijdelijke bestanden worden verwijderd."

#!/usr/bin/env bash
# Create a disaster-recovery backup for the 3Division airsoft/OTS stack.
#
# The backup intentionally contains secrets (.env, CA material, certificates,
# database contents). It is created mode 0600 under a root-only directory.
set -euo pipefail
umask 077

PORTAL_ROOT="${PORTAL_ROOT:-/opt/opentak-onboarding-portal}"
PORTAL_USER="${PORTAL_USER:-opentak-portal}"
PORTAL_PYTHON="${PORTAL_PYTHON:-${PORTAL_ROOT}/venv/bin/python}"
OTS_PYTHON="${OTS_PYTHON:-/home/team3d/.opentakserver_venv_1713/bin/python}"
OTS_RUN_USER="${OTS_RUN_USER:-team3d}"
OTS_HOME="${OTS_HOME:-/home/team3d}"
OTS_DATA_FOLDER="${OTS_DATA_FOLDER:-/home/team3d/ots-1713}"
BACKUP_ROOT="${BACKUP_ROOT:-/var/backups/3division-airsoft}"
VERIFY_BACKUP_AFTER_CREATE="${VERIFY_BACKUP_AFTER_CREATE:-1}"
BACKUP_VERIFIER="${BACKUP_VERIFIER:-${PORTAL_ROOT}/scripts/installers/verify_3division_airsoft_backup.sh}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this backup script with sudo/root." >&2
  exit 2
fi

for cmd in git tar sha256sum mktemp sudo pg_dump pg_restore; do
  command -v "${cmd}" >/dev/null 2>&1 || {
    echo "Required command missing: ${cmd}" >&2
    exit 2
  }
done

[[ -d "${PORTAL_ROOT}/.git" ]] || {
  echo "Portal repository not found: ${PORTAL_ROOT}" >&2
  exit 2
}
[[ -x "${PORTAL_PYTHON}" ]] || {
  echo "Portal Python not found: ${PORTAL_PYTHON}" >&2
  exit 2
}
[[ -x "${OTS_PYTHON}" ]] || {
  echo "OTS Python not found: ${OTS_PYTHON}" >&2
  exit 2
}
[[ -d "${OTS_DATA_FOLDER}" ]] || {
  echo "OTS data folder not found: ${OTS_DATA_FOLDER}" >&2
  exit 2
}
[[ -f "${PORTAL_ROOT}/.env" ]] || {
  echo "Portal .env not found." >&2
  exit 2
}

install -d -m 700 "${BACKUP_ROOT}"
stamp="$(date +%Y%m%d-%H%M%S)"
name="3division-airsoft-${stamp}"
stage="$(mktemp -d "${BACKUP_ROOT}/.${name}.XXXXXX")"
archive="${BACKUP_ROOT}/${name}.tar.gz"
bundle_tmp=""

cleanup() {
  [[ -z "${bundle_tmp}" ]] || rm -f "${bundle_tmp}"
  rm -rf "${stage}"
}
trap cleanup EXIT

mkdir -p   "${stage}/portal"   "${stage}/database"   "${stage}/ots"   "${stage}/system"   "${stage}/versions"

echo "Creating backup staging set: ${name}"

portal_commit="$(sudo -u "${PORTAL_USER}" git -C "${PORTAL_ROOT}" rev-parse HEAD)"
portal_branch="$(sudo -u "${PORTAL_USER}" git -C "${PORTAL_ROOT}" branch --show-current || true)"

# Self-contained Git backup of the portal source and all refs.
# The staging directory is intentionally root-only, so create the bundle in a
# portal-user-writable temporary file and move it into the protected staging set.
bundle_tmp="$(mktemp /tmp/3division-portal-bundle.XXXXXX)"
chown "${PORTAL_USER}":"${PORTAL_USER}" "${bundle_tmp}"
sudo -u "${PORTAL_USER}" git -C "${PORTAL_ROOT}" bundle create "${bundle_tmp}" --all
mv "${bundle_tmp}" "${stage}/portal/portal.bundle"
bundle_tmp=""
chmod 600 "${stage}/portal/portal.bundle"

cp -a "${PORTAL_ROOT}/.env" "${stage}/portal/.env"
chmod 600 "${stage}/portal/.env"

# Runtime portal state and uploads are outside Git.
runtime_items=()
for item in instance datapackages updates; do
  if [[ -e "${PORTAL_ROOT}/${item}" ]]; then
    runtime_items+=("${item}")
  fi
done
if (( ${#runtime_items[@]} > 0 )); then
  tar --acls --xattrs --numeric-owner -czf "${stage}/portal/runtime-data.tar.gz"     -C "${PORTAL_ROOT}" "${runtime_items[@]}"
else
  tar -czf "${stage}/portal/runtime-data.tar.gz" --files-from /dev/null
fi

# Determine the portal DB backend without printing connection strings/passwords.
portal_db_info="$(
  sudo -u "${PORTAL_USER}" env HOME="${PORTAL_ROOT}" TESTING=True     bash -c 'cd "$1" && "$2" - <<"PY"
from sqlalchemy import text
from app import create_app
from app.models import db

app = create_app()
with app.app_context():
    backend = db.engine.url.get_backend_name()
    if backend == "sqlite":
        print("sqlite\t" + str(db.engine.url.database or "") + "\t")
    else:
        row = db.session.execute(text("select current_database(), current_user")).one()
        print(f"{backend}\t{row[0]}\t{row[1]}")
PY' _ "${PORTAL_ROOT}" "${PORTAL_PYTHON}"
)"
IFS=$'\t' read -r portal_db_backend portal_db_name portal_db_role <<<"${portal_db_info}"

case "${portal_db_backend}" in
  sqlite)
    command -v sqlite3 >/dev/null 2>&1 || {
      echo "sqlite3 is required for the portal SQLite backup." >&2
      exit 2
    }
    [[ -f "${portal_db_name}" ]] || {
      echo "Portal SQLite database not found." >&2
      exit 2
    }
    sqlite3 "${portal_db_name}" ".backup '${stage}/database/portal.sqlite'"
    ;;
  postgresql|postgres)
    sudo -u postgres pg_dump --format=custom "${portal_db_name}" >"${stage}/database/portal.dump"
    ;;
  *)
    echo "Unsupported portal database backend: ${portal_db_backend}" >&2
    exit 2
    ;;
esac

# Determine the live OTS DB identity as the OTS service user. This deliberately
# emits only backend/database/role names, never the database URI or password.
ots_db_info="$(
  sudo -u "${OTS_RUN_USER}" env HOME="${OTS_HOME}" OTS_DATA_FOLDER="${OTS_DATA_FOLDER}"     "${OTS_PYTHON}" - <<'PY'
from sqlalchemy import text
from opentakserver.app import create_app
from opentakserver.extensions import db

app = create_app(cli=True)
with app.app_context():
    backend = db.engine.url.get_backend_name()
    if backend == "sqlite":
        print("sqlite\t" + str(db.engine.url.database or "") + "\t")
    else:
        row = db.session.execute(text("select current_database(), current_user")).one()
        print(f"{backend}\t{row[0]}\t{row[1]}")
PY
)"
IFS=$'\t' read -r ots_db_backend ots_db_name ots_db_role <<<"${ots_db_info}"

case "${ots_db_backend}" in
  sqlite)
    command -v sqlite3 >/dev/null 2>&1 || {
      echo "sqlite3 is required for the OTS SQLite backup." >&2
      exit 2
    }
    [[ -f "${ots_db_name}" ]] || {
      echo "OTS SQLite database not found." >&2
      exit 2
    }
    sqlite3 "${ots_db_name}" ".backup '${stage}/database/ots.sqlite'"
    ;;
  postgresql|postgres)
    sudo -u postgres pg_dump --format=custom "${ots_db_name}" >"${stage}/database/ots.dump"
    ;;
  *)
    echo "Unsupported OTS database backend: ${ots_db_backend}" >&2
    exit 2
    ;;
esac

# OTS_DATA_FOLDER contains the irreplaceable TAK CA/certificate state.
tar --acls --xattrs --numeric-owner -czf "${stage}/ots/ots-data.tar.gz"   -C "$(dirname "${OTS_DATA_FOLDER}")" "$(basename "${OTS_DATA_FOLDER}")"

# Keep service/Nginx configuration needed to reconstruct the host. Web TLS
# certificates can be reissued; the TAK CA itself is already in OTS_DATA_FOLDER.
system_items=()
for item in   etc/systemd/system/opentak-portal.service   etc/systemd/system/opentakserver-1713.service   etc/nginx/nginx.conf   etc/nginx/sites-available   etc/nginx/sites-enabled   etc/nginx/conf.d   etc/nginx/mtls-atak
do
  if [[ -e "/${item}" ]]; then
    system_items+=("${item}")
  fi
done
if (( ${#system_items[@]} > 0 )); then
  tar --acls --xattrs --numeric-owner -czf "${stage}/system/system-config.tar.gz"     -C / "${system_items[@]}"
else
  tar -czf "${stage}/system/system-config.tar.gz" --files-from /dev/null
fi

# Version metadata helps rebuild virtualenvs if a full host restore is needed.
sudo -u "${PORTAL_USER}" "${PORTAL_PYTHON}" -m pip freeze >"${stage}/versions/portal-pip-freeze.txt" 2>/dev/null || true
sudo -u "${OTS_RUN_USER}" "${OTS_PYTHON}" -m pip freeze >"${stage}/versions/ots-pip-freeze.txt" 2>/dev/null || true
sudo -u "${PORTAL_USER}" bash -c 'cd "$1/frontend" && npm ls --depth=0' _ "${PORTAL_ROOT}"   >"${stage}/versions/frontend-npm.txt" 2>&1 || true
uname -a >"${stage}/versions/uname.txt"
if command -v lsb_release >/dev/null 2>&1; then
  lsb_release -a >"${stage}/versions/os-release.txt" 2>&1 || true
elif [[ -f /etc/os-release ]]; then
  cp /etc/os-release "${stage}/versions/os-release.txt"
fi

cat >"${stage}/manifest.txt" <<EOF
backup_format=1
created_at=$(date --iso-8601=seconds)
portal_commit=${portal_commit}
portal_branch=${portal_branch}
portal_root=${PORTAL_ROOT}
portal_db_backend=${portal_db_backend}
portal_db_name=${portal_db_name}
portal_db_role=${portal_db_role}
ots_data_folder=${OTS_DATA_FOLDER}
ots_db_backend=${ots_db_backend}
ots_db_name=${ots_db_name}
ots_db_role=${ots_db_role}
EOF
chmod 600 "${stage}/manifest.txt"

(
  cd "${stage}"
  find . -type f ! -name SHA256SUMS -print0     | sort -z     | xargs -0 sha256sum > SHA256SUMS
)
chmod 600 "${stage}/SHA256SUMS"

tar --acls --xattrs --numeric-owner -czf "${archive}" -C "${stage}" .
chmod 600 "${archive}"

# Verify the outer archive is readable before reporting success.
tar -tzf "${archive}" >/dev/null

size="$(du -h "${archive}" | awk '{print $1}')"
echo
echo "Backup created successfully."
echo "File: ${archive}"
echo "Size: ${size}"
echo "Portal commit: ${portal_commit:0:12}"
echo
echo "IMPORTANT: this archive contains passwords, private CA material and database data."
echo "Keep it root-only and copy it to encrypted/off-host storage."

if [[ "${VERIFY_BACKUP_AFTER_CREATE}" == "1" ]]; then
  if [[ ! -f "${BACKUP_VERIFIER}" ]]; then
    echo "Backup verifier not found: ${BACKUP_VERIFIER}" >&2
    exit 1
  fi
  echo
  echo "Running non-destructive backup verification..."
  bash "${BACKUP_VERIFIER}" "${archive}"
  echo "Backup verification passed."
fi

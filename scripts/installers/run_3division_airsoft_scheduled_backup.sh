#!/usr/bin/env bash
# Run one verified scheduled backup and retain only the newest scheduled copies.
set -euo pipefail
umask 077

PORTAL_ROOT="${PORTAL_ROOT:-/opt/opentak-onboarding-portal}"
BACKUP_ROOT="${BACKUP_ROOT:-/var/backups/3division-airsoft}"
RETENTION_COUNT="${AIRSOFT_BACKUP_RETENTION_COUNT:-7}"
BACKUP_SCRIPT="${BACKUP_SCRIPT:-${PORTAL_ROOT}/scripts/installers/backup_3division_airsoft_stack.sh}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this scheduled backup wrapper with sudo/root." >&2
  exit 2
fi

if [[ ! "${RETENTION_COUNT}" =~ ^[0-9]+$ ]] || [[ "${RETENTION_COUNT}" -lt 1 ]]; then
  echo "AIRSOFT_BACKUP_RETENTION_COUNT must be an integer >= 1." >&2
  exit 2
fi

[[ -f "${BACKUP_SCRIPT}" ]] || {
  echo "Backup script not found: ${BACKUP_SCRIPT}" >&2
  exit 2
}

echo "Running verified scheduled airsoft backup..."
BACKUP_LABEL=scheduled BACKUP_ROOT="${BACKUP_ROOT}" bash "${BACKUP_SCRIPT}"

mapfile -t backups < <(
  find "${BACKUP_ROOT}"     -maxdepth 1     -type f     -name '3division-airsoft-scheduled-*.tar.gz'     -printf '%T@ %p\n'     | sort -nr     | cut -d' ' -f2-
)

count="${#backups[@]}"
if (( count > RETENTION_COUNT )); then
  echo
  echo "Applying scheduled-backup retention: keep newest ${RETENTION_COUNT}."
  for ((i=RETENTION_COUNT; i<count; i++)); do
    old="${backups[i]}"
    echo "Removing expired scheduled backup: ${old}"
    rm -f -- "${old}"
  done
else
  echo
  echo "Scheduled-backup retention: ${count}/${RETENTION_COUNT} backups present; nothing to remove."
fi

echo
echo "Scheduled airsoft backup completed successfully."

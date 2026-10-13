#!/usr/bin/env bash
# Install/refresh the daily verified 3Division airsoft backup timer.
set -euo pipefail

PORTAL_ROOT="${PORTAL_ROOT:-/opt/opentak-onboarding-portal}"
SERVICE_NAME="3division-airsoft-backup.service"
TIMER_NAME="3division-airsoft-backup.timer"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"
TIMER_PATH="/etc/systemd/system/${TIMER_NAME}"
RUNNER="${PORTAL_ROOT}/scripts/installers/run_3division_airsoft_scheduled_backup.sh"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer with sudo/root." >&2
  exit 2
fi

for cmd in systemctl install; do
  command -v "${cmd}" >/dev/null 2>&1 || {
    echo "Required command missing: ${cmd}" >&2
    exit 2
  }
done

[[ -f "${RUNNER}" ]] || {
  echo "Scheduled backup runner not found: ${RUNNER}" >&2
  exit 2
}

cat >"${SERVICE_PATH}" <<EOF
[Unit]
Description=3Division verified airsoft disaster-recovery backup
ConditionPathExists=${PORTAL_ROOT}
ConditionPathExists=${RUNNER}

[Service]
Type=oneshot
ExecStart=/bin/bash ${RUNNER}
UMask=0077
Nice=10
EOF

cat >"${TIMER_PATH}" <<'EOF'
[Unit]
Description=Run 3Division airsoft disaster-recovery backup daily

[Timer]
OnCalendar=*-*-* 02:30:00 Europe/Amsterdam
Persistent=true
AccuracySec=1min
Unit=3division-airsoft-backup.service

[Install]
WantedBy=timers.target
EOF

chmod 644 "${SERVICE_PATH}" "${TIMER_PATH}"

systemctl daemon-reload
systemctl enable --now "${TIMER_NAME}"

echo
echo "Daily airsoft backup timer installed."
echo "Schedule: every day at 02:30 Europe/Amsterdam"
echo "Retention: newest 7 scheduled backups"
echo "Manual/golden backups are not removed by scheduled retention."
echo
systemctl --no-pager --full status "${TIMER_NAME}" || true
echo
systemctl --no-pager list-timers "${TIMER_NAME}" || true

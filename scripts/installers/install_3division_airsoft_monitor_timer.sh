#!/usr/bin/env bash
set -euo pipefail

PORTAL_ROOT="${PORTAL_ROOT:-/opt/opentak-onboarding-portal}"
CHECKER="${PORTAL_ROOT}/scripts/installers/check_3division_airsoft_operations.sh"
SERVICE_NAME="3division-airsoft-monitor.service"
TIMER_NAME="3division-airsoft-monitor.timer"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"
TIMER_PATH="/etc/systemd/system/${TIMER_NAME}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer with sudo/root." >&2
  exit 2
fi

[[ -f "${CHECKER}" ]] || { echo "Monitor missing: ${CHECKER}" >&2; exit 2; }
bash -n "${CHECKER}"

cat >"${SERVICE_PATH}" <<EOF
[Unit]
Description=3Division TAK operational health check
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/bin/bash ${CHECKER}
UMask=0077
EOF

cat >"${TIMER_PATH}" <<EOF
[Unit]
Description=Run 3Division TAK operational health check every 15 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=15min
Persistent=true
AccuracySec=1min
Unit=${SERVICE_NAME}

[Install]
WantedBy=timers.target
EOF

chmod 644 "${SERVICE_PATH}" "${TIMER_PATH}"
systemctl daemon-reload
systemctl enable --now "${TIMER_NAME}"

echo
echo "Running first operational health check..."
if ! systemctl start "${SERVICE_NAME}"; then
  echo
  echo "Initial health check reported a failure."
  systemctl --no-pager --full status "${SERVICE_NAME}" || true
  echo
  journalctl -u "${SERVICE_NAME}" -n 100 --no-pager || true
  exit 1
fi

echo
echo "Operational monitoring installed."
echo "Interval: every 15 minutes"
echo "Status: /var/lib/3division-airsoft-monitor/last-status.txt"
echo
systemctl --no-pager list-timers "${TIMER_NAME}" || true
echo
cat /var/lib/3division-airsoft-monitor/last-status.txt

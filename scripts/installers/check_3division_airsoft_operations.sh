#!/usr/bin/env bash
set -u
set -o pipefail
umask 077

BACKUP_ROOT="${BACKUP_ROOT:-/var/backups/3division-airsoft}"
STATUS_DIR="${STATUS_DIR:-/var/lib/3division-airsoft-monitor}"
PUBLIC_URL="${PUBLIC_URL:-https://atak.3division.eu/game/api/v1/settings}"
DISK_WARN="${AIRSOFT_MONITOR_DISK_WARN:-80}"
DISK_FAIL="${AIRSOFT_MONITOR_DISK_FAIL:-90}"
BACKUP_MAX_AGE_HOURS="${AIRSOFT_MONITOR_BACKUP_MAX_AGE_HOURS:-30}"
CERT_WARN_DAYS="${AIRSOFT_MONITOR_CERT_WARN_DAYS:-30}"
CERT_FAIL_DAYS="${AIRSOFT_MONITOR_CERT_FAIL_DAYS:-7}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run with sudo/root." >&2
  exit 2
fi

for cmd in awk curl df find grep logger openssl stat systemctl; do
  command -v "${cmd}" >/dev/null 2>&1 || { echo "Missing: ${cmd}" >&2; exit 2; }
done

install -d -m 755 "${STATUS_DIR}"
REPORT="$(mktemp)"
trap 'install -o root -g root -m 644 "${REPORT}" "${STATUS_DIR}/last-status.txt"; rm -f "${REPORT}"' EXIT

PASS_COUNT=0
WARN_COUNT=0
FAIL_COUNT=0
emit(){ printf '%s\n' "$*"; printf '%s\n' "$*" >>"${REPORT}"; }
section(){ emit ""; emit "========== $* =========="; }
pass(){ emit "PASS  $*"; PASS_COUNT=$((PASS_COUNT+1)); }
warn(){ emit "WARN  $*"; WARN_COUNT=$((WARN_COUNT+1)); }
fail(){ emit "FAIL  $*"; FAIL_COUNT=$((FAIL_COUNT+1)); }

check_unit(){
  local unit="$1"
  local state
  state="$(systemctl show "${unit}" -p LoadState --value 2>/dev/null || true)"
  if [[ -z "${state}" || "${state}" == "not-found" ]]; then
    fail "required unit ${unit} not installed"
  elif systemctl is-active --quiet "${unit}"; then
    pass "service ${unit} active"
  else
    fail "service ${unit} is not active"
  fi
}

check_timer(){
  local unit="$1"
  if systemctl is-active --quiet "${unit}"; then pass "timer ${unit} active"; else fail "timer ${unit} is not active"; fi
  if systemctl is-enabled --quiet "${unit}" 2>/dev/null; then pass "timer ${unit} enabled"; else warn "timer ${unit} not enabled"; fi
}

check_disk(){
  local path="$1" label="$2"
  local usage
  usage="$(df -P "${path}" 2>/dev/null | awk 'NR==2 {gsub("%","",$5); print $5}')"
  if [[ ! "${usage}" =~ ^[0-9]+$ ]]; then
    fail "cannot determine ${label} disk usage"
  elif (( usage >= DISK_FAIL )); then
    fail "${label} disk usage ${usage}%"
  elif (( usage >= DISK_WARN )); then
    warn "${label} disk usage ${usage}%"
  else
    pass "${label} disk usage ${usage}%"
  fi
}

emit "3Division TAK operational monitor"
emit "Checked at: $(date --iso-8601=seconds)"

section "CRITICAL SERVICES"
for unit in opentak-portal.service opentakserver-1713.service rabbitmq-server.service nginx.service prosody.service mumble-server.service fail2ban.service; do
  check_unit "${unit}"
done

section "FILESYSTEM"
check_disk "/" "root filesystem"
if [[ -d "${BACKUP_ROOT}" ]]; then check_disk "${BACKUP_ROOT}" "backup filesystem"; else fail "backup directory missing"; fi

section "BACKUPS"
check_timer "3division-airsoft-backup.timer"
latest_line="$(find "${BACKUP_ROOT}" -maxdepth 1 -type f -name '3division-airsoft-scheduled-*.tar.gz' -printf '%T@ %p\n' 2>/dev/null | sort -nr | sed -n '1p')"
if [[ -z "${latest_line}" ]]; then
  fail "no scheduled backup found"
else
  latest_epoch="${latest_line%% *}"; latest_epoch="${latest_epoch%%.*}"
  latest_file="${latest_line#* }"
  age_seconds=$(( $(date +%s) - latest_epoch ))
  age_hours=$(( age_seconds / 3600 ))
  if (( age_seconds > BACKUP_MAX_AGE_HOURS * 3600 )); then
    fail "latest scheduled backup is ${age_hours}h old"
  else
    pass "latest scheduled backup is ${age_hours}h old: $(basename "${latest_file}")"
  fi
fi
backup_result="$(systemctl show 3division-airsoft-backup.service -p Result --value 2>/dev/null || true)"
if [[ "${backup_result}" == "success" ]]; then pass "last backup service result success"; elif [[ -n "${backup_result}" ]]; then fail "last backup service result ${backup_result}"; else warn "backup result unavailable"; fi

section "PUBLIC HTTPS"
http_code="$(curl -sS --max-time 15 -o /dev/null -w '%{http_code}' "${PUBLIC_URL}" 2>/dev/null || true)"
if [[ "${http_code}" == "200" ]]; then pass "public portal HTTP 200"; else fail "public portal HTTP ${http_code:-unreachable}"; fi

section "TLS CERTIFICATES"
cert_count=0
while IFS= read -r cert; do
  [[ -n "${cert}" ]] || continue
  cert_count=$((cert_count+1))
  label="${cert#/etc/letsencrypt/live/}"; label="${label%/fullchain.pem}"
  if ! openssl x509 -in "${cert}" -noout >/dev/null 2>&1; then
    fail "cannot parse certificate ${label}"
  elif ! openssl x509 -in "${cert}" -noout -checkend "$((CERT_FAIL_DAYS*86400))" >/dev/null 2>&1; then
    fail "certificate ${label} expires within ${CERT_FAIL_DAYS} days"
  elif ! openssl x509 -in "${cert}" -noout -checkend "$((CERT_WARN_DAYS*86400))" >/dev/null 2>&1; then
    warn "certificate ${label} expires within ${CERT_WARN_DAYS} days"
  else
    pass "certificate ${label} valid beyond ${CERT_WARN_DAYS} days"
  fi
done < <(find -L /etc/letsencrypt/live -mindepth 2 -maxdepth 2 -type f -name fullchain.pem 2>/dev/null | sort)
(( cert_count > 0 )) || warn "no Let's Encrypt fullchain certificates found"

if systemctl show certbot.timer -p LoadState --value 2>/dev/null | grep -qx loaded; then
  if systemctl is-active --quiet certbot.timer; then pass "certbot.timer active"; else warn "certbot.timer not active"; fi
else
  warn "certbot.timer not installed"
fi

section "TIME"
if command -v timedatectl >/dev/null 2>&1; then
  ntp="$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)"
  if [[ "${ntp}" == "yes" ]]; then pass "system clock synchronized"; else warn "system clock synchronization state: ${ntp:-unknown}"; fi
fi

section "FAILED SYSTEMD UNITS"
failed="$(systemctl --failed --no-legend --plain 2>/dev/null | awk 'NF {print $1}' | tr '\n' ' ' || true)"
if [[ -z "${failed// }" ]]; then pass "no failed systemd units"; else warn "failed units: ${failed}"; fi

section "RESULT"
emit "PASS=${PASS_COUNT}  WARN=${WARN_COUNT}  FAIL=${FAIL_COUNT}"
if (( FAIL_COUNT > 0 )); then
  emit "RESULT: FAIL"
  logger -t 3division-airsoft-monitor -- "FAIL pass=${PASS_COUNT} warn=${WARN_COUNT} fail=${FAIL_COUNT}"
  exit 1
fi
if (( WARN_COUNT > 0 )); then emit "RESULT: PASS WITH WARNINGS"; else emit "RESULT: PASS"; fi
exit 0

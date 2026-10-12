#!/usr/bin/env bash
set -euo pipefail

PORTAL_ROOT="${PORTAL_ROOT:-/opt/opentak-onboarding-portal}"
PORTAL_USER="${PORTAL_USER:-opentak-portal}"
PORTAL_SERVICE="${PORTAL_SERVICE:-opentak-portal}"
ENV_FILE="${PORTAL_ENV_FILE:-${PORTAL_ROOT}/.env}"

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
set_env_value "GAME_PERMANENT_USERS_BLUE" "COBLauw"

# Watch-only users receive both teams but never get IN membership, so their own
# PLI is not routed into GameRed/GameBlue.
set_env_value "GAME_PERMANENT_OBSERVERS_RED" "3DAdmin,3DivisionGameMaster"
set_env_value "GAME_PERMANENT_OBSERVERS_BLUE" "3DAdmin,3DivisionGameMaster"

# CO login QR codes are intentionally created/reset from the administrator portal.
# Existing legacy .env tokens, if present, are left untouched for compatibility.

echo "Building frontend..."
cd "${PORTAL_ROOT}/frontend"
sudo -u "${PORTAL_USER}" npm ci
sudo -u "${PORTAL_USER}" npm run build

echo "Restarting ${PORTAL_SERVICE}..."
systemctl restart "${PORTAL_SERVICE}"
systemctl is-active --quiet "${PORTAL_SERVICE}"

echo
echo "Airsoft game portal deployment completed."
echo "GameRed send+receive: CORood"
echo "GameBlue send+receive: COBLauw"
echo "Watch-only on both teams (OUT only): 3DAdmin, 3DivisionGameMaster"
echo "The portal automatically removes IN membership from watch-only observers."
echo "CO login QR codes are created and reset by an administrator in the portal."

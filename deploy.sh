#!/usr/bin/env bash
# Install or update the market dashboard on Ubuntu 24.04. Safe to re-run.
#
#   sudo ./deploy.sh
#   sudo ALLOW_FROM=203.0.113.7 ./deploy.sh   # open port 8000 to one IP only
#
# Overrides: REPO_URL, BRANCH, APP_DIR, APP_USER, PORT, ALLOW_FROM.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/drkhuls/Screener.git}"
BRANCH="${BRANCH:-main}"
APP_DIR="${APP_DIR:-/opt/market-dashboard}"
APP_USER="${APP_USER:-market-dashboard}"
PORT="${PORT:-8000}"
ALLOW_FROM="${ALLOW_FROM:-}"
SERVICE="market-dashboard"
UNIT_PATH="/etc/systemd/system/${SERVICE}.service"

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi

as_app() { runuser -u "${APP_USER}" -- "$@"; }

echo "==> Packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3-venv python3-pip git ufw

echo "==> User ${APP_USER}"
if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  useradd --system --home-dir "${APP_DIR}" --no-create-home --shell /usr/sbin/nologin "${APP_USER}"
fi

echo "==> Code in ${APP_DIR} (${BRANCH})"
install -d -o "${APP_USER}" -g "${APP_USER}" -m 750 "${APP_DIR}"
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"
if [[ -d "${APP_DIR}/.git" ]]; then
  as_app git -C "${APP_DIR}" fetch --quiet origin "${BRANCH}"
  as_app git -C "${APP_DIR}" checkout --quiet "${BRANCH}"
  as_app git -C "${APP_DIR}" merge --ff-only --quiet "origin/${BRANCH}"
elif [[ -z "$(ls -A "${APP_DIR}")" ]]; then
  as_app git clone --quiet --branch "${BRANCH}" "${REPO_URL}" "${APP_DIR}"
else
  echo "${APP_DIR} is not empty and not a git checkout; move it aside and re-run." >&2
  exit 1
fi

echo "==> Python venv"
if [[ ! -x "${APP_DIR}/.venv/bin/python" ]]; then
  as_app python3 -m venv "${APP_DIR}/.venv"
fi
as_app "${APP_DIR}/.venv/bin/pip" install --quiet --upgrade pip
as_app "${APP_DIR}/.venv/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt"

echo "==> .env"
if [[ ! -f "${APP_DIR}/.env" ]]; then
  install -o "${APP_USER}" -g "${APP_USER}" -m 600 "${APP_DIR}/.env.example" "${APP_DIR}/.env"
  echo "Created ${APP_DIR}/.env from .env.example; fill in the Schwab keys and DASHBOARD_USER/PASSWORD."
fi
chown "${APP_USER}:${APP_USER}" "${APP_DIR}/.env"
chmod 600 "${APP_DIR}/.env"
for token in "${APP_DIR}/token.json" $(sed -n 's/^SCHWAB_TOKEN_PATH=//p' "${APP_DIR}/.env"); do
  if [[ -f "${token}" ]]; then
    chown "${APP_USER}:${APP_USER}" "${token}"
    chmod 600 "${token}"
  fi
done

echo "==> Firewall"
ufw allow OpenSSH >/dev/null
if [[ -n "${ALLOW_FROM}" ]]; then
  ufw delete allow "${PORT}/tcp" >/dev/null 2>&1 || true
  ufw allow from "${ALLOW_FROM}" to any port "${PORT}" proto tcp >/dev/null
else
  ufw allow "${PORT}/tcp" >/dev/null
fi
ufw --force enable >/dev/null
ufw status verbose

echo "==> Service"
sed -e "s|/opt/market-dashboard|${APP_DIR}|g" \
    -e "s|^User=.*|User=${APP_USER}|" \
    -e "s|^Group=.*|Group=${APP_USER}|" \
    "${APP_DIR}/${SERVICE}.service" > "${UNIT_PATH}"
chmod 644 "${UNIT_PATH}"
systemctl daemon-reload
systemctl enable --quiet "${SERVICE}"
systemctl restart "${SERVICE}"
sleep 3
systemctl --no-pager --full status "${SERVICE}" || true

cat <<EOF

Dashboard: http://<server-ip>:${PORT}
Logs:      journalctl -u ${SERVICE} -f
Schwab login (weekly):
  sudo -u ${APP_USER} ${APP_DIR}/.venv/bin/python ${APP_DIR}/backend.py login --manual
EOF

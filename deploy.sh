#!/usr/bin/env bash
# Deploy volvo-mqtt to hal from Linux/macOS/Git Bash.
#   ./deploy.sh [pi@hal.local] [port]
#   KEEP_SERVER_ENV=1 ./deploy.sh      # don't upload the local .env
#
# If a .env exists next to this script it is uploaded to the server and
# replaces the server copy (previous one kept as .env.bak-<timestamp>).
# Tokens in data/ are never touched.
set -euo pipefail
TARGET="${1:-pi@hal.local}"
PORT="${2:-8084}"
APP_DIR=/home/pi/volvo-mqtt
CONFIG_DIR=/home/pi/portainer_data/volvo-mqtt
cd "$(dirname "$0")"

tar -czf - --exclude=__pycache__ --exclude=data --exclude=.env --exclude=tests \
  volvo_mqtt requirements.txt Dockerfile docker-compose.yml .dockerignore .env.example homebridge-mqttthing.json README.md \
  | ssh "$TARGET" "mkdir -p $APP_DIR $CONFIG_DIR/data && tar -xzf - -C $APP_DIR"

UPLOAD_ENV=0
if [ -f .env ] && [ "${KEEP_SERVER_ENV:-0}" != "1" ]; then
  echo "Uploading local .env (server copy will be backed up)..."
  scp .env "$TARGET:/tmp/volvo-mqtt.env"
  UPLOAD_ENV=1
fi

ssh "$TARGET" bash -s <<EOF
set -e
if [ "$UPLOAD_ENV" = "1" ] && [ -f /tmp/volvo-mqtt.env ]; then
  if [ -f $CONFIG_DIR/.env ]; then
    cp -p $CONFIG_DIR/.env $CONFIG_DIR/.env.bak-\$(date +%Y%m%d-%H%M%S)
    ls -1t $CONFIG_DIR/.env.bak-* 2>/dev/null | tail -n +6 | xargs -r rm --
  fi
  mv /tmp/volvo-mqtt.env $CONFIG_DIR/.env
  sed -i 's/\r\$//' $CONFIG_DIR/.env
  chmod 600 $CONFIG_DIR/.env
  echo ">>> Server .env replaced with your local copy (backup kept)."
fi
if [ ! -f $CONFIG_DIR/.env ]; then
  cp $APP_DIR/.env.example $CONFIG_DIR/.env && chmod 600 $CONFIG_DIR/.env
  sed -i "s/^WEB_PORT=.*/WEB_PORT=$PORT/" $CONFIG_DIR/.env
  echo ">>> Created $CONFIG_DIR/.env - edit it, then run this deploy again."
  exit 0
fi
# Keep the dashboard on the chosen port (80 and 8080 are taken on hal)
if grep -q '^WEB_PORT=' $CONFIG_DIR/.env; then
  sed -i "s/^WEB_PORT=.*/WEB_PORT=$PORT/" $CONFIG_DIR/.env
else
  echo "WEB_PORT=$PORT" >> $CONFIG_DIR/.env
fi
if ! grep -q '^WEB_PASSWORD_HASH=.\+\|^WEB_PASSWORD=.\+\|^WEB_AUTH_ENABLED=false' $CONFIG_DIR/.env; then
  echo ">>> WARNING: no dashboard password set. Sign-in will fail until WEB_PASSWORD_HASH is set."
fi
cd $APP_DIR
docker compose up -d --build --force-recreate
sleep 3
docker compose ps
docker logs --tail 20 volvo-mqtt
EOF
echo "Dashboard: http://${TARGET#*@}:$PORT"

#!/bin/sh
# Start script for the Home Assistant add-on.
# Your files are in /config here: the app_configs share (older HA: addon_configs),
# folder <something>_tv_kidlock.
set -e
CONF="${CONFIG_DIR:-/config}"
APP="${APP_DIR:-/app}"
mkdir -p "$CONF/data"

if [ ! -d "$CONF/questions" ]; then
  cp -r "$APP/questions" "$CONF/questions"
  echo "Copied sample questions to the questions folder"
fi

if [ ! -f "$CONF/config.yaml" ]; then
  sed -e "s#^data_dir:.*#data_dir: $CONF/data#" \
      -e "s#^  bank_dir:.*#  bank_dir: $CONF/questions#" \
      "$APP/config.example.yaml" > "$CONF/config.yaml"
  echo "============================================================"
  echo " Created config.yaml in the app_configs share"
  echo " (older HA: addon_configs), in the folder ending _tv_kidlock."
  echo " Edit tv.host, quiz_url, parent_pin and the HA switch,"
  echo " then restart the app."
  echo "============================================================"
  exit 0
fi

if grep -qE '^\s+host: 192\.168\.1\.50\b|parent_pin: "change-me"' "$CONF/config.yaml"; then
  echo "config.yaml still has the example TV IP or PIN - edit it, then restart."
  exit 0
fi

exec python3 -u "$APP/kidlock.py" -c "$CONF/config.yaml"

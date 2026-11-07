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
  echo "Created config.yaml for advanced settings (app_configs share, folder ending _tv_kidlock)."
fi

OPTS="${APP_OPTIONS:-/data/options.json}"
if [ -f "$OPTS" ]; then
  if ! python3 -c "import json,sys; o=json.load(open('$OPTS')); sys.exit(0 if o.get('tv_host') and o.get('parent_pin') else 1)"; then
    echo "============================================================"
    echo " Open the app's Configuration tab, fill in the TV IP and a"
    echo " parent PIN (plus the UniFi block switch), Save, then Start."
    echo "============================================================"
    exit 0
  fi
elif grep -qE '^\s+host: 192\.168\.1\.50\b|parent_pin: "change-me"' "$CONF/config.yaml"; then
  echo "config.yaml still has the example TV IP or PIN - edit it, then restart."
  exit 0
fi

exec python3 -u "$APP/kidlock.py" -c "$CONF/config.yaml"

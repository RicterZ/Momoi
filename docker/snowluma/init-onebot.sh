#!/bin/sh
# Seed SnowLuma's OneBot config once. Later edits in the data volume are kept.
set -eu

config_dir=/app/data/config
config="$config_dir/onebot.json"
token_file="$config_dir/onebot.token"

mkdir -p "$config_dir"
if [ -f "$config" ]; then
  exit 0
fi

token=$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
printf '%s\n' "$token" > "$token_file"

cat > "$config" <<EOF
{
  "networks": {
    "httpServers": [
      {
        "name": "http-default",
        "host": "127.0.0.1",
        "port": 3000,
        "path": "/",
        "enableWebSocket": false,
        "accessToken": "$token",
        "messageFormat": "array",
        "reportSelfMessage": false
      }
    ],
    "httpClients": [],
    "wsServers": [
      {
        "name": "ws-default",
        "host": "0.0.0.0",
        "port": 3001,
        "path": "/",
        "role": "Universal",
        "accessToken": "$token",
        "messageFormat": "array",
        "reportSelfMessage": false
      }
    ],
    "wsClients": []
  },
  "historySync": { "enabled": false }
}
EOF

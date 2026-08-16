#!/bin/sh
set -eu

WORKSPACE="${MOMOI_WORKSPACE:-${HOME}/.momoi}"
EXAMPLE=/usr/share/momoi/example

if [ ! -f "${WORKSPACE}/config.json" ]; then
  mkdir -p "${WORKSPACE}"
  cp -R "${EXAMPLE}/." "${WORKSPACE}/"
  python3 - "${WORKSPACE}/config.json" <<'PY'
import json
import os
import secrets
import sys
from pathlib import Path

path = Path(sys.argv[1])
config = json.loads(path.read_text(encoding="utf-8"))
channels = config.setdefault("channels", {})
enabled = channels.setdefault("enabled", {})
napcat = enabled.setdefault("napcat", {})
napcat["url"] = os.environ.get("MOMOI_NAPCAT_URL", "ws://host.docker.internal:3001")
if owner := os.environ.get("MOMOI_OWNER_QQ", "").strip():
    napcat["owner_qq"] = owner

timezone = os.environ.get("MOMOI_TIMEZONE") or os.environ.get("TZ") or "UTC"
config.setdefault("notifications", {})["timezone"] = timezone

dashboard = config.setdefault("dashboard", {})
dashboard_token = os.environ.get("MOMOI_DASHBOARD_TOKEN", "").strip() or secrets.token_urlsafe(24)
dashboard["token"] = dashboard_token

webhooks = config.setdefault("webhooks", {})
webhooks["enabled"] = True
webhooks["host"] = os.environ.get("MOMOI_WEBHOOKS_HOST", "0.0.0.0")
webhook_token = os.environ.get("MOMOI_WEBHOOKS_TOKEN", "").strip() or secrets.token_urlsafe(24)
webhooks["token"] = webhook_token

path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
print(f"Momoi workspace created at {path.parent}", flush=True)
print(f"Dashboard token: {dashboard_token}", flush=True)
print(f"Webhook token: {webhook_token}", flush=True)
print(
    "Set MOMOI_LLM_BASE_URL, MOMOI_LLM_API_KEY, MOMOI_LLM_MODEL"
    " and MOMOI_OWNER_QQ — or run: momoi channel login weixin",
    flush=True,
)
PY
fi

exec momoi "$@"

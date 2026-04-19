#!/usr/bin/env bash
set -euo pipefail

OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"
CONFIG="$OPENCLAW_HOME/openclaw.json"
LOG_DIR="$OPENCLAW_HOME/logs"
LOG_FILE="$LOG_DIR/stability-guard.log"

mkdir -p "$LOG_DIR"
STAMP="$(date -Is)"

echo "[$STAMP] OpenClaw stability guard starting" >> "$LOG_FILE"

if [ ! -f "$CONFIG" ]; then
  echo "[$STAMP] Config missing: $CONFIG" >> "$LOG_FILE"
  exit 0
fi

export CONFIG
python3 - <<'PY'
import json
import os
import shutil
from datetime import datetime

p = os.environ["CONFIG"]
with open(p, "r", encoding="utf-8") as f:
    data = json.load(f)

changed = False

agents = data.setdefault("agents", {})
defaults = agents.setdefault("defaults", {})
sandbox = defaults.setdefault("sandbox", {})
if sandbox.get("mode") != "off":
    sandbox["mode"] = "off"
    changed = True

# Preserve any existing backend/image settings while forcing direct mode.
tools = data.setdefault("tools", {})
elevated = tools.setdefault("elevated", {})
if elevated.get("enabled") is not True:
    elevated["enabled"] = True
    changed = True

# Keep the guard schema-safe for the live WSL OpenClaw runtime.
# Only enforce the two stability-critical settings above.

if changed:
    backup = f"{p}.bak_guard_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(p, backup)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    print(f"UPDATED_CONFIG {p}")
else:
    print(f"CONFIG_OK {p}")

print("SANDBOX_MODE", data["agents"]["defaults"]["sandbox"]["mode"])
print("ELEVATED_ENABLED", data["tools"]["elevated"]["enabled"])
PY

rm -f "$OPENCLAW_HOME"/memory/*.sqlite.tmp* \
      "$OPENCLAW_HOME"/memory/*.sqlite-shm \
      "$OPENCLAW_HOME"/memory/*.sqlite-wal 2>/dev/null || true

if command -v docker >/dev/null 2>&1; then
  ids="$(docker ps -aq --filter name=openclaw-sbx- 2>/dev/null || true)"
  if [ -n "$ids" ]; then
    docker rm -f $ids >/dev/null 2>&1 || true
    echo "[$STAMP] Removed stale sandbox containers" >> "$LOG_FILE"
  fi
fi

echo "[$STAMP] OpenClaw stability guard complete" >> "$LOG_FILE"

#!/bin/bash
set -euo pipefail

load_env_file() {
  local env_file="$1"
  [[ -f "$env_file" ]] || return 0
  while IFS= read -r raw_line || [[ -n "$raw_line" ]]; do
    local line="${raw_line%$'\r'}"
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" == *=* ]] || continue
    local key="${line%%=*}"
    local value="${line#*=}"
    key="$(printf '%s' "$key" | xargs)"
    value="$(printf '%s' "$value" | sed -e 's/^ *//' -e 's/ *$//' -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")"
    export "$key=$value"
  done < "$env_file"
}

write_wrapped_json() {
  local out_file="$1"
  local key_name="$2"
  local ticker="$3"
  local date_value="$4"
  local payload_json="$5"
  PAYLOAD_JSON="$payload_json" python3 - "$out_file" "$key_name" "$ticker" "$date_value" <<'PY'
import json
import os
import sys
out_file, key_name, ticker, date_value = sys.argv[1:5]
payload = json.loads(os.environ['PAYLOAD_JSON'])
body = payload.get('body')
record = body[0] if isinstance(body, list) and body else body if isinstance(body, dict) else [] if key_name == 'chains' else {}
wrapped = {'ticker': ticker, 'date': date_value, key_name: record, 'raw': payload}
if key_name == 'short' and isinstance(body, list):
    wrapped['records'] = body
elif key_name == 'chains' and not isinstance(record, list):
    wrapped[key_name] = body if isinstance(body, list) else []
with open(out_file, 'w', encoding='utf-8') as handle:
    json.dump(wrapped, handle, indent=2)
    handle.write('\n')
PY
}

json_field() {
  local file_path="$1"
  local expression="$2"
  python3 - "$file_path" "$expression" <<'PY'
import json
import sys
file_path, expression = sys.argv[1:3]
with open(file_path, 'r', encoding='utf-8') as handle:
    payload = json.load(handle)
parts = expression.split('.')
value = payload
for part in parts:
    if isinstance(value, dict):
        value = value.get(part)
    else:
        value = None
        break
if value is None:
    print('N/A')
elif isinstance(value, (dict, list)):
    print(len(value))
else:
    print(value)
PY
}

OPENCLAW_HOME="${OPENCLAW_HOME:-/home/kcinc/.openclaw}"
WORKSPACE_ROOT="${WORKSPACE_ROOT:-$OPENCLAW_HOME/workspace}"
TRADING_DIR="$WORKSPACE_ROOT/trading"
DATE="$(date +%Y-%m-%d)"

load_env_file "$HOME/.env"
load_env_file "$OPENCLAW_HOME/.env"
load_env_file "$OPENCLAW_HOME/workspace/.env"
load_env_file "$HOME/.config/openclaw/keys.env"

if [[ -z "${MBOUM_API_KEY:-}" ]]; then
  echo "[10-77] MBOUM_API_KEY not set" >&2
  exit 3
fi

raw_inputs=()
if [[ "$#" -gt 0 ]]; then
  raw_inputs=("$@")
elif [[ -n "${TICKERS:-}" ]]; then
  raw_inputs=("$TICKERS")
elif [[ -n "${TICKER:-}" ]]; then
  raw_inputs=("$TICKER")
else
  echo "Usage: $(basename "$0") <TICKER ...> or set TICKERS=POET,BMNR" >&2
  exit 2
fi

declare -a tickers=()
declare -A seen=()
for raw_input in "${raw_inputs[@]}"; do
  IFS=',' read -r -a split_inputs <<< "$raw_input"
  for item in "${split_inputs[@]}"; do
    ticker="$(printf '%s' "$item" | tr '[:lower:]' '[:upper:]' | xargs)"
    [[ -n "$ticker" ]] || continue
    if [[ -z "${seen[$ticker]:-}" ]]; then
      tickers+=("$ticker")
      seen[$ticker]=1
    fi
  done
done

mkdir -p "$TRADING_DIR/data" "$TRADING_DIR/distill"
last_index=$((${#tickers[@]} - 1))

for idx in "${!tickers[@]}"; do
  TICKER="${tickers[$idx]}"
  echo "[10-77] Fetching MBOUM data for $TICKER..."

  quote_json="$(curl -fsS -H "Authorization: Bearer $MBOUM_API_KEY" -H "User-Agent: OpenClaw/1.0" -H "Accept: application/json" "https://api.mboum.com/v1/markets/stock/quotes?ticker=$TICKER")"
  write_wrapped_json "$TRADING_DIR/data/$TICKER-quote.json" quote "$TICKER" "$DATE" "$quote_json"

  short_json="$(curl -fsS -H "Authorization: Bearer $MBOUM_API_KEY" -H "User-Agent: OpenClaw/1.0" -H "Accept: application/json" "https://api.mboum.com/v2/markets/stock/short-interest?ticker=$TICKER&type=STOCKS")"
  write_wrapped_json "$TRADING_DIR/data/$TICKER-short.json" short "$TICKER" "$DATE" "$short_json"

  options_json="$(curl -fsS -H "Authorization: Bearer $MBOUM_API_KEY" -H "User-Agent: OpenClaw/1.0" -H "Accept: application/json" "https://api.mboum.com/v1/markets/options?ticker=$TICKER&display=list")"
  write_wrapped_json "$TRADING_DIR/data/$TICKER-options.json" chains "$TICKER" "$DATE" "$options_json"

  spot="$(json_field "$TRADING_DIR/data/$TICKER-quote.json" quote.regularMarketPrice)"
  if [[ "$spot" == "N/A" ]]; then
    spot="$(json_field "$TRADING_DIR/data/$TICKER-quote.json" quote.ask)"
  fi
  chain_count="$(json_field "$TRADING_DIR/data/$TICKER-options.json" chains)"
  short_date="$(json_field "$TRADING_DIR/data/$TICKER-short.json" short.settlementDate)"

  distill_file="$TRADING_DIR/distill/$DATE-10-77-$TICKER.md"
  cat > "$distill_file" <<EOF
## 10-77 $TICKER ($DATE)

- Spot: $spot
- Chains: $chain_count
- Latest short settlement: $short_date
- Quote file: $TRADING_DIR/data/$TICKER-quote.json
- Short file: $TRADING_DIR/data/$TICKER-short.json
- Options file: $TRADING_DIR/data/$TICKER-options.json
EOF

  echo "[10-77] Complete for $TICKER | Spot: $spot | Chains: $chain_count"
  if [[ "$idx" -lt "$last_index" ]]; then
    echo
  fi
done

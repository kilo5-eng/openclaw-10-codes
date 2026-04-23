#!/bin/bash
# 10-77 BMNR v7 - MBOUM w/ working /stock/quotes (data!) + guards
set -euo pipefail

TICKER=BMNR
DATE=$(date +%Y-%m-%d)
DIR=\"/home/kcinc/.openclaw/workspace/trading\"

echo \"[10-77 v7] Fetching MBOUM data for $TICKER...\"
mkdir -p \"$DIR/data\" \"$DIR/distill\"

# Load env full path
[[ -f /home/kcinc/.openclaw/workspace/.env ]] && . /home/kcinc/.openclaw/workspace/.env
[[ -f ~/.hermes/.env ]] && . ~/.hermes/.env

# Quote (/stock/quotes - working)
curl -s \"https://api.mboum.com/v1/markets/stock/quotes?ticker=$TICKER\" \\
  -H \"Authorization: Bearer \$MBOUM_API_KEY\" | \\
jq --arg ticker \"$TICKER\" --arg date \"$DATE\" \\
  '{ticker: $ticker, date: $date, quote: (.body[0] // {})}' > \"$DIR/data/$TICKER-quote.json\"

# Short (v2)
curl -s \"https://api.mboum.com/v2/markets/stock/short-interest?ticker=$TICKER&type=STOCKS\" \\
  -H \"Authorization: Bearer \$MBOUM_API_KEY\" | \\
jq --arg ticker \"$TICKER\" --arg date \"$DATE\" \\
  '{ticker: $ticker, date: $date, short: (.body[0] // {})}' > \"$DIR/data/$TICKER-short.json\"

# Options
curl -s \"https://api.mboum.com/v1/markets/options?ticker=$TICKER&display=list\" \\
  -H \"Authorization: Bearer \$MBOUM_API_KEY\" | \\
jq --arg ticker \"$TICKER\" --arg date \"$DATE\" \\
  '{ticker: $ticker, date: $date, chains: (.body // [])}' > \"$DIR/data/$TICKER-options.json\"

# Distill
SPOT=$(jq -r '(.quote.regularMarketPrice // \"N/A\")' \"$DIR/data/$TICKER-quote.json\")
CHAIN_COUNT=$(jq '(.chains | length // 0)' \"$DIR/data/$TICKER-options.json\")
echo \"## 10-77 $TICKER ($DATE) | Spot: $SPOT | Chains: $CHAIN_COUNT\" > \"$DIR/distill/2026-04-22-10-77-$TICKER.md\"

echo \"✅ v7 complete.\"
ls -la \"$DIR/data/$TICKER-*\" \"$DIR/distill/*$TICKER*\"
chmod +x \"$0\"
#!/bin/bash
# 10-77 BMNR v6.1 - MBOUM (jq safe, env load)
set -euo pipefail

TICKER=BMNR
DATE=$(date +%Y-%m-%d)
DIR=\"/home/kcinc/.openclaw/workspace/trading\"

echo \"[10-77 v6.1] Fetching MBOUM data for $TICKER...\"
mkdir -p \"$DIR/data\" \"$DIR/distill\"

# Load env (MBOUM_KEY etc)
for envfile in .env ~/.env ~/.hermes/.env; do
  [[ -f $envfile ]] && . $envfile
done

# Quote
curl -s \"https://api.mboum.com/v1/markets/quote?symbol=$TICKER&type=STOCKS\" \\
  -H \"Authorization: Bearer \$MBOUM_API_KEY\" | \\
jq --arg ticker \"$TICKER\" --arg date \"$DATE\" \\
  '{ticker: $ticker, date: $date, quote: (.body[0] // {})}' > \"$DIR/data/$TICKER-quote.json\"

# Short
curl -s \"https://api.mboum.com/v2/markets/stock/short-interest?ticker=$TICKER&type=STOCKS\" \\
  -H \"Authorization: Bearer \$MBOUM_API_KEY\" | \\
jq --arg ticker \"$TICKER\" --arg date \"$DATE\" \\
  '{ticker: $ticker, date: $date, short: (.body[0] // {})}' > \"$DIR/data/$TICKER-short.json\"

# Options
curl -s \"https://api.mboum.com/v1/markets/options?ticker=$TICKER&display=list\" \\
  -H \"Authorization: Bearer \$MBOUM_API_KEY\" | \\
jq --arg ticker \"$TICKER\" --arg date \"$DATE\" \\
  '{ticker: $ticker, date: $date, chains: (.body // [])}' > \"$DIR/data/$TICKER-options.json\"

# Distill safe
SPOT=$(jq -r 'if .quote.regularMarketPrice then .quote.regularMarketPrice else \"N/A\" end' \"$DIR/data/$TICKER-quote.json\")
CHAIN_COUNT=$(jq '.chains | length // 0' \"$DIR/data/$TICKER-options.json\")
echo \"## 10-77 $TICKER ($DATE) | Spot: $SPOT | Chains: $CHAIN_COUNT\" > \"$DIR/distill/$(date +%Y-%m-%d)-10-77-$TICKER.md\"

echo \"✅ 10-77 v6.1 $TICKER complete.\"
ls -la \"$DIR/data/$TICKER-*\" \"$DIR/distill/*$TICKER*\"
chmod +x \"$0\"
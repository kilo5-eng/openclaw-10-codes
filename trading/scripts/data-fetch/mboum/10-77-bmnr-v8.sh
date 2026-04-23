#!/bin/bash
# 10-77 BMNR v8 - No .env load (key set), /stock/quotes, safe jq
set -euo pipefail

TICKER=BMNR
DATE=$(date +%Y-%m-%d)
DIR=\"/home/kcinc/.openclaw/workspace/trading\"

echo \"[10-77 v8] Fetching MBOUM data for $TICKER...\"
mkdir -p \"$DIR/data\" \"$DIR/distill\"

# Quote working endpoint
curl -s \"https://api.mboum.com/v1/markets/stock/quotes?ticker=$TICKER\" \\
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

# Distill
SPOT=$(jq -r '.quote.regularMarketPrice // \"N/A\"' \"$DIR/data/$TICKER-quote.json\")
CHAIN_COUNT=$(jq '.chains | length // 0' \"$DIR/data/$TICKER-options.json\")
echo \"## 10-77 $TICKER ($DATE) | Spot: $SPOT | Chains: $CHAIN_COUNT\" > \"$DIR/distill/$(date +%Y-%m-%d)-10-77-$TICKER.md\"

echo \"✅ v8 complete.\"
ls -la \"$DIR/data/$TICKER-*\" \"$DIR/distill/*$TICKER*\"
chmod +x \"$0\"
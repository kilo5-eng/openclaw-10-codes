#!/bin/bash
# 10-77 BMNR v9 - body object (not array!), full headers
set -euo pipefail

TICKER=BMNR
DATE=$(date +%Y-%m-%d)
DIR=\"/home/kcinc/.openclaw/workspace/trading\"

echo \"[10-77 v9] Fetching MBOUM data for $TICKER...\"
mkdir -p \"$DIR/data\" \"$DIR/distill\"

# Quote (/stock/quotes, body object)
curl -s -H \"Authorization: Bearer \$MBOUM_API_KEY\" \\
  -H \"User-Agent: OpenClaw/1.0\" \\
  -H \"Accept: application/json\" \\
  \"https://api.mboum.com/v1/markets/stock/quotes?ticker=$TICKER\" | \\
jq --arg ticker \"$TICKER\" --arg date \"$DATE\" \\
  '{ticker: $ticker, date: $date, quote: (.body // {})}' > \"$DIR/data/$TICKER-quote.json\"

# Short (body object or array[0])
curl -s -H \"Authorization: Bearer \$MBOUM_API_KEY\" \\
  -H \"User-Agent: OpenClaw/1.0\" \\
  -H \"Accept: application/json\" \\
  \"https://api.mboum.com/v2/markets/stock/short-interest?ticker=$TICKER&type=STOCKS\" | \\
jq --arg ticker \"$TICKER\" --arg date \"$DATE\" \\
  '{ticker: $ticker, date: $date, short: (.body // {})}' > \"$DIR/data/$TICKER-short.json\"

# Options
curl -s -H \"Authorization: Bearer \$MBOUM_API_KEY\" \\
  -H \"User-Agent: OpenClaw/1.0\" \\
  -H \"Accept: application/json\" \\
  \"https://api.mboum.com/v1/markets/options?ticker=$TICKER&display=list\" | \\
jq --arg ticker \"$TICKER\" --arg date \"$DATE\" \\
  '{ticker: $ticker, date: $date, chains: (.body // [])}' > \"$DIR/data/$TICKER-options.json\"

# Distill
SPOT=$(jq -r '.quote.lastSalePrice // .quote.regularMarketPrice // \"N/A\"' \"$DIR/data/$TICKER-quote.json\")
CHAIN_COUNT=$(jq '.chains | length // 0' \"$DIR/data/$TICKER-options.json\")
echo \"## 10-77 $TICKER ($DATE) | Spot: $SPOT | Chains: $CHAIN_COUNT\" > \"$DIR/distill/$(date +%Y-%m-%d)-10-77-$TICKER.md\"

echo \"✅ v9 complete (body object).\"

chmod +x \"$0\"source .env 2>/dev/null || true

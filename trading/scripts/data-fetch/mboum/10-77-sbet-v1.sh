#!/bin/bash
# 10-77 SBET v1 - copy v9
set -euo pipefail

TICKER=SBET
DATE=$(date +%Y-%m-%d)
DIR=\"/home/kcinc/.openclaw/workspace/trading\"

echo \"[10-77 v1] Fetching MBOUM data for $TICKER...\"
mkdir -p \"$DIR/data\" \"$DIR/distill\"

curl -s -H \"Authorization: Bearer \$MBOUM_API_KEY\" \\
  -H \"User-Agent: OpenClaw/1.0\" \\
  -H \"Accept: application/json\" \\
  \"https://api.mboum.com/v1/markets/stock/quotes?ticker=$TICKER\" | \\
jq --arg ticker \"$TICKER\" --arg date \"$DATE\" \\
  '{ticker: $ticker, date: $date, quote: (.body // {})}' > \"$DIR/data/$TICKER-quote.json\"

curl -s -H \"Authorization: Bearer \$MBOUM_API_KEY\" \\
  -H \"User-Agent: OpenClaw/1.0\" \\
  -H \"Accept: application/json\" \\
  \"https://api.mboum.com/v2/markets/stock/short-interest?ticker=$TICKER&type=STOCKS\" | \\
jq --arg ticker \"$TICKER\" --arg date \"$DATE\" \\
  '{ticker: $ticker, date: $date, short: (.body // {})}' > \"$DIR/data/$TICKER-short.json\"

curl -s -H \"Authorization: Bearer \$MBOUM_API_KEY\" \\
  -H \"User-Agent: OpenClaw/1.0\" \\
  -H \"Accept: application/json\" \\
  \"https://api.mboum.com/v1/markets/options?ticker=$TICKER&display=list\" | \\
jq --arg ticker \"$TICKER\" --arg date \"$DATE\" \\
  '{ticker: $ticker, date: $date, chains: (.body // [])}' > \"$DIR/data/$TICKER-options.json\"

SPOT=$(jq -r '.quote.lastSalePrice // .quote.regularMarketPrice // \"N/A\"' \"$DIR/data/$TICKER-quote.json\")
CHAIN_COUNT=$(jq '.chains | length // 0' \"$DIR/data/$TICKER-options.json\")
echo \"## 10-77 $TICKER ($DATE) | Spot: $SPOT | Chains: $CHAIN_COUNT\" > \"$DIR/distill/$(date +%Y-%m-%d)-10-77-$TICKER.md\"

echo \"✅ v1 complete.\"
chmod +x \"$0\"
#!/bin/bash
# 10-77 multi-ticker v1 - MBOUM quote/short/options, handles list or single
set -euo pipefail

TICKERS=${1:-BMNR}
DATE=$(date +%Y-%m-%d)
DIR=\"/home/kcinc/.openclaw/workspace/trading\"

# Split if multi
IFS=',' read -ra TICKER_LIST <<< \"$TICKERS\"

for TICKER in \"\${TICKER_LIST[@]}\"; do
  echo \"[10-77 v1] Fetching for $TICKER...\"
  mkdir -p \"$DIR/data\" \"$DIR/distill\"

  # Quote
  curl -s -H \"Authorization: Bearer \$MBOUM_API_KEY\" \\
    -H \"User-Agent: OpenClaw/1.0\" \\
    \"https://api.mboum.com/v1/markets/stock/quotes?ticker=$TICKER\" | \\
  jq --arg ticker \"$TICKER\" --arg date \"$DATE\" \\
    '{ticker: $ticker, date: $date, quote: (.body // {})}' > \"$DIR/data/$TICKER-quote.json\"

  # Short (empty OK)
  curl -s -H \"Authorization: Bearer \$MBOUM_API_KEY\" \\
    -H \"User-Agent: OpenClaw/1.0\" \\
    \"https://api.mboum.com/v2/markets/stock/short-interest?ticker=$TICKER&type=STOCKS\" | \\
  jq --arg ticker \"$TICKER\" --arg date \"$DATE\" \\
    '{ticker: $ticker, date: $date, short: (.body // {})}' > \"$DIR/data/$TICKER-short.json\"

  # Options
  curl -s -H \"Authorization: Bearer \$MBOUM_API_KEY\" \\
    -H \"User-Agent: OpenClaw/1.0\" \\
    \"https://api.mboum.com/v1/markets/options?ticker=$TICKER&display=list\" | \\
  jq --arg ticker \"$TICKER\" --arg date \"$DATE\" \\
    '{ticker: $ticker, date: $date, chains: (.body // [])}' > \"$DIR/data/$TICKER-options.json\"

  # Distill
  SPOT=$(jq -r '.quote.regularMarketPrice // .quote.lastSalePrice // \"N/A\"' \"$DIR/data/$TICKER-quote.json\")
  CHAIN_COUNT=$(jq '.chains | length // 0' \"$DIR/data/$TICKER-options.json\")
  echo \"## 10-77 $TICKER ($DATE) | Spot: $SPOT | Chains: $CHAIN_COUNT\" > \"$DIR/distill/$(date +%Y-%m-%d)-10-77-$TICKER.md\"
done

echo \"✅ 10-77 multi v1 complete for $TICKERS\"
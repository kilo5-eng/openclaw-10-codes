#!/bin/bash
# 10-77 BMNR v5 - MBOUM Options + Short Interest (fixed escapes, no yf)
set -e

TICKER=BMNR
DATE=$(date +%Y-%m-%d)
DIR="/home/kcinc/.openclaw/workspace/trading"

echo "[10-77] Fetching MBOUM data for $TICKER..."

# Quote
curl -s "https://api.mboum.com/v1/markets/quote?symbol=$TICKER&type=STOCKS" \
  -H "Authorization: Bearer $MBOUM_API_KEY" | jq --arg ticker "$TICKER" --arg date "$DATE" '{ticker: $ticker, date: $date, quote: .body[0]}' > "$DIR/data/$TICKER-quote.json"

# Short Interest (v2 endpoint)
curl -s "https://api.mboum.com/v2/markets/stock/short-interest?ticker=$TICKER&type=STOCKS" \
  -H "Authorization: Bearer $MBOUM_API_KEY" | jq --arg ticker "$TICKER" --arg date "$DATE" '{ticker: $ticker, date: $date, short: .body[0]}' > "$DIR/data/$TICKER-short.json"

# Options chains
curl -s "https://api.mboum.com/v1/markets/options?ticker=$TICKER&display=list" \
  -H "Authorization: Bearer $MBOUM_API_KEY" | jq --arg ticker "$TICKER" --arg date "$DATE" '{ticker: $ticker, date: $date, chains: .body}' > "$DIR/data/$TICKER-options.json"

# No yf fallback (policy: MBOUM/Fintel only)

# Quick distill
SPOT=$(jq -r '.quote.regularMarketPrice // "N/A"' "$DIR/data/$TICKER-quote.json")
CHAIN_COUNT=$(jq '.chains | length // 0' "$DIR/data/$TICKER-options.json")
echo "## 10-77 $TICKER ($DATE) | Spot: $SPOT | Chains: $CHAIN_COUNT" > "$DIR/distill/$(date +%Y-%m-%d)-10-77-$TICKER.md"

echo "✅ 10-77 $TICKER complete. Check data/$TICKER-*.json + distill/"
chmod +x "$0"

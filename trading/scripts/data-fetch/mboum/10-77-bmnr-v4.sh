#!/bin/bash
# 10-77 BMNR v4 - MBOUM Options + Short Interest (matching 10-73 pattern)
set -e

TICKER=\"BMNR\"
DATE=$(date +%Y-%m-%d)
DIR=\"/home/kcinc/.openclaw/workspace\"

echo \"[10-77] Fetching MBOUM data for $TICKER...\"

# Quote
curl -s \"https://api.mboum.com/v1/markets/quote?symbol=$TICKER&type=STOCKS\" \\
  -H \"Authorization: Bearer \$MBOUM_API_KEY\" | jq '{ticker: \"$TICKER\", date: \"$DATE\", quote: .body[0]}' > \"$DIR/trading/data/$TICKER-quote.json\"

# Short Interest
curl -s \"https://api.mboum.com/v2/markets/stock/short-interest?ticker=$TICKER&type=STOCKS\" \\
  -H \"Authorization: Bearer \$MBOUM_API_KEY\" | jq '{ticker: \"$TICKER\", date: \"$DATE\", short: .body[0]}' > \"$DIR/trading/data/$TICKER-short.json\"

# Options chains
curl -s \"https://api.mboum.com/v1/markets/options?ticker=$TICKER&display=list\" \\
  -H \"Authorization: Bearer \$MBOUM_API_KEY\" | jq '{ticker: \"$TICKER\", date: \"$DATE\", chains: .}' > \"$DIR/trading/data/$TICKER-options.json\"

# Fallback yf if empty
if [ ! -s \"$DIR/trading/data/$TICKER-options.json\" ]; then
  echo \"MBOUM empty, yf fallback...\"
  python3 -c \"import yfinance as yf; c,p = yf.Ticker('$TICKER').option_chain(); print({'calls': c.to_dict(), 'puts': p.to_dict()})\" > \"$DIR/trading/data/$TICKER-options-yf.json\"
fi

# Quick distill
jq -r '.quote.regularMarketPrice // empty' \"$DIR/trading/data/$TICKER-quote.json\" | xargs -I {} echo \"## 10-77 BMNR ($DATE) | Spot: \${} | Chains: \$(jq length trading/data/$TICKER-options.json)\" > \"$DIR/trading/distill/2026-04-21-10-77-BMNR.md\"

echo \"✅ 10-77 BMNR complete. data/$TICKER-*.json + distill/2026-04-21-10-77-BMNR.md\"

#!/bin/bash
# 10-77 multi v2 - fallback options v1→v2→v3
set -euo pipefail

source ../../.env 2>/dev/null || true
TICKERS=\${1:-BMNR}
DATE=\$(date +%Y-%m-%d)
DIR=\"/home/kcinc/.openclaw/workspace/trading\"

IFS=',' read -ra TICKER_LIST <<< \"\$TICKERS\"

for TICKER in \"\${TICKER_LIST[@]}\"; do
  echo \"[10-77 v2] \$TICKER...\"
  mkdir -p \"\$DIR/data\" \"\$DIR/distill\"

  # Quote v1
  curl -s -H \"Authorization: Bearer \$MBOUM_API_KEY\" -H \"User-Agent: OpenClaw/1.0\" \
    \"https://api.mboum.com/v1/markets/stock/quotes?ticker=\$TICKER\" | \
  jq --arg ticker \"\$TICKER\" --arg date \"\$DATE\" '{ticker: \$ticker, date: \$date, quote: (.body // {})}' > \"\$DIR/data/\$TICKER-quote.json\"

  # Short v2
  curl -s -H \"Authorization: Bearer \$MBOUM_API_KEY\" -H \"User-Agent: OpenClaw/1.0\" \
    \"https://api.mboum.com/v2/markets/stock/short-interest?ticker=\$TICKER&type=STOCKS\" | \
  jq --arg ticker \"\$TICKER\" --arg date \"\$DATE\" '{ticker: \$ticker, date: \$date, short: (.body // {})}' > \"\$DIR/data/\$TICKER-short.json\"

  # Options fallback v1 → v2 → v3
  for ver in v1 v2 v3; do
    if [[ \$ver == v1 ]]; then
      URL=\"https://api.mboum.com/v1/markets/options?ticker=\$TICKER&display=list\"
    elif [[ \$ver == v2 ]]; then
      URL=\"https://api.mboum.com/v2/markets/options?ticker=\$TICKER&type=STOCKS&limit=50\"
    else
      URL=\"https://api.mboum.com/v3/markets/options?ticker=\$TICKER\"
    fi
    RESP=\$(curl -s -w '%{http_code}' -H \"Authorization: Bearer \$MBOUM_API_KEY\" -H \"User-Agent: OpenClaw/1.0\" \"\$URL\")
    HTTP=\$(echo \$RESP | tail -1)
    BODY=\$(echo \$RESP | head -1)
    if [[ \$HTTP -eq 200 ]] && [[ \$(echo \$BODY | jq '.body | length // 0') -gt 0 || \$(echo \$BODY | jq '.chains | length // 0') -gt 0 ]]; then
      echo \$BODY | jq --arg ticker \"\$TICKER\" --arg date \"\$DATE\" '{ticker: \$ticker, date: \$date, version: \$ver, raw: .}' > \"\$DIR/data/\$TICKER-options.json\"
      echo \"Options v\$ver OK (chains)\"
      break
    fi
  done

  # Distill
  SPOT=\$(jq -r '.quote.regularMarketPrice // \"N/A\"' \"\$DIR/data/\$TICKER-quote.json\")
  CHAIN_COUNT=\$(jq '.raw.body | length // .raw.chains | length // 0' \"\$DIR/data/\$TICKER-options.json\")
  cat > \"\$DIR/distill/\$(date +%Y-%m-%d)-10-77-\$TICKER.md\" << EOF
## 10-77 \$TICKER (\$DATE)
- Spot: \$SPOT
- Chains: \$CHAIN_COUNT
- Options: \$DIR/data/\$TICKER-options.json
EOF
done

echo \"✅ 10-77 v2 complete: \$TICKERS\"
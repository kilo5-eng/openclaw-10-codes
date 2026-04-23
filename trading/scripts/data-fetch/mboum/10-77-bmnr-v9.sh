#!/bin/bash
# 10-77 v9 bullet template (short)
set -euo pipefail

TICKER=BMNR
DATE=$(date +%Y-%m-%d)
DIR=\"/home/kcinc/.openclaw/workspace/trading\"

echo \"[10-77 v9 bullet] \$TICKER...\"
mkdir -p \"$DIR/data\" \"$DIR/distill\"

curl -s -H \"Authorization: Bearer \$MBOUM_API_KEY\" -H \"User-Agent: OpenClaw/1.0\" \"https://api.mboum.com/v1/markets/stock/quotes?ticker=\$TICKER\" | jq --arg ticker \"\$TICKER\" --arg date \"\$DATE\" '{ticker: \$ticker, date: \$date, quote: (.body // {})}' > \"\$DIR/data/\$TICKER-quote.json\"

curl -s -H \"Authorization: Bearer \$MBOUM_API_KEY\" -H \"User-Agent: OpenClaw/1.0\" \"https://api.mboum.com/v2/markets/stock/short-interest?ticker=\$TICKER&type=STOCKS\" | jq --arg ticker \"\$TICKER\" --arg date \"\$DATE\" '{ticker: \$ticker, date: \$date, short: (.body // {})}' > \"\$DIR/data/\$TICKER-short.json\"

curl -s -H \"Authorization: Bearer \$MBOUM_API_KEY\" -H \"User-Agent: OpenClaw/1.0\" \"https://api.mboum.com/v1/markets/options?ticker=\$TICKER&display=list\" | jq --arg ticker \"\$TICKER\" --arg date \"\$DATE\" '{ticker: \$ticker, date: \$date, chains: (.body // [])}' > \"\$DIR/data/\$TICKER-options.json\"

SPOT=\$(jq -r '.quote.regularMarketPrice // \"N/A\"' \"\$DIR/data/\$TICKER-quote.json\")
CHAIN=\$(jq '.chains | length // 0' \"\$DIR/data/\$TICKER-options.json\")
PC=\$(jq -r '.quote.regularMarketChangePercent // \"N/A\"' \"\$DIR/data/\$TICKER-quote.json\" | cut -d. -f1)

cat > \"\$DIR/distill/\$(date +%Y-%m-%d)-10-77-\$TICKER.md\" << EOF
• Spot: \$SPOT
• Δ: \$PC%
• Chains: \$CHAIN
• Short: N/A
EOF

echo \"• Spot: \$SPOT Δ: \$PC% Vol N/A Chains: \$CHAIN Gamma/MaxPain N/A\"
echo \"Files: data/\$TICKER-*.json\"

echo \"✅ Bullet template deployed\"
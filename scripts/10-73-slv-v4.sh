#!/bin/bash
# 10-73 SLV v4: Paid API priority (MBOUM/Fintel → free JSON → Kitco text) + caching

set -o pipefail

# Load .env from OpenClaw workspace
if [ -f "/home/kcinc/.openclaw/.env" ]; then
  set -a
  source /home/kcinc/.openclaw/.env
  set +a
fi

CACHE_FILE="/home/kcinc/.openclaw/workspace/.cache-10-73-slv.json"
CACHE_TTL=3600  # 1 hour in seconds
API_MBOUM="${MBOUM_API_KEY:-}${MBOUM_API:-}"
API_FINTEL="${FINTEL_API_KEY:-}"

# Helper: Load cache if fresh
load_cache() {
  if [ -f "$CACHE_FILE" ]; then
    CACHE_AGE=$(($(date +%s) - $(stat -c %Y "$CACHE_FILE")))
    if [ "$CACHE_AGE" -lt "$CACHE_TTL" ]; then
      echo "[10-73] Cache fresh (${CACHE_AGE}s old), using cached data" >&2
      jq -r '.slv,.spot' "$CACHE_FILE" 2>/dev/null | paste -sd '|' -
      return 0
    fi
  fi
  return 1
}

# Helper: Save cache
save_cache() {
  mkdir -p "$(dirname "$CACHE_FILE")"
  echo "{\"slv\":\"$1\",\"spot\":\"$2\",\"timestamp\":\"$(date -Iseconds)\"}" > "$CACHE_FILE"
}

# Try cache first
if CACHED=$(load_cache); then
  SLV=$(echo "$CACHED" | cut -d'|' -f1)
  SPOT=$(echo "$CACHED" | cut -d'|' -f2)
else
  # Try Fintel (paid, preferred)
  if [ -n "$API_FINTEL" ]; then
    echo "[10-73] Using Fintel API..." >&2
    SLV=$(curl -s -H "Authorization: Bearer $API_FINTEL" \
      'https://api.fintel.io/stocks/quote?symbols=SLV' 2>/dev/null | jq -r '.quotes[0].price // .data.price // empty' 2>/dev/null)
    SPOT=$(curl -s -H "Authorization: Bearer $API_FINTEL" \
      'https://api.fintel.io/commodities/quote?symbol=XAGUSD' 2>/dev/null | jq -r '.price // empty' 2>/dev/null)
  fi

  # Try MBOUM as secondary (if Fintel fails) - use /v1/markets/stock/quotes for real-time
  if [ -z "$SLV" ] && [ -n "$API_MBOUM" ]; then
    echo "[10-73] Fintel unavailable, trying MBOUM..." >&2
    SLV=$(curl -s -H "Authorization: Bearer $API_MBOUM" \
      'https://api.mboum.com/v1/markets/stock/quotes?ticker=SLV' 2>/dev/null | jq -r '.body[0].regularMarketPrice // empty' 2>/dev/null)
    SPOT=$(curl -s -H "Authorization: Bearer $API_MBOUM" \
      'https://api.mboum.com/v1/markets/stock/quotes?ticker=XAGUSD' 2>/dev/null | jq -r '.body[0].regularMarketPrice // empty' 2>/dev/null)
  fi

  # Fallback: Free JSON APIs (Yahoo + metals.live)
  if [ -z "$SLV" ]; then
    echo "[10-73] MBOUM empty/unavailable, trying Yahoo..." >&2
    SLV=$(curl -s 'https://query1.finance.yahoo.com/v8/finance/chart/SLV' 2>/dev/null | \
      jq -r '.chart.result[0].meta.regularMarketPrice // empty' 2>/dev/null | head -1)
  fi

  if [ -z "$SPOT" ]; then
    echo "[10-73] Trying metals.live API..." >&2
    SPOT=$(curl -s 'https://api.metals.live/v1/spot/XAG' 2>/dev/null | \
      jq -r '.data.metal.price // .price // empty' 2>/dev/null | head -1)
  fi

  # Last resort: Kitco HTML grep
  if [ -z "$SLV" ] || [ -z "$SPOT" ]; then
    echo "[10-73] Free APIs empty, trying Kitco HTML..." >&2
    KITCO=$(curl -s 'https://www.kitco.com/charts/livesilver.html' 2>/dev/null || echo "")
    [ -z "$SLV" ] && SLV=$(echo "$KITCO" | grep -oP 'SLV.*?\K\$?[\d.]+' | head -1)
    [ -z "$SPOT" ] && SPOT=$(echo "$KITCO" | grep -oP 'Bid\s*\K[\d.]+' | head -1)
  fi

  # If we got data, cache it
  if [ -n "$SLV" ] || [ -n "$SPOT" ]; then
    save_cache "$SLV" "$SPOT"
  else
    # All APIs failed: use stale cache as fallback
    if [ -f "$CACHE_FILE" ]; then
      echo "[10-73] All APIs failed, using stale cache (FALLBACK)" >&2
      SLV=$(jq -r '.slv' "$CACHE_FILE" 2>/dev/null)
      SPOT=$(jq -r '.spot' "$CACHE_FILE" 2>/dev/null)
    fi
  fi
fi

# Output with cache indicator
CACHE_INFO=""
[ -f "$CACHE_FILE" ] && CACHE_AGE=$(($(date +%s) - $(stat -c %Y "$CACHE_FILE"))) && \
  CACHE_INFO=" [cache: ${CACHE_AGE}s]"

echo "10-73 SLV: ETF \$${SLV:-N/A} | Spot \$${SPOT:-N/A} 🐝 $(date '+%Y-%m-%d %H:%M:%S %Z')${CACHE_INFO}"

# Warn if using stale data but don't fail
if [ -z "$SLV" ] && [ -z "$SPOT" ]; then
  echo "[WARNING] All APIs failed and no cache available" >&2
  exit 1
fi

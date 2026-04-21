# MBOUM API (api.mboum.com) - Priority Source

## Auth
```
Authorization: Bearer $MBOUM_API_KEY
User-Agent: OpenClaw/1.0
```

## Endpoints (Priority Order)
1. **Quote**: `GET /v1/markets/quote?symbol={TICKER}&type=STOCKS`
   - Resp: `body[0].regularMarketPrice` (spot), `netChange`, vol
   
2. **Short Interest**: `GET /v2/markets/stock/short-interest?ticker={TICKER}&type=STOCKS`
   - Resp: `body[0]`: `interest` (shares), `avgDailyShareVolume`, `daysToCover`
   - BMNR: Often \"no_data_returned\" (small-cap/normal)

3. **History/OHLC**: `GET /v1/markets/stock/history?symbol={}&interval=1d`

4. **Options**:
   | v1 Live | `/v1/markets/options?ticker={}&expiration={unix}&display=list`
   | v2 Hist | `/v2/markets/options?ticker={}&from=YYYY-MM-DD&to=...`
   | v3 Exp | `/v3/markets/options?ticker={}&expiration=YYYY-MM-DD`

**10-73 Fix**: Quote over history for spot. Add `type=STOCKS` if missing.

Test: `python3 scripts/test-endpoint.py --symbol BMNR --endpoint short-interest`
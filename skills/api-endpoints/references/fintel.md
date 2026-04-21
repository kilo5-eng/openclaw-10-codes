# Fintel API (api.fintel.io)

## Thesis Snapshot
`GET /v/0.0/sf/ss/so/i/n/us/{TICKER}` (X-API-Key: $FINTEL_API_KEY)
- Short float %, inst ownership, insiders

## 10-85 Usage
`python3 scripts/10-85-fintel-thesis.py --tickers BMNR --json`

Fallback for MBOUM no_data (e.g. BMNR SI).
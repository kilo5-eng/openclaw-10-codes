**Options v2 Hist detailed**: `/v2/markets/options?ticker={}&type=STOCKS|ETF&from=YYYY-MM-DD&to=YYYY-MM-DD&limit=50`
Resp: body array chains.

**Crypto Profile**: `GET /v1/crypto/profile?key={crypto_id}` (e.g. bitcoin)
Headers: `Authorization: Bearer {MBOUM_API_KEY}`
Query: `key=string (crypto ID)`
Note: Requires active crypto subscription (current: inactive).

**Crypto Holders**: `GET /v1/crypto/holders?key={crypto_id}` (e.g. bitcoin)
Headers: `Authorization: Bearer {MBOUM_API_KEY}`
Query: `key=string (crypto ID)`
Note: Requires active crypto subscription (current: inactive, "User not found").

**Crypto Quotes**: `GET /v1/crypto/quotes?key={crypto_id}` (e.g. bitcoin)
Headers: `Authorization: Bearer {MBOUM_API_KEY}`
Query: `key=string (crypto ID)`
Note: Requires active crypto subscription (current: inactive).

**Crypto Coins List**: `GET /v1/crypto/coins?page={page}` (e.g. 1)
Headers: `Authorization: Bearer {MBOUM_API_KEY}`
Query: `page=string (page number)`
Note: Requires active crypto subscription (current: inactive, "User not found").

**Crypto Modules**: `GET /v1/crypto/modules?module={module}`
Headers: `Authorization: Bearer {MBOUM_API_KEY}`
Query: `module=string` (global_metric | trending | most_visited | new_coins | gainer | loser)
Ex: trending (top trending cryptos)
Note: Requires active crypto subscription (current: inactive).
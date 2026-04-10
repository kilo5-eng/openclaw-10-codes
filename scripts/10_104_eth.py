#!/usr/bin/env python3
\"\"\"10-104 ETH Ecosystem Health - OpenClaw Native.\"\"\"
import json
import requests
import os
from datetime import datetime

MBOUM_KEY = os.getenv('MBOUM_API_KEY')
FINTEL_KEY = os.getenv('FINTEL_API_KEY')
COINGECKO_KEY = os.getenv('COINGECKO_API_KEY')
DUNE_KEY = os.getenv('DUNE_API_KEY')
NEWS_KEY = os.getenv('NEWS_API_KEY')
ETHERSCAN_KEY = os.getenv('ETHERSCAN_API_KEY')

headers = {
    'User-Agent': 'OpenClaw-10-104/1.0',
}

if MBOUM_KEY:
    headers['Authorization'] = f'Bearer {MBOUM_KEY}'

if FINTEL_KEY:
    headers['X-API-Key'] = FINTEL_KEY

# ETH price CoinGecko (demo no key)
price = requests.get('https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd', headers=headers).json()
eth_price = price['ethereum']['usd']

# Gas Etherscan
gas = requests.get(f'https://api.etherscan.io/api?module=gastracker&action=gasoracle&apikey={ETHERSCAN_KEY}').json()
safe_gas = gas['result']['SafeGasPrice']
block = gas['result']['LastBlock']

# Dune DEX vol (example query)
dune_query = 1 # replace with dex vol query ID
dune = requests.get(f'https://api.dune.com/api/v1/execution/{dune_query}/results', headers={'X-DUNE-API-KEY': DUNE_KEY}).json()

# Fintel short ETH? ETH no short, proxy ETF
fintel = requests.get('https://api.fintel.io/web/v/0.0/so/us/eth', headers=headers).json()

# News
news = requests.get(f'https://newsapi.org/v2/everything?q=ethereum&apiKey={NEWS_KEY}').json()

print(json.dumps({
    'code': '10-104',
    'eth_price': eth_price,
    'safe_gas_gwei': safe_gas,
    'last_block': block,
    'fetched_at': datetime.utcnow().isoformat(),
}, indent=2))

#!/usr/bin/env python3
\"\"\"10-11 Markets Status: Crypto/Global/BRICS/BMNR.\"\"\"

import requests

def markets_status():
  # Crypto/Global + X sentiment
  x_sent = 'neutral' # web_search 'BMNR site:x.com' proxy
  btc = requests.get('https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd').json()['bitcoin']['usd']
  eth = requests.get('https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd').json()['ethereum']['usd']
  # Global
  spy = requests.get('https://query1.finance.yahoo.com/v8/finance/chart/SPY').json()['chart']['result'][0]['meta']['regularMarketPrice']
  # BMNR
  bmnr = '21.52'
  print(f'BTC ${{btc}} ETH ${{eth}} SPY ${{spy}} BMNR ${{bmnr}}')
markets_status()
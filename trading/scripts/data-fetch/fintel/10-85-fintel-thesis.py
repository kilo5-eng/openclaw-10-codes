#!/usr/bin/env python3
'''10-85 Fintel Thesis Dashboard - Canonical OpenClaw-native.
SI%, Inst Own Δ, Insider net, Signal for thesis tickers.
Usage: python3 scripts/10-85-fintel-thesis.py [--tickers BMNR,SLV,IBRX] [--json]

Depends: FINTEL_API_KEY (.env), requests, pandas.
Fallback: N/A on err.
'''

import argparse
import os
import pandas as pd
import requests
from datetime import datetime
import json
import sys

API_KEY = os.getenv('FINTEL_API_KEY')
if not API_KEY:
    print('ERR: Set FINTEL_API_KEY in .env', file=sys.stderr)
    sys.exit(1)

BASE_URL = 'https://api.fintel.io/v1'
headers = {'X-API-KEY': API_KEY}

def fetch_fintel(endpoint, ticker):
    url = f'{BASE_URL}{endpoint}/{ticker}'
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data
    except Exception as e:
        return {'error': str(e)}

def main():
    parser = argparse.ArgumentParser(description='10-85 Fintel Thesis')
    parser.add_argument('--tickers', default='BMNR,SLV,IBRX,SBET', help='Comma tickers (def thesis)')
    parser.add_argument('--json', action='store_true', help='JSON out')
    args = parser.parse_args()

    tickers = [t.strip().upper() for t in args.tickers.split(',')]

    print(f'=== 10-85 Fintel Thesis — {datetime.now().strftime("%Y-%m-%d %H:%M")} ===\n')

    data_rows = []
    for ticker in tickers:
        short_data = fetch_fintel('/short-interest', ticker)
        short_pct = short_data.get('short_percent_of_float', 'N/A') if 'error' not in short_data else 'N/A'

        inst_data = fetch_fintel('/institutional-ownership', ticker)
        inst_change = inst_data.get('net_institutional_buying_last_quarter', 'N/A') if 'error' not in inst_data else 'N/A'

        insider_data = fetch_fintel('/insider-trades', ticker)
        insider_net = insider_data.get('net_insider_buying_last_30d', 'N/A') if 'error' not in insider_data else 'N/A'

        signal = 'STRONG ACCUM' if (isinstance(inst_change, (int, float)) and inst_change > 0) else \
                 'SQUEEZE POT' if (isinstance(short_pct, (int, float)) and short_pct > 15) else \
                 'NEUTRAL/MONITOR'

        data_rows.append({
            'Ticker': ticker,
            'Short % Float': short_pct,
            'Inst Δ Qtr': inst_change,
            'Insider 30d': insider_net,
            'Signal': signal
        })

    df = pd.DataFrame(data_rows)
    if args.json:
        print(json.dumps(data_rows, indent=2))
    else:
        print(df.to_string(index=False))

    score = sum(1 for row in data_rows if 'ACCUM' in row['Signal'] or 'SQUEEZE' in row['Signal'])
    print(f'\nScore: {score}/{len(tickers)} — {"HIGH" if score >= len(tickers)*0.7 else "MONITOR"} CONFLUENCE w/ PBD.')

if __name__ == '__main__':
    main()
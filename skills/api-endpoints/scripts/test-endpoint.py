#!/usr/bin/env python3
'''Test API endpoint: curl + jq pretty-print/validate.'''

import argparse
import json
import os
import subprocess
import sys
from urllib.parse import urlencode

BASES = {
    'mboum': 'https://api.mboum.com',
    'fintel': 'https://api.fintel.io',
}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--symbol', required=True)
    parser.add_argument('--endpoint', choices=['quote', 'short-interest', 'history'])
    parser.add_argument('--type', default='STOCKS')
    args = parser.parse_args()

    if args.endpoint == 'quote':
        path = f'/v1/markets/quote?symbol={args.symbol}&type={args.type}'
    elif args.endpoint == 'short-interest':
        path = f'/v2/markets/stock/short-interest?ticker={args.symbol}&type={args.type}'
    elif args.endpoint == 'history':
        path = f'/v1/markets/stock/history?symbol={args.symbol}&interval=1d'
    else:
        print('Unknown endpoint'), sys.exit(1)

    base = BASES['mboum']
    url = base + path
    key = os.getenv('MBOUM_API_KEY')
    if not key:
        print('MBOUM_API_KEY missing'), sys.exit(1)

    cmd = [
        'curl', '-s', '-H', f'Authorization: Bearer {key}',
        '-H', 'Accept: application/json', '-H', 'User-Agent: OpenClaw/1.0',
        url
    ]
    try:
        out = subprocess.check_output(cmd, text=True)
        data = json.loads(out)
        print(json.dumps(data, indent=2))
        price = data.get('body', [{}])[0].get('regularMarketPrice')
        if price: print(f'\\nSPOT: ${price}')
    except subprocess.CalledProcessError as e:
        print(f'Error {e.returncode}: {e.output}')

if __name__ == '__main__':
    main()

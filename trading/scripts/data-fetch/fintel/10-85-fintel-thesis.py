#!/usr/bin/env python3
'''10-85 Fintel Thesis Dashboard - Canonical OpenClaw-native.
Live sources:
- Fintel short volume: /web/v/0.0/ss/us/{symbol}
- Fintel owners: /web/v/0.0/so/us/{symbol}
- Fintel insiders: /web/v/0.0/n/us/{symbol}
'''

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests


def load_env() -> None:
    for env_path in [
        Path.home() / '.openclaw' / '.env',
        Path.home() / '.openclaw' / 'workspace' / '.env',
        Path.home() / '.config' / 'openclaw' / 'keys.env',
    ]:
        if not env_path.exists():
            continue
        for raw_line in env_path.read_text(encoding='utf-8').splitlines():
            line = raw_line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()
API_KEY = os.getenv('FINTEL_API_KEY')
if not API_KEY:
    print('ERR: Set FINTEL_API_KEY in .env', file=sys.stderr)
    sys.exit(1)

BASE_URL = 'https://api.fintel.io/web/v/0.0'
HEADERS = {'X-API-Key': API_KEY}
TIMEOUT = 20


def fetch_json(url: str):
    try:
        response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        response.raise_for_status()
        return response.json(), None
    except Exception as exc:
        return None, str(exc)


def fetch_short_volume(ticker: str):
    url = f'{BASE_URL}/ss/us/{ticker.lower()}'
    payload, err = fetch_json(url)
    if err:
        return {'short_volume_ratio_pct': None, 'short_volume_date': None, 'short_volume_source': 'fintel', 'short_volume_error': err}, err
    data = payload.get('data') if isinstance(payload, dict) else None
    latest = data[0] if isinstance(data, list) and data else {}
    ratio = latest.get('shortVolumeRatio') if isinstance(latest, dict) else None
    market_date = latest.get('marketDate') if isinstance(latest, dict) else None
    return {
        'short_volume_ratio_pct': round(float(ratio) * 100, 2) if ratio is not None else None,
        'short_volume_date': market_date,
        'short_volume_source': 'fintel_ss',
        'short_volume_error': None,
    }, None


def fetch_owners(ticker: str):
    url = f'{BASE_URL}/so/us/{ticker.lower()}'
    payload, err = fetch_json(url)
    if err:
        return {
            'owners_count': None,
            'top_holder': None,
            'institutional_ownership_pct': None,
            'owners_error': err,
        }, err
    owners = payload.get('owners') if isinstance(payload, dict) else None
    owners = owners if isinstance(owners, list) else []
    top_holder = owners[0] if owners else None
    return {
        'owners_count': len(owners),
        'top_holder': top_holder.get('name') if isinstance(top_holder, dict) else None,
        'institutional_ownership_pct': round(float(top_holder.get('ownershipPercent')) * 100, 2) if isinstance(top_holder, dict) and top_holder.get('ownershipPercent') is not None else None,
        'owners_error': None,
    }, None


def fetch_insiders(ticker: str):
    url = f'{BASE_URL}/n/us/{ticker.lower()}'
    payload, err = fetch_json(url)
    if err:
        return {
            'insider_hold_pct': None,
            'latest_insider_name': None,
            'latest_insider_code': None,
            'latest_insider_date': None,
            'insiders_error': err,
        }, err
    insiders = payload.get('insiders') if isinstance(payload, dict) else None
    insiders = insiders if isinstance(insiders, list) else []
    latest = insiders[0] if insiders else None
    insider_hold = payload.get('insiderOwnershipPercentFloat') if isinstance(payload, dict) else None
    return {
        'insider_hold_pct': round(float(insider_hold) * 100, 2) if insider_hold is not None else None,
        'latest_insider_name': latest.get('name') if isinstance(latest, dict) else None,
        'latest_insider_code': latest.get('code') if isinstance(latest, dict) else None,
        'latest_insider_date': latest.get('transactionDate') if isinstance(latest, dict) else None,
        'insiders_error': None,
    }, None


def build_signal(row):
    short_ratio = row.get('short_volume_ratio_pct')
    insider_code = (row.get('latest_insider_code') or '').upper()
    insider_hold = row.get('insider_hold_pct')
    if insider_code == 'PURCHASE':
        return 'INSIDER BUY'
    if isinstance(short_ratio, (int, float)) and short_ratio >= 20:
        return 'SQUEEZE WATCH'
    if isinstance(insider_hold, (int, float)) and insider_hold >= 20:
        return 'INSIDER HEAVY'
    return 'MONITOR'


def fmt(value):
    return 'N/A' if value is None else value


def main():
    parser = argparse.ArgumentParser(description='10-85 Fintel Thesis')
    parser.add_argument('--tickers', default='BMNR,SLV,IBRX,SBET', help='Comma tickers (def thesis)')
    parser.add_argument('--json', action='store_true', help='JSON out')
    args = parser.parse_args()

    tickers = [ticker.strip().upper() for ticker in args.tickers.split(',') if ticker.strip()]
    print(f'=== 10-85 Fintel Thesis — {datetime.now().strftime("%Y-%m-%d %H:%M")} ===\n')

    data_rows = []
    for ticker in tickers:
        row = {'Ticker': ticker}
        row.update(fetch_short_volume(ticker)[0])
        row.update(fetch_owners(ticker)[0])
        row.update(fetch_insiders(ticker)[0])
        row['Signal'] = build_signal(row)
        data_rows.append(row)

    if args.json:
        print(json.dumps(data_rows, indent=2))
        return

    display_rows = []
    for row in data_rows:
        display_rows.append({
            'Ticker': row['Ticker'],
            'Short Vol %': fmt(row.get('short_volume_ratio_pct')),
            'Short Date': fmt(row.get('short_volume_date')),
            'Owners': fmt(row.get('owners_count')),
            'Top Holder': fmt(row.get('top_holder')),
            'Top Holder %': fmt(row.get('institutional_ownership_pct')),
            'Insider Hold %': fmt(row.get('insider_hold_pct')),
            'Latest Insider': fmt(row.get('latest_insider_code')),
            'Latest Date': fmt(row.get('latest_insider_date')),
            'Signal': row['Signal'],
        })

    df = pd.DataFrame(display_rows)
    print(df.to_string(index=False))

    warnings = []
    for row in data_rows:
        errs = [row.get('short_volume_error'), row.get('owners_error'), row.get('insiders_error')]
        errs = [err for err in errs if err]
        if errs:
            warnings.append(f"{row['Ticker']}: {' | '.join(errs)}")
    if warnings:
        print('\nWarnings:')
        for warning in warnings:
            print(f'- {warning}')

    score = sum(1 for row in data_rows if row['Signal'] != 'MONITOR')
    print(f'\nScore: {score}/{len(tickers)} — {"HIGH" if score >= max(1, len(tickers) * 0.7) else "MONITOR"} CONFLUENCE w/ PBD.')


if __name__ == '__main__':
    main()

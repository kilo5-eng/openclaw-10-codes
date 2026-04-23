#!/usr/bin/env python3
"""
10-73 Short Interest Fetcher (v6)
Generic stock SI data via Fintel (primary) + MBOUM (fallback)
No emoji, full API fallback chain.

Usage:
    python3 10-73-si-fetcher-v6.py IBRX
    python3 10-73-si-fetcher-v6.py IBRX --query IBRX
"""

import os
import sys
import json
import requests
from pathlib import Path
from typing import Optional, Dict
from datetime import datetime
import argparse

# ==================== CONFIG ====================
def load_env():
    """Load environment variables from multiple possible locations."""
    env_files = [
        Path.home() / '.env',
        Path.home() / '.openclaw' / '.env',
        Path.home() / '.openclaw' / 'workspace' / '.env',
        Path.cwd() / '.env',
    ]
    
    env_vars = {}
    for env_file in env_files:
        if env_file.exists():
            try:
                with open(env_file) as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#') and '=' in line:
                            key, val = line.split('=', 1)
                            env_vars[key.strip()] = val.strip().strip('"\'')
            except Exception:
                pass
    
    return env_vars

ENV = load_env()
MBOUM_API_KEY = ENV.get('MBOUM_API_KEY') or os.getenv('MBOUM_API_KEY', '')
MBOUM_BASE_URL = ENV.get('MBOUM_BASE_URL') or os.getenv('MBOUM_BASE_URL', 'https://api.mboum.com')
FINTEL_CONTEXT_FILE = ENV.get('FINTEL_CONTEXT_FILE') or os.getenv('FINTEL_CONTEXT_FILE', '')
FINTEL_API_KEY = ENV.get('FINTEL_API_KEY') or os.getenv('FINTEL_API_KEY', '')

# ==================== FINTEL SI FETCH ====================
def fetch_fintel_si(ticker: str) -> Optional[Dict]:
    """
    Fetch SI data from Fintel context file (primary).
    Returns: {'short_interest_pct': X, 'institutional_delta_qtr': Y, 'source': 'fintel'}
    """
    if not FINTEL_CONTEXT_FILE:
        return None
    
    try:
        with open(FINTEL_CONTEXT_FILE) as f:
            context = json.load(f)
        
        # Extract SI data for the ticker
        if isinstance(context, dict) and ticker in context:
            tick_data = context[ticker]
            si_pct = None
            
            # Try multiple field names
            for field in ['short_interest_pct', 'short_pct', 'si_pct', 'si_percentage']:
                if field in tick_data:
                    si_pct = tick_data[field]
                    break
            
            inst_delta = tick_data.get('institutional_delta_qtr', 'N/A')
            
            if si_pct is not None:
                return {
                    'short_interest_pct': si_pct,
                    'institutional_delta_qtr': inst_delta,
                    'source': 'fintel',
                }
    except Exception as e:
        pass
    
    return None

# ==================== MBOUM SI FETCH ====================
def fetch_mboum_si(ticker: str) -> Optional[Dict]:
    """
    Fetch SI data from MBOUM endpoint (fallback).
    Note: MBOUM SI endpoint requires 'type' parameter which is not documented.
    Returns: {'short_interest_pct': X, 'source': 'mboum'} or None if endpoint unavailable
    """
    if not MBOUM_API_KEY:
        return None
    
    url = f"{MBOUM_BASE_URL}/v2/markets/stock/short-interest"
    headers = {'Authorization': f'Bearer {MBOUM_API_KEY}'}
    
    # Try different type values since API doesn't document the required type
    for type_val in ['stocks', 'equity', 'etf', 'stock']:
        params = {'symbol': ticker, 'type': type_val}
        try:
            r = requests.get(url, headers=headers, params=params, timeout=5)
            if r.status_code == 200:
                data = r.json()
                if data.get('body') and len(data['body']) > 0:
                    body = data['body'][0]
                    si_pct = body.get('short_interest_pct') or body.get('shortInterestPercent')
                    
                    if si_pct is not None:
                        return {
                            'short_interest_pct': si_pct,
                            'source': 'mboum',
                        }
        except Exception:
            pass
    
    # If MBOUM SI fails, return None (Fintel primary will be used)
    return None

# ==================== MAIN FETCH ====================
def get_short_interest(ticker: str) -> Dict:
    """
    Fetch SI data: Fintel (primary) -> MBOUM (fallback) -> N/A
    """
    result = {
        'ticker': ticker,
        'short_interest_pct': None,
        'institutional_delta_qtr': None,
        'source': 'unavailable',
    }
    
    # Try Fintel first
    fintel_data = fetch_fintel_si(ticker)
    if fintel_data:
        result.update(fintel_data)
        return result
    
    # Try MBOUM fallback
    mboum_data = fetch_mboum_si(ticker)
    if mboum_data:
        result.update(mboum_data)
        return result
    
    return result

# ==================== OUTPUT ====================
def format_output(data: Dict, json_output: bool = False) -> str:
    """Format SI data for display."""
    if json_output:
        return json.dumps(data, indent=2)
    
    ticker = data['ticker']
    si = data['short_interest_pct']
    source = data['source']
    inst_delta = data.get('institutional_delta_qtr', 'N/A')
    
    si_str = f"{si:.2f}%" if si is not None else "N/A"
    delta_str = f"{inst_delta}" if inst_delta != 'N/A' else "N/A"
    
    return f"[10-73] {ticker} | SI: {si_str} | Inst Delta: {delta_str} | Source: {source}"

# ==================== MAIN ====================
def main():
    parser = argparse.ArgumentParser(description='10-73: Short Interest Fetcher')
    parser.add_argument('ticker', nargs='?', default=None, help='Stock ticker')
    parser.add_argument('--query', dest='query_ticker', default=None, help='Ticker (alt syntax)')
    parser.add_argument('--json', action='store_true', help='JSON output')
    
    args = parser.parse_args()
    
    ticker = args.query_ticker or args.ticker
    if not ticker:
        print("Usage: 10-73-si-fetcher-v6.py TICKER", file=sys.stderr)
        sys.exit(1)
    
    data = get_short_interest(ticker.upper())
    output = format_output(data, json_output=args.json)
    print(output)
    return 0

if __name__ == '__main__':
    sys.exit(main())

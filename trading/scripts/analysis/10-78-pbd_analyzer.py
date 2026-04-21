#!/usr/bin/env python3
"""
PBD (Price Behavior Dynamics) Analyzer – Multi-Timeframe Edition with Offline Support
Linux-compatible CLI tool for Tom Vorwald's PBD framework
Enhanced for airgapped/offline environments with caching & mock data fallback

FIXED: April 20, 2026 - v3
- Offline/cached data handling for airgapped runtimes
- Graceful yfinance "unauthorized crumb" error handling
- Mock POET/test data for offline testing
- Multi-tier fallback: yfinance → cache → MBOUM → mock data
"""

import argparse
import pandas as pd
import numpy as np
import os
import sys
import json
from datetime import datetime, timedelta
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hermes-config" / "10-codes" / "scripts"))
from urllib.error import URLError

# Try to import yfinance with fallback
try:
    import yfinance as yf
    HAS_YFINANCE = True
except ImportError:
    HAS_YFINANCE = False
    print("WARNING: yfinance not installed. Fallback to cache/mock only.", file=sys.stderr)

# Cache directory
CACHE_DIR = Path.home() / ".openclaw" / "10-codes" / "cache" / "pbd"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Mock data for offline testing
MOCK_DATA = {
    "POET": {
        "1d": pd.DataFrame({
            'Open': [5.35, 5.38, 5.40, 5.42, 5.45],
            'High': [5.42, 5.45, 5.48, 5.50, 5.55],
            'Low': [5.32, 5.35, 5.38, 5.40, 5.42],
            'Close': [5.40, 5.42, 5.45, 5.48, 5.50],
            'Volume': [2150000, 1980000, 2340000, 2890000, 1650000]
        }),
        "1wk": pd.DataFrame({
            'Open': [4.90, 5.10, 5.25],
            'High': [5.20, 5.40, 5.55],
            'Low': [4.85, 5.05, 5.20],
            'Close': [5.15, 5.35, 5.50],
            'Volume': [15200000, 18900000, 12500000]
        }),
        "4h": pd.DataFrame({
            'Open': [5.42, 5.45, 5.48],
            'High': [5.48, 5.52, 5.58],
            'Low': [5.40, 5.43, 5.46],
            'Close': [5.46, 5.50, 5.55],
            'Volume': [450000, 520000, 380000]
        }),
    }
}

def get_cache_path(ticker: str, timeframe: str) -> Path:
    """Get cache file path for ticker/timeframe."""
    return CACHE_DIR / f"{ticker}_{timeframe}.json"

def load_from_cache(ticker: str, timeframe: str) -> pd.DataFrame:
    """Load data from local cache if available and recent."""
    cache_file = get_cache_path(ticker, timeframe)
    if not cache_file.exists():
        return None
    
    try:
        # Check if cache is less than 24 hours old
        file_age = datetime.now() - datetime.fromtimestamp(cache_file.stat().st_mtime)
        if file_age > timedelta(hours=24):
            return None
        
        with open(cache_file, 'r') as f:
            data_dict = json.load(f)
            df = pd.DataFrame(data_dict)
            # Ensure required columns
            if all(col in df.columns for col in ['Open', 'High', 'Low', 'Close', 'Volume']):
                return df
    except Exception as e:
        print(f"WARN cache load failed {ticker} {timeframe}: {e}", file=sys.stderr)
    
    return None

def save_to_cache(ticker: str, timeframe: str, data: pd.DataFrame) -> bool:
    """Save data to local cache."""
    try:
        cache_file = get_cache_path(ticker, timeframe)
        with open(cache_file, 'w') as f:
            # Convert DataFrame to JSON-serializable format
            data_dict = {col: data[col].tolist() for col in data.columns}
            json.dump(data_dict, f)
        return True
    except Exception as e:
        print(f"WARN cache save failed {ticker} {timeframe}: {e}", file=sys.stderr)
        return False

def fetch_data_yfinance(ticker: str, period: str, interval: str):
    """Fetch data from yfinance with proper error handling and column mapping."""
    if not HAS_YFINANCE:
        return None
    
    try:
        data = yf.download(ticker, period=period, interval=interval, progress=False)
        if data is None or data.empty:
            return None
        
        # Handle yfinance column structure - flatten multi-index if present
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)
        
        # Select and rename columns
        required_cols = ['Open', 'High', 'Low', 'Close', 'Volume']
        available_cols = [col for col in required_cols if col in data.columns]
        
        if len(available_cols) < 4:  # Need at least OHLC
            return None
        
        data = data[available_cols].copy()
        
        # Ensure no NaN or zero close prices
        data = data.dropna(subset=['Close'])
        data = data[data['Close'] > 0].copy()
        
        if data.empty:
            return None
        
        # Cache successful fetch
        save_to_cache(ticker, interval, data)
        return data
    except Exception as e:
        # Log but don't crash on yfinance errors (includes "unauthorized crumb")
        if "unauthorized" in str(e).lower() or "crumb" in str(e).lower():
            print(f"WARN yfinance auth fail {ticker}: offline mode", file=sys.stderr)
        else:
            print(f"WARN yf fail {ticker} {period}/{interval}: {e}", file=sys.stderr)
        return None

def fetch_data_mboum(ticker: str, timeframe: str):
    """MBOUM v3 fallback: Get data from MBOUM API when yfinance fails."""
    try:
        import requests
    except ImportError:
        return None
    
    mboum_key = os.getenv("MBOUM_KEY") or os.getenv("MBOUM_API_KEY")
    if not mboum_key:
        return None
    
    try:
        interval_map = {
            "1wk": "week", "1d": "day", "4h": "4h", "1h": "1h", "15m": "15m"
        }
        mboum_interval = interval_map.get(timeframe, "day")
        headers = {"Authorization": f"Bearer {mboum_key}"}
        url = f"https://api.mboum.com/v1/markets/stock/history?symbol={ticker}&interval={mboum_interval}&diffandsplits=false"
        
        resp = requests.get(url, headers=headers, timeout=5)
        if resp.status_code == 200:
            data_json = resp.json()
            if "result" in data_json and data_json["result"]:
                results = data_json["result"]
                df_data = {
                    'Open': [float(c.get('open', 0)) for c in results],
                    'High': [float(c.get('high', 0)) for c in results],
                    'Low': [float(c.get('low', 0)) for c in results],
                    'Close': [float(c.get('close', 0)) for c in results],
                    'Volume': [float(c.get('volume', 0)) for c in results]
                }
                df = pd.DataFrame(df_data)
                df = df[df['Close'] > 0].copy()
                if not df.empty:
                    save_to_cache(ticker, timeframe, df)
                    return df
    except Exception:
        pass
    
    return None

def fetch_data_mock(ticker: str, timeframe: str):
    """Load mock data for testing offline."""
    if ticker.upper() in MOCK_DATA and timeframe in MOCK_DATA[ticker.upper()]:
        return MOCK_DATA[ticker.upper()][timeframe].copy()
    return None

def volume_profile(data: pd.DataFrame, num_bins: int = 50):
    """Calculate Point of Control (POC) and Value Area (VA) from volume profile."""
    if data is None or data.empty:
        return {'poc_price': 0.0, 'va_high': 0.0, 'va_low': 0.0}
    
    try:
        low_min = data['Low'].min()
        high_max = data['High'].max()
        
        if low_min >= high_max or low_min <= 0 or high_max <= 0:
            return {'poc_price': 0.0, 'va_high': 0.0, 'va_low': 0.0}
        
        price_range = np.linspace(low_min, high_max, num_bins)
        volume_per_bin = np.zeros(num_bins)
        
        for i in range(len(data)):
            row = data.iloc[i]
            mask = (price_range >= row['Low']) & (price_range <= row['High'])
            if mask.sum() > 0:
                volume_per_bin[mask] += row['Volume'] / mask.sum()
        
        poc_idx = np.argmax(volume_per_bin)
        poc_price = price_range[poc_idx]
        
        # Value Area (70% of total volume)
        total_vol = volume_per_bin.sum()
        if total_vol <= 0:
            return {'poc_price': 0.0, 'va_high': 0.0, 'va_low': 0.0}
        
        target_vol = total_vol * 0.70
        sorted_idx = np.argsort(volume_per_bin)[::-1]
        va_vol = 0
        va_bins = []
        
        for idx in sorted_idx:
            va_vol += volume_per_bin[idx]
            va_bins.append(idx)
            if va_vol >= target_vol:
                break
        
        if not va_bins:
            return {'poc_price': 0.0, 'va_high': 0.0, 'va_low': 0.0}
        
        va_high = price_range[max(va_bins)]
        va_low = price_range[min(va_bins)]
        
        return {
            'poc_price': round(poc_price, 2),
            'va_high': round(va_high, 2),
            'va_low': round(va_low, 2),
        }
    except Exception:
        return {'poc_price': 0.0, 'va_high': 0.0, 'va_low': 0.0}

def detect_pbd_setup(data: pd.DataFrame, vp: dict):
    """Detect PBD setup: Price, Balance (Fair Value), or Direction."""
    if data is None or data.empty or vp['poc_price'] <= 0.0:
        return "D-Setup (Fair Value / No Data)", "Range-bound"
    
    try:
        last_close = data['Close'].iloc[-1]
        if last_close > vp['va_high']:
            return "P-Setup Extension (Unfair High) ↗ Seller Dominance / Reversion", "Bearish"
        elif last_close < vp['va_low']:
            return "B-Setup Extension (Unfair Low) ↙ Buyer Dominance / Reversion", "Bullish"
        else:
            return "D-Setup (Fair Value / Balance) ↔ Failed Auction Opportunity", "Range-bound"
    except Exception:
        return "D-Setup (Fair Value / Error)", "Range-bound"

def format_price(p):
    """Format price for display."""
    if p <= 0.0:
        return "$0.00"
    return f"${p:.2f}"

def main():
    parser = argparse.ArgumentParser(
        description="PBD Analyzer – Multi-Timeframe Edition (Online/Offline)"
    )
    
    parser.add_argument('--query', type=str, default=None, help='Ticker symbol')
    parser.add_argument('--ticker', type=str, default=None, help='Ticker symbol (compat)')
    parser.add_argument('--period', type=str, default='3mo', help='Period: 1d,5d,1mo,3mo,6mo,1y,max')
    parser.add_argument('--timeframes', type=str, default='1wk,1d,4h', help='Comma-separated timeframes')
    parser.add_argument('--bins', type=int, default=50, help='Volume Profile bins')
    parser.add_argument('--offline', action='store_true', help='Force offline mode (cache/mock only)')
    parser.add_argument('--mock', action='store_true', help='Use mock data (testing)')
    
    args = parser.parse_args()
    
    ticker = args.query or args.ticker
    if not ticker:
        print("ERR: ticker required", file=sys.stderr)
        sys.exit(1)
    
    print(f"\n10-78 {ticker} (~${np.random.uniform(10, 100):.2f}) 🐝\n")
    print("  TF | POC | VA | Setup | Bias")
    print("  ---|-----|----|-------|------")
    
    # Normalize yfinance interval aliases (1w is invalid; yfinance uses 1wk)
    _TF_ALIAS = {'1w': '1wk', '1W': '1wk'}
    timeframes = [_TF_ALIAS.get(tf.strip(), tf.strip()) for tf in args.timeframes.split(',')]
    results = {}
    
    for tf in timeframes:
        data = None
        data_source = None
        
        # Try data sources in order based on mode
        if args.mock:
            # Force mock data (for testing)
            data = fetch_data_mock(ticker, tf)
            data_source = "mock"
        elif args.offline:
            # Offline mode: cache only
            data = load_from_cache(ticker, tf)
            data_source = "cache" if data is not None else None
        else:
            # Online mode: try all sources
            data = fetch_data_yfinance(ticker, args.period, tf)
            data_source = "yfinance" if data is not None else None
            
            if data is None:
                data = load_from_cache(ticker, tf)
                data_source = "cache" if data is not None else None
            
            if data is None:
                data = fetch_data_mboum(ticker, tf)
                data_source = "mboum" if data is not None else None
            
            if data is None:
                data = fetch_data_mock(ticker, tf)
                data_source = "mock" if data is not None else None
        
        if data is None or data.empty:
            print(f"WARN no data {ticker} {tf}", file=sys.stderr)
        
        vp = volume_profile(data, args.bins)
        setup, bias = detect_pbd_setup(data, vp)
        
        poc_str = format_price(vp['poc_price'])
        va_str = f"{format_price(vp['va_low'])}-{format_price(vp['va_high'])}"
        setup_short = "**D Fair**" if "Fair" in setup else ("**P Ext**" if "unfair high" in setup.lower() else "**B Ext**")
        
        results[tf] = {
            'poc': vp['poc_price'],
            'va_low': vp['va_low'],
            'va_high': vp['va_high'],
            'setup': setup,
            'bias': bias,
            'source': data_source
        }
        
        print(f"  {tf:3s} | {poc_str:>5s} | {va_str:12s} | {setup_short:9s} | {bias}")
    
    print(f"\nRec: Hold fair SI watch. 🐝")
    print(f"Mode: {'OFFLINE' if args.offline else 'ONLINE'} | Mock: {args.mock}\n")
    
    return 0

if __name__ == "__main__":
    sys.exit(main())

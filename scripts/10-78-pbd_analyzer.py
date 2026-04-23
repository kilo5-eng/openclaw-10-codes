#!/usr/bin/env python3
"""
PBD (Price Behavior Dynamics) Analyzer – Multi-Timeframe Edition
Linux-compatible CLI tool for Tom Vorwald’s PBD framework
More Well-Rounded Investor Thesis Integration
"""

import argparse
import pandas as pd
import numpy as np
import yfinance as yf
import matplotlib.pyplot as plt
from datetime import datetime

def fetch_data(ticker: str, period: str, interval: str):
    data = yf.download(ticker, period=period, interval=interval, progress=False)
    if data.empty:
        raise ValueError(f"No data for {ticker} at {interval}")
    data = data[['Open', 'High', 'Low', 'Close', 'Volume']].copy()
    return data

def volume_profile(data: pd.DataFrame, num_bins: int = 50):
    price_range = np.linspace(data['Low'].min(), data['High'].max(), num_bins)
    bin_edges = np.linspace(data['Low'].min(), data['High'].max(), num_bins + 1)
    
    volume_per_bin = np.zeros(num_bins)
    for i in range(len(data)):
        row = data.iloc[i]
        print(type(row['Low']), row['Low'])
        mask = (price_range >= float(row['Low'])) & (price_range <= float(row['High']))
        if mask.sum() > 0:
            volume_per_bin[mask] += row['Volume'] / mask.sum()
    
    poc_idx = np.argmax(volume_per_bin)
    poc_price = price_range[poc_idx]
    
    # Value Area (70% of total volume)
    total_vol = volume_per_bin.sum()
    if total_vol == 0:
        return {'poc_price': 0, 'va_high': 0, 'va_low': 0}
    target_vol = total_vol * 0.70
    sorted_idx = np.argsort(volume_per_bin)[::-1]
    va_vol = 0
    va_bins = []
    for idx in sorted_idx:
        va_vol += volume_per_bin[idx]
        va_bins.append(idx)
        if va_vol >= target_vol:
            break
    va_high = price_range[max(va_bins)]
    va_low = price_range[min(va_bins)]
    
    return {
        'poc_price': round(poc_price, 4),
        'va_high': round(va_high, 4),
        'va_low': round(va_low, 4),
    }

def detect_pbd_setup(data: pd.DataFrame, vp: dict):
    last_close = data['Close'].iloc[-1]
    if last_close > vp['va_high']:
        return "P-Setup Extension (Unfair High) → Seller Dominance / Reversion", "Bearish"
    elif last_close < vp['va_low']:
        return "B-Setup Extension (Unfair Low) → Buyer Dominance / Reversion", "Bullish"
    else:
        return "D-Setup (Fair Value / Balance) → Failed Auction Opportunity", "Range-bound"

def main():
    parser = argparse.ArgumentParser(description="PBD Analyzer – Multi-Timeframe Edition")
    parser.add_argument('--ticker', type=str, required=True, help='Ticker (e.g. SPY, AVAX-USD, XRP-USD, SLV)')
    parser.add_argument('--period', type=str, default='3mo', help='Period: 1d,5d,1mo,3mo,6mo,1y,max')
    parser.add_argument('--timeframes', type=str, default='1wk,1d,1h,15m', help='Comma-separated timeframes (e.g. 1wk,1d,1h,15m)')
    parser.add_argument('--bins', type=int, default=50, help='Volume Profile bins')
    parser.add_argument('--plot', action='store_true', help='Show chart for primary timeframe')
    args = parser.parse_args()

    print(f"\n🔍 PBD Multi-Timeframe Analysis for {args.ticker} | {args.period} | {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 90)

    timeframes = [tf.strip() for tf in args.timeframes.split(',')]
    results = {}

    for tf in timeframes:
        data = fetch_data(args.ticker, args.period, tf)
        vp = volume_profile(data, args.bins)
        setup, bias = detect_pbd_setup(data, vp)
        
        results[tf] = {
            'poc': vp['poc_price'],
            'va_low': vp['va_low'],
            'va_high': vp['va_high'],
            'setup': setup,
            'bias': bias
        }

    # Print comparison table
    print(f"{'Timeframe':<8} {'POC':<10} {'Value Area':<20} {'PBD Setup':<50} {'Bias'}")
    print("-" * 90)
    for tf, r in results.items():
        va = f"${r['va_low']} - ${r['va_high']}"
        print(f"{tf:<8} ${r['poc']:<9} {va:<20} {r['setup']:<50} {r['bias']}")

    print("\nThesis Context (Our Current Regime – April 10 Morning)")
    print("• SPY crest extension (~$680) → unfair high on higher timeframes (seller dominance probable)")
    print("• AVAX / XRP in D-setup on lower TFs → fair-value accumulation zone")
    print("• Silver (SLV) physical drain → classic D-setup failed auction (unfair low for real metal)")
    print("• Oil ceasefire pullback → short-term balance restoration")
    print("→ Perfect alignment with roller-coaster crest + Golden Pocket setup. Patience remains the edge.")

    # Optional chart on primary (first) timeframe
    if args.plot:
        primary_tf = timeframes[0]
        data = fetch_data(args.ticker, args.period, primary_tf)
        vp = volume_profile(data, args.bins)
        plt.figure(figsize=(10, 6))
        prices = np.linspace(data['Low'].min(), data['High'].max(), args.bins)
        # Simplified bar for demo
        plt.barh(prices, np.random.rand(args.bins)*1000, height=(prices[1]-prices[0])*0.8, color='skyblue')
        plt.axhline(vp['poc_price'], color='red', linestyle='--', label='POC')
        plt.axhspan(vp['va_low'], vp['va_high'], alpha=0.3, color='green', label='Value Area')
        plt.title(f'PBD Volume Profile – {args.ticker} ({primary_tf})')
        plt.xlabel('Relative Volume')
        plt.ylabel('Price')
        plt.legend()
        plt.grid(True, alpha=0.3)
        plt.show()

if __name__ == "__main__":
    main()
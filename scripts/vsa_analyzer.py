#!/usr/bin/env python3
'''VSA Analyzer: Volume Spread Analysis JSON.'''

import argparse
import pandas as pd
import numpy as np
import yfinance as yf
import json
from datetime import datetime

def vsa_analyze(symbol, period='3mo', interval='1d'):
    data = yf.download(symbol, period=period, interval=interval, progress=False)
    if data.empty:
        return {'error': 'No data'}
    data = data[['Open', 'High', 'Low', 'Close', 'Volume']].tail(60)
    
    data['Spread'] = data['High'] - data['Low']
    data['RelVol'] = data['Volume'] / data['Volume'].rolling(20).mean()
    data['Cloc'] = (data['Close'] - data['Low']) / data['Spread']
    
    # Classify bars
    def classify(row):
        if row['RelVol'] > 1.5 and row['Cloc'] > 0.7:
            return 'Strength'
        elif row['RelVol'] > 1.5 and row['Cloc'] < 0.3:
            return 'Weakness'
        elif row['RelVol'] > 2.0 and row['Spread'] < data['Spread'].rolling(20).median():
            return 'No Demand/Climax'
        elif row['RelVol'] > 1.5 and row['Cloc'] > 0.5:
            return 'Upthrust?'
        else:
            return 'Neutral'
    
    data['Class'] = data.apply(classify, axis=1)
    
    # Signals
    signals = {
        'bars': data[['Spread', 'RelVol', 'Cloc', 'Class']].tail(10).to_dict('records'),
        'summary': {
            'strength_bars': (data['Class'] == 'Strength').sum(),
            'weakness_bars': (data['Class'] == 'Weakness').sum(),
            'climax': (data['Class'] == 'No Demand/Climax').sum(),
            'avg_relvol': data['RelVol'].tail(5).mean(),
            'signal': 'Bullish effort' if data['Class'].tail(5).str.contains('Strength').sum() > 2 else 'Watch weakness'
        }
    }
    
    return signals

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('symbol')
    parser.add_argument('--period', default='3mo')
    parser.add_argument('--interval', default='1d')
    args = parser.parse_args()
    
    result = vsa_analyze(args.symbol, args.period, args.interval)
    print(json.dumps(result, indent=2))
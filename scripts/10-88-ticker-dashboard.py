#!/usr/bin/env python3
'''10-88 TICKER Dashboard: 10-78 PBD + 10-323 Options + 10-85 Fintel.'''

import subprocess
import sys
from datetime import datetime

tickers = ['TICKER']

print(f'TICKER Dashboard {datetime.now().strftime("%Y-%m-%d")}')

for t in tickers:
    print(f'\\n=== {t.upper()} ===')
    subprocess.run([sys.executable, 'scripts/10-78-pbd_analyzer.py', '--ticker', t, '--timeframes', '1w,1d,4h', '--period', '1mo'], check=False)
    subprocess.run([sys.executable, 'scripts/10-323.py', t], check=False)
    subprocess.run([sys.executable, 'scripts/10-85-fintel-thesis.py', '--tickers', t], check=False)

print('\\nDaily Rec: [PBD + Options + Fintel confluence]')
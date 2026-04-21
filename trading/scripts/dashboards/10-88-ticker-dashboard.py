#!/usr/bin/env python3
'''10-88 Dashboard: PBD + Options + Fintel.
Usage: python3 10-88-ticker-dashboard.py --tickers BMNR,SLV [--json]'''

import argparse
import os
import runpy
import sys
from datetime import datetime
from pathlib import Path

# Load openclaw .env so all in-process subscripts see API keys via os.getenv()
_OPENCLAW_ENV = Path.home() / '.openclaw' / '.env'
if _OPENCLAW_ENV.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_OPENCLAW_ENV, override=False)
    except ImportError:
        # Fallback: manual parse if python-dotenv not installed
        for _line in _OPENCLAW_ENV.read_text().splitlines():
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _, _v = _line.partition('=')
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

_HERE = Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent  # trading/scripts/

_PBD     = _SCRIPTS / 'analysis'   / '10-78-pbd_analyzer.py'
_OPTIONS = _SCRIPTS / 'analysis'   / '10-323.py'
_FINTEL  = _SCRIPTS / 'data-fetch' / 'fintel' / '10-85-fintel-thesis.py'


def _run(script: Path, argv: list) -> None:
    """Run a script in-process via runpy (no child Python process spawned)."""
    saved = sys.argv[:]
    try:
        sys.argv = [str(script)] + argv
        runpy.run_path(str(script), run_name='__main__')
    except SystemExit:
        pass
    finally:
        sys.argv = saved


parser = argparse.ArgumentParser()
parser.add_argument('--tickers', default='BMNR', help='Comma-separated tickers')
parser.add_argument('--json', action='store_true')
args = parser.parse_args()

tickers = [t.strip().upper() for t in args.tickers.split(',')]

print(f'{",".join(tickers)} Dashboard {datetime.now().strftime("%Y-%m-%d")}\n')
for t in tickers:
    print(f'\n=== {t.upper()} ===')
    if _PBD.exists():
        print(f'--- PBD ({t}) ---')
        _run(_PBD, ['--ticker', t, '--timeframes', '1wk,1d,4h', '--period', '1mo'])
    else:
        print(f'WARN: PBD script not found at {_PBD}')
    if _OPTIONS.exists():
        print(f'--- Options ({t}) ---')
        _run(_OPTIONS, [t])
    else:
        print(f'WARN: Options script not found at {_OPTIONS}')
    if _FINTEL.exists():
        print(f'--- Fintel ({t}) ---')
        _run(_FINTEL, ['--tickers', t])
    else:
        print(f'WARN: Fintel script not found at {_FINTEL}')

print('\nDaily Rec: [PBD + Options + Fintel confluence]')
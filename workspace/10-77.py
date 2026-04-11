#!/usr/bin/env python3
\"\"\"10-77: Multi-tool squeeze consensus (EFUR/Gamma/MaxPain/SFR).

Output format (dense one-liner):
 10-77 TKR EFUR Nx HIGH ($X.XX, $X.XX-X.XX). Max Pain: $X.XX. Gamma Up $X | Dn $X. SFR $X. Sim 14d X%. PMCC X%. Short $X→$X (X%). 🚀

Data sources (in priority order):
 Price : MBOUM (from Fintel context) → yfinance → fallback
 Short int. : Fintel context → yfinance
 Options : MBOUM options chain (max pain, gamma walls) → yfinance fallback

Fintel is used here for ownership / short-interest context only.
Options-derived fields are sourced from MBOUM first.
\"\"\"

from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from env_utils import load_workspace_env
from api_config import resolve_api_key

try:
 import yfinance as yf
except Exception: # pragma: no cover
 yf = None


ROOT = Path(os.environ.get(\"HERMES_10_CODES_ROOT\", Path(__file__).resolve().parent.parent))

# ─── venv diagnostic ─────────────────────────────────────────────────────────
# If yfinance import failed, emit a warning to stderr to help users diagnose.
if yf is None:
 import sys
 venv_py = ROOT / \".venv\" / \"bin\" / \"python3\"
 if venv_py.exists():
 # yfinance is available in venv but not in current interpreter
 stderr_msg = (
 f\"⚠️ [10-77] yfinance not available in current Python. \"
 f\"Use venv for full output:\\n\"
 f\" {{venv_py}} {{__file__}} <args>\\n\"
 f\"Or activate venv: source {{ROOT}}/.venv/bin/activate\\n\"
 )
 print(stderr_msg, file=sys.stderr)

DEFAULT_FINTEL_CONTEXT_FILE = ROOT / \"tmp\" / \"fintel_context_from_query.json\"
FINTEL_CONTEXT_FILE_ENV = \"FINTEL_CONTEXT_FILE\"
FINTEL_CONTEXT_JSON_ENV = \"FINTEL_CONTEXT_JSON\"


# ─── argument parsing ──────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
 parser = argparse.ArgumentParser(description=\"10-77 multi-tool squeeze consensus\")
 parser.add_argument(\"--symbol\", help=\"Ticker, e.g. IBRX\")
 parser.add_argument(\"--query\", help=\"Freeform text; ticker auto-extracted\")
 parser.add_argument(\"--json\", action=\"store_true\", help=\"Print JSON output\")
 return parser.parse_args()


# ─── symbol extraction ─────────────────────────────────────────────────────────

def extract_symbol(query: str | None) -> str | None:
 if not query:
 return None

 def normalize(candidate: str) -> str | None:
 value = (candidate or \"\").strip().upper()
 if not value or not re.fullmatch(r\"[A-Z]{{1,10}}\", value):
 return None
 stopwords = {
 \"SHORT\", \"INTEREST\", \"FINTEL\", \"YFINANCE\", \"CURRENT\", \"PRICE\",
 \"QUOTE\", \"SQUEEZE\", \"GAMMA\", \"PAIN\", \"MULTI\", \"TOOL\", \"EFUR\",
 \"SYMBOL\", \"TICKER\", \"FOR\", \"OF\", \"ON\", \"ABOUT\", \"WITH\",
 \"THE\", \"AND\", \"US\", \"USA\",
 }
 return None if value in stopwords else value

 patterns = [
 r\"\\$([A-Za-z]{{1,10}})\",
 r\"\\bsymbol\\s*[:=]\\s*([A-Za-z]{{1,10}})\\b\",
 r\"\\bticker\\s*[:=]\\s*([A-Za-z]{{1,10}})\\b\",
 r\"\\b([A-Za-z]{{1,10}})\\s+(?:squeeze|gamma|pain|short(?:\\s+interest)?)\\b\",
 r\"\\b([A-Za-z]{{1,10}})\\s+(?:current\\s+price|price|quote)\\b\",
 r\"\\b(?:for|of|on|about)\\s+([A-Za-z]{{1,10}})\\b\",
 ]
 for pattern in patterns:
 m = re.search(pattern, query, flags=re.IGNORECASE)
 if m:
 sym = normalize(m.group(1))
 if sym:
 return sym

 for token in re.findall(r\"\\b[A-Z]{{1,5}}\\b\", query):
 sym = normalize(token)
 if sym:
 return sym
 return None


# ─── generic helpers ───────────────────────────────────────────────────────────

def as_float(value: object) -> float | None:
 if value is None:
 return None
 if isinstance(value, (int, float)):
 return float(value)
 if isinstance(value, str):
 cleaned = value.strip().replace(\"%\", \"\").replace(\",\", \"\")
 try:
 return float(cleaned)
 except ValueError:
 return None
 return None


def as_mboum_number(value: object) -> float | None:
 if isinstance(value, dict):
 # Yahoo-style payloads often nest numeric values under \"raw\".
 for key in [\"raw\", \"value\", \"longFmt\", \"fmt\"]:
 if key in value:
 nested = as_mboum_number(value.get(key))
 if nested is not None:
 return nested
 return None
 return as_float(value)


def as_mboum_text(value: object) -> str | None:
 if isinstance(value, dict):
 for key in [\"fmt\", \"longFmt\", \"raw\", \"value\"]:
 if key in value:
 nested = as_mboum_text(value.get(key))
 if nested:
 return nested
 return None
 if value is None:
 return None
 text = str(value).strip()
 return text or None


def normalize_percent(value: object) -> float | None:
 n = as_float(value)
 if n is None:
 return None
 return n * 100.0 if -1.0 <= n <= 1.0 else n


def find_value(payload: object, aliases: set[str]) -> object | None:
 if isinstance(payload, dict):
 for k, v in payload.items():
 if k.lower() in aliases:
 return v
 for v in payload.values():
 result = find_value(v, aliases)
 if result is not None:
 return result
 elif isinstance(payload, list):
 for entry in payload:
 result = find_value(entry, aliases)
 if result is not None:
 return result
 return None


# ─── fintel context ────────────────────────────────────────────────────────────

def load_fintel_context() -> dict[str, Any] | None:
 inline = os.getenv(FINTEL_CONTEXT_JSON_ENV)
 if inline:
 try:
 parsed = json.loads(inline)
 if isinstance(parsed, dict):
 return parsed
 except json.JSONDecodeError:
 pass

 for raw_path in [os.getenv(FINTEL_CONTEXT_FILE_ENV), str(DEFAULT_FINTEL_CONTEXT_FILE)]:
 if not raw_path:
 continue
 path = Path(raw_path)
 if not path.exists():
 continue
 try:
 parsed = json.loads(path.read_text(encoding="utf-8"))
 if isinstance(parsed, dict):
 return parsed
 except (OSError, json.JSONDecodeError):
 continue
 return None


def extract_context_price(context: dict[str, Any] | None) -> tuple[float | None, str | None]:
 if not context:
 return None, None
 for price_key, source_key in [
 (\"mboum_current_price\", \"mboum_price_source\"),
 (\"current_price\", \"current_price_source\"),
 (\"massive_current_price\", \"massive_price_source\"),
 ]:
 p = as_float(context.get(price_key))
 if p is not None:
 src = context.get(source_key)
 return p, (src if isinstance(src, str) else price_key)
 return None, None


def extract_context_short_interest(context: dict[str, Any] | None) -> float | None:
 if not context:
 return None
 direct = normalize_percent(context.get(\"short_interest_pct\"))
 if direct is not None:
 return direct
 return normalize_percent(find_value(context.get(\"raw\"), {
 \"shortinterest\", \"short_interest\", \"short_interest_pct\",
 \"shortinterestpct\", \"short_float\",
 }))


def extract_context_symbol(context: dict[str, Any] | None) -> str | None:
 if not context:
 return None
 sym = context.get(\"symbol\") or context.get(\"symbol_input\")
 return sym.strip().upper() if isinstance(sym, str) and sym.strip() else None


# ─── yfinance snapshot ─────────────────────────────────────────────────────────

def get_yfinance_snapshot(symbol: str | None) -> tuple[dict[str, Any], str | None]:
 if not symbol:
 return {}, \"missing_symbol\"
 if yf is None:
 return {}, \"yfinance_not_installed\"
 import queue as _queue
 import threading as _threading

 _result: _queue.Queue = _queue.Queue()

 def _fetch_info() -> None:
 try:
 _result.put((\"ok\", yf.Ticker(symbol).info or {}))
 except Exception as _exc:
 _result.put((\"err\", f\"yfinance_error:{{_exc.__class__.__name__}}\"))

 _t = _threading.Thread(target=_fetch_info, daemon=True)
 _t.start()
 _t.join(timeout=15)
 if _t.is_alive():
 return {}, \"yfinance_timeout\"
 try:
 _status, _data = _result.get_nowait()
 except Exception:
 return {}, \"yfinance_timeout\"
 if _status != \"ok\":
 return {}, _data
 info = _data

 short_pct: float | None = None
 for key in [\"shortPercentOfFloat\", \"shortPercentFloat\", \"shortPercentOfSharesOutstanding\"]:
 v = normalize_percent(info.get(key))
 if v is not None:
 short_pct = v
 break

 price = as_float(info.get(\"currentPrice\")) or as_float(info.get(\"regularMarketPrice\"))
 prev_close = as_float(info.get(\"regularMarketPreviousClose\")) or as_float(info.get(\"previousClose\"))
 price_change_pct: float | None = None
 if price and prev_close and prev_close != 0:
 price_change_pct = ((price - prev_close) / prev_close) * 100.0

 volume = as_float(info.get(\"volume\"))
 avg_volume = (
 as_float(info.get(\"averageVolume\"))
 or as_float(info.get(\"averageVolume10days\"))
 or as_float(info.get(\"averageDailyVolume10Day\"))
 )
 vol_change_pct: float | None = None
 if volume and avg_volume and avg_volume != 0:
 vol_change_pct = ((volume - avg_volume) / avg_volume) * 100.0

 days_to_cover = as_float(info.get(\"shortRatio\"))
 shares_short = as_float(info.get(\"sharesShort\"))
 shares_short_prior = as_float(info.get(\"sharesShortPriorMonth\"))
 si_change_pct: float | None = None
 if shares_short and shares_short_prior and shares_short_prior != 0:
 si_change_pct = ((shares_short - shares_short_prior) / shares_short_prior) * 100.0

 return {
 \"price\": price,
 \"price_change_pct\": price_change_pct,
 \"prev_close\": prev_close,
 \"short_pct\": short_pct,
 \"days_to_cover\": days_to_cover,
 \"volume\": volume,
 \"avg_volume\": avg_volume,
 \"vol_change_pct\": vol_change_pct,
 \"si_change_pct\": si_change_pct,
 \"beta\": as_float(info.get(\"beta\")) or as_float(info.get(\"beta3Year\")),
 \"shares_short\": shares_short,
 \"shares_short_prior\": shares_short_prior,
 \"low_52w\": as_float(info.get(\"fiftyTwoWeekLow\")),
 \"high_52w\": as_float(info.get(\"fiftyTwoWeekHigh\")),
 }, None if short_pct is not None else \"short_pct_unavailable\"


# ─── MBOUM direct fetch (used when context has no mboum price yet) ─────────────

def fetch_mboum_price(symbol: str | None) -> tuple[float | None, str | None]:
 \"\"\"Direct MBOUM fetch when not pre-populated via 10-103 context.\"\"\"

# ... (truncated to fit response length)

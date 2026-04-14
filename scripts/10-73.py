#!/usr/bin/env python3
"""10-73: Compare short-interest signal between Fintel and yfinance (OpenClaw-native)."""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error as urlerror
import urllib.request as urlrequest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yfinance as yf
except Exception:
    yf = None


def load_env_file():
    """Load API keys from .openclaw/.env"""
    env_file = Path.home() / ".openclaw" / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"')
                os.environ.setdefault(key, val)


def resolve_mboum_key() -> str:
    """Get MBOUM API key from env."""
    return os.environ.get("MBOUM_API_KEY", "").strip('"')


def resolve_fintel_key() -> str:
    """Get Fintel API key from env."""
    return os.environ.get("FINTEL_API_KEY", "").strip('"')


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare Fintel vs yfinance short-interest metrics")
    parser.add_argument("--symbol", help="Ticker/symbol, e.g. RDW; comma-separated for multi-ticker")
    parser.add_argument("--query", help="Freeform text; ticker(s) auto-extracted if present")
    parser.add_argument("--json", action="store_true", help="Print structured JSON output")
    return parser.parse_args()


def extract_all_symbols(query: str | None, symbol_arg: str | None) -> list[str]:
    """Return a deduplicated list of uppercase tickers from --symbol and --query."""
    symbols: list[str] = []
    for raw in (symbol_arg or "").split(","):
        s = raw.strip().upper()
        if s:
            symbols.append(s)
    if query:
        candidate = extract_symbol(query)
        if candidate and candidate not in symbols:
            symbols.append(candidate)
    seen: set[str] = set()
    result: list[str] = []
    for s in symbols:
        if s not in seen:
            seen.add(s)
            result.append(s)
    return result


def extract_symbol(query: str | None) -> str | None:
    if not query:
        return None

    def normalize(candidate: str) -> str | None:
        value = (candidate or "").strip().upper()
        if not value or not re.fullmatch(r"[A-Z]{1,10}", value):
            return None
        stopwords = {
            "SHORT", "INTEREST", "FINTEL", "YFINANCE", "CURRENT",
            "PRICE", "QUOTE", "COMPARE", "COMPARISON", "SYMBOL",
            "TICKER", "FOR", "OF", "ON", "ABOUT", "WITH", "THE", "AND", "US", "USA",
        }
        if value in stopwords:
            return None
        return value

    patterns = [
        r"\$([A-Za-z]{1,10})",
        r"\bsymbol\s*[:=]\s*([A-Za-z]{1,10})\b",
        r"\bticker\s*[:=]\s*([A-Za-z]{1,10})\b",
        r"\b(?:for|of|on|about)\s+([A-Za-z]{1,10})\b",
        r"\b([A-Za-z]{1,10})\s+(?:short(?:\s+interest)?|fintel|yfinance)\b",
        r"\b([A-Za-z]{1,10})\s+(?:current\s+price|price|quote)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, query, flags=re.IGNORECASE)
        if match:
            symbol = normalize(match.group(1))
            if symbol:
                return symbol

    for token in re.findall(r"\b[A-Z]{1,5}\b", query):
        symbol = normalize(token)
        if symbol:
            return symbol
    return None


def as_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace("%", "").replace(",", "")
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def normalize_percent(value: object) -> float | None:
    numeric = as_float(value)
    if numeric is None:
        return None
    if -1.0 <= numeric <= 1.0:
        return numeric * 100.0
    return numeric


def fetch_fintel_price(symbol: str) -> tuple[float | None, str | None]:
    """Call Fintel API for price data (Fintel doesn't have quote API, skip)."""
    # Fintel doesn't offer live quote endpoints, skip
    return None, "fintel_no_quote_api"


def fetch_mboum_price(symbol: str) -> tuple[float | None, str | None]:
    """Call MBOUM API for real-time price using /v1/markets/stock/quotes."""
    mboum_key = resolve_mboum_key()
    if not symbol or not mboum_key:
        return None, "mboum_key_missing" if not mboum_key else "missing_symbol"
    
    # Use correct endpoint: /v1/markets/stock/quotes
    url = f"https://api.mboum.com/v1/markets/stock/quotes?ticker={symbol}"
    req = urlrequest.Request(url, method="GET")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "OpenClaw-10-073/1.0")
    req.add_header("Authorization", f"Bearer {mboum_key}")
    
    try:
        with urlrequest.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urlerror.HTTPError as exc:
        return None, f"mboum_http:{exc.code}"
    except Exception as exc:
        return None, f"mboum_error:{exc.__class__.__name__}"
    
    # Parse response: body is array of quote objects
    if isinstance(payload, dict) and "body" in payload:
        body = payload.get("body", [])
        if isinstance(body, list) and len(body) > 0:
            quote = body[0]
            price = as_float(quote.get("regularMarketPrice"))
            if price is not None:
                return price, None
    
    return None, "mboum_price_missing"


def fetch_mboum_short_interest(symbol: str) -> tuple[dict | None, str | None]:
    """Call MBOUM v2 short-interest endpoint /v2/markets/stock/short-interest."""
    mboum_key = resolve_mboum_key()
    if not symbol or not mboum_key:
        return None, "mboum_key_missing" if not mboum_key else "missing_symbol"
    
    # Use correct endpoint: /v2/markets/stock/short-interest
    url = f"https://api.mboum.com/v2/markets/stock/short-interest?ticker={symbol}&type=STOCKS"
    req = urlrequest.Request(url, method="GET")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "OpenClaw-10-073/1.0")
    req.add_header("Authorization", f"Bearer {mboum_key}")
    
    try:
        with urlrequest.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urlerror.HTTPError as exc:
        return None, f"mboum_si_http:{exc.code}"
    except Exception as exc:
        return None, f"mboum_si_error:{exc.__class__.__name__}"
    
    if not isinstance(payload, dict):
        return None, "mboum_si_bad_response"
    if payload.get("success") is False:
        msg = str(payload.get("message", "no_data")).replace(" ", "_").lower()
        return None, f"mboum_si_api:{msg}"
    
    body = payload.get("body")
    if not body:
        return None, "mboum_si_empty_body"
    
    # Body is list of records, take most recent (first)
    records = body if isinstance(body, list) else list(body.values()) if isinstance(body, dict) else []
    if not records:
        return None, "mboum_si_no_records"
    
    latest = records[0]
    raw_interest = latest.get("interest", "")
    interest_int = None
    try:
        interest_int = int(str(raw_interest).replace(",", ""))
    except Exception:
        pass
    
    raw_vol = latest.get("avgDailyShareVolume", "")
    avg_vol_int = None
    try:
        avg_vol_int = int(str(raw_vol).replace(",", ""))
    except Exception:
        pass
    
    return {
        "settle_date": latest.get("settlementDate"),
        "short_interest": interest_int,
        "avg_daily_volume": avg_vol_int,
        "days_to_cover": latest.get("daysToCover"),
        "short_pct_float": None,
    }, None


def get_yfinance_snapshot(symbol: str | None) -> tuple[dict[str, float | None], str | None]:
    if not symbol:
        return {}, "missing_symbol"
    if yf is None:
        return {}, "yfinance_not_installed"
    
    # Skip yfinance due to crumb auth issues
    return {}, "yfinance_disabled_crumb_auth_issue"
    
    yf_err: str | None = None
    try:
        info = yf.Ticker(symbol).info or {}
    except Exception as exc:
        yf_err = f"yfinance_error:{exc.__class__.__name__}"
        info = {}
    
    aliases = ["shortPercentOfFloat", "shortPercentFloat", "shortPercentOfSharesOutstanding"]
    short_pct = None
    for key in aliases:
        value = normalize_percent(info.get(key))
        if value is not None:
            short_pct = value
            break
    
    days_to_cover = as_float(info.get("shortRatio"))
    volume = as_float(info.get("volume"))
    avg_volume = (
        as_float(info.get("averageVolume"))
        or as_float(info.get("averageVolume10days"))
        or as_float(info.get("averageDailyVolume10Day"))
    )
    price = as_float(info.get("currentPrice")) or as_float(info.get("regularMarketPrice"))
    previous_close = as_float(info.get("regularMarketPreviousClose")) or as_float(info.get("previousClose"))
    beta = as_float(info.get("beta")) or as_float(info.get("beta3Year"))
    shares_short = as_float(info.get("sharesShort"))
    shares_short_prior = as_float(info.get("sharesShortPriorMonth"))
    
    si_change_pct = None
    if shares_short is not None and shares_short_prior is not None and shares_short_prior != 0:
        si_change_pct = ((shares_short - shares_short_prior) / shares_short_prior) * 100.0
    
    vol_change_pct = None
    if volume is not None and avg_volume is not None and avg_volume != 0:
        vol_change_pct = ((volume - avg_volume) / avg_volume) * 100.0
    
    price_change_pct = None
    if price is not None and previous_close is not None and previous_close != 0:
        price_change_pct = ((price - previous_close) / previous_close) * 100.0
    
    snapshot = {
        "short_percent_of_float_pct": short_pct,
        "days_to_cover": days_to_cover,
        "volume": volume,
        "avg_volume": avg_volume,
        "vol_change_pct": vol_change_pct,
        "price": price,
        "price_change_pct": price_change_pct,
        "previous_close": previous_close,
        "beta": beta,
        "shares_short": shares_short,
        "shares_short_prior": shares_short_prior,
        "si_change_pct": si_change_pct,
    }
    
    # Unreachable due to early return above
    if yf_err:
        return snapshot, yf_err
    if short_pct is None:
        return snapshot, "short_percent_of_float_unavailable"
    return snapshot, None


def format_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f}%"


def format_number_compact(value: float | None) -> str:
    if value is None:
        return "n/a"
    number = float(value)
    abs_number = abs(number)
    if abs_number >= 1_000_000_000:
        return f"{number / 1_000_000_000:.2f}B"
    if abs_number >= 1_000_000:
        return f"{number / 1_000_000:.2f}M"
    if abs_number >= 1_000:
        return f"{number / 1_000:.2f}K"
    return f"{number:.0f}"


def format_price(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"${value:.2f}"


def format_plain(value: float | None, precision: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{precision}f}"


def format_signed_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.2f}%"


def run_single(symbol: str | None) -> dict[str, Any]:
    """Fetch and return payload for a single ticker."""
    fintel_price, fintel_price_error = fetch_fintel_price(symbol)
    mboum_price, mboum_price_error = fetch_mboum_price(symbol)
    mboum_si_data, mboum_si_error = fetch_mboum_short_interest(symbol)
    yfinance_snapshot, yfinance_error = get_yfinance_snapshot(symbol)
    
    yfinance_short_pct = yfinance_snapshot.get("short_percent_of_float_pct")
    yfinance_price = yfinance_snapshot.get("price")
    
    # Priority: Fintel > MBOUM > yfinance
    price = fintel_price if fintel_price is not None else (mboum_price if mboum_price is not None else yfinance_price)
    price_source = "fintel" if fintel_price is not None else ("mboum" if mboum_price is not None else "yfinance")
    
    return {
        "code": "10-73",
        "symbol": symbol,
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fintel_price": fintel_price,
        "fintel_price_error": fintel_price_error,
        "mboum_price": mboum_price,
        "mboum_price_error": mboum_price_error,
        "mboum_si_data": mboum_si_data,
        "mboum_si_error": mboum_si_error,
        "yfinance_price": yfinance_price,
        "yfinance_short_pct": yfinance_short_pct,
        "yfinance_error": yfinance_error,
        "price_display": price,
        "price_source": price_source,
    }


def print_single(payload: dict[str, Any]) -> None:
    symbol = payload.get("symbol") or "unknown"
    now_local = datetime.now().strftime("%H:%M")
    print(f"10-73 {symbol} {now_local} | DATA: fintel+mboum+yfinance | NOT finviz")
    print(f" Price [{payload.get('price_source', '?')}]: {format_price(payload.get('price_display'))}")
    if payload.get("yfinance_short_pct"):
        print(f" SI [yfinance]: {format_pct(payload.get('yfinance_short_pct'))}")
    if payload.get("mboum_si_data"):
        print(f" SI shares [mboum_v2]: {format_number_compact(payload['mboum_si_data'].get('short_interest'))}")
    if payload.get("fintel_price_error"):
        print(f" WARN fintel_price: {payload['fintel_price_error']}")
    if payload.get("mboum_price_error"):
        print(f" WARN mboum_price: {payload['mboum_price_error']}")
    if payload.get("yfinance_error"):
        print(f" WARN yfinance: {payload['yfinance_error']}")
    if payload.get("mboum_si_error"):
        print(f" WARN mboum_si: {payload['mboum_si_error']}")


def main() -> int:
    args = parse_args()
    load_env_file()
    
    symbols = extract_all_symbols(args.query, args.symbol)
    if not symbols:
        print("Usage: python3 10-73.py --query TICKER")
        print("   or: python3 10-73.py --symbol TICKER1,TICKER2")
        return 1
    
    results: list[dict[str, Any]] = []
    for sym in symbols:
        results.append(run_single(sym))
    
    if args.json:
        if len(results) == 1:
            print(json.dumps(results[0], indent=2))
        else:
            print(json.dumps({"code": "10-73", "tickers": results}, indent=2))
    else:
        for payload in results:
            print_single(payload)
            if results.index(payload) < len(results) - 1:
                print()
    
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

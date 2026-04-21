#!/usr/bin/env python3
"""10-73: Compare short-interest signal between Fintel and yfinance."""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error as urlerror
import urllib.request as urlrequest
from datetime import datetime, timezone
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hermes-config" / "10-codes" / "scripts"))
from typing import Any

from env_utils import load_workspace_env
from api_config import resolve_api_key

try:
    import yfinance as yf
except Exception:  # pragma: no cover - handled at runtime
    yf = None


ROOT = Path(os.environ.get("HERMES_10_CODES_ROOT", Path(__file__).resolve().parent.parent))
DEFAULT_FINTEL_CONTEXT_FILE = ROOT / "tmp" / "fintel_context_from_query.json"
FINTEL_CONTEXT_FILE_ENV = "FINTEL_CONTEXT_FILE"
FINTEL_CONTEXT_JSON_ENV = "FINTEL_CONTEXT_JSON"
MATCH_THRESHOLD_PP = 5.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare Fintel vs yfinance short-interest metrics")
    parser.add_argument("--symbol", help="Ticker/symbol, e.g. RDW; comma-separated for multi-ticker")
    parser.add_argument("--query", help="Freeform text; ticker(s) auto-extracted if present")
    parser.add_argument("--json", action="store_true", help="Print structured JSON output")
    return parser.parse_args()


def extract_all_symbols(query: str | None, symbol_arg: str | None) -> list[str]:
    """Return a deduplicated list of uppercase tickers from --symbol and --query."""
    symbols: list[str] = []
    # --symbol may be comma-separated
    for raw in (symbol_arg or "").split(","):
        s = raw.strip().upper()
        if s:
            symbols.append(s)
    # also parse free-form query for additional tickers not already present
    if query:
        candidate = extract_symbol(query)
        if candidate and candidate not in symbols:
            symbols.append(candidate)
    # deduplicate preserving order
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
            "SHORT",
            "INTEREST",
            "FINTEL",
            "YFINANCE",
            "CURRENT",
            "PRICE",
            "QUOTE",
            "COMPARE",
            "COMPARISON",
            "SYMBOL",
            "TICKER",
            "FOR",
            "OF",
            "ON",
            "ABOUT",
            "WITH",
            "THE",
            "AND",
            "US",
            "USA",
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


def find_value(payload: object, aliases: set[str]) -> object | None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key.lower() in aliases:
                return value
        for value in payload.values():
            nested = find_value(value, aliases)
            if nested is not None:
                return nested
    elif isinstance(payload, list):
        for entry in payload:
            nested = find_value(entry, aliases)
            if nested is not None:
                return nested
    return None


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
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            return parsed

    return None


def extract_fintel_short_interest_pct(context: dict[str, Any] | None) -> float | None:
    if not context:
        return None

    direct = normalize_percent(context.get("short_interest_pct"))
    if direct is not None:
        return direct

    raw = context.get("raw")
    aliases = {
        "shortinterest",
        "short_interest",
        "short_interest_pct",
        "shortinterestpct",
        "short_float",
        "shortpercentoffloat",
        "shortpercentfloat",
        "short_percent_of_float",
        "shortpercent",
    }
    return normalize_percent(find_value(raw, aliases))


def extract_fintel_meta(context: dict[str, Any] | None) -> tuple[str | None, bool | None]:
    if not context:
        return None, None
    source = context.get("short_interest_source")
    proxy = context.get("short_interest_proxy")
    source_text = source if isinstance(source, str) and source.strip() else None
    proxy_bool = proxy if isinstance(proxy, bool) else None
    return source_text, proxy_bool


def extract_fintel_current_price(context: dict[str, Any] | None) -> tuple[float | None, str | None]:
    if not context:
        return None, None
    price = as_float(context.get("current_price"))
    source = context.get("current_price_source")
    source_text = source if isinstance(source, str) and source.strip() else None
    return price, source_text


def extract_massive_cross_ref_price(context: dict[str, Any] | None) -> tuple[float | None, str | None]:
    if not context:
        return None, None
    price = as_float(context.get("massive_current_price"))
    source = context.get("massive_price_source")
    if not source:
        source = context.get("massive_current_price_detail")
    source_text = source if isinstance(source, str) and source.strip() else None
    return price, source_text


def extract_mboum_price(context: dict[str, Any] | None) -> tuple[float | None, str | None]:
    if not context:
        return None, None
    price = as_float(context.get("mboum_current_price"))
    source = context.get("mboum_price_source")
    source_text = source if isinstance(source, str) and source.strip() else None
    return price, source_text


def _get_mboum_key() -> str:
    """Return stripped MBOUM API key, handling quoted .env values."""
    try:
        key, _ = resolve_api_key("mboum")
        return (key or "").strip().strip('"').strip("'")
    except Exception:
        return ""


def fetch_mboum_price(symbol: str) -> tuple[float | None, str | None]:
    """Call MBOUM /v1/markets/stock/quotes and return (price, error_or_None)."""
    mboum_key = _get_mboum_key()
    if not symbol or not mboum_key:
        return None, "mboum_key_missing" if not mboum_key else "missing_symbol"
    base = (os.getenv("MBOUM_BASE_URL") or "https://api.mboum.com").strip().rstrip("/")
    url = f"{base}/v1/markets/stock/quotes"
    req = urlrequest.Request(f"{url}?ticker={symbol}", method="GET")
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
    body = payload.get("body") if isinstance(payload, dict) else None
    if isinstance(body, list) and body:
        price = as_float(body[0].get("regularMarketPrice"))
        if price is not None:
            return price, None
    return None, "mboum_price_missing"


def fetch_mboum_quote_data(symbol: str) -> tuple[dict, str | None]:
    """Fetch quote fields (price, vol, price_change_pct) from MBOUM quotes endpoint."""
    mboum_key = _get_mboum_key()
    if not symbol or not mboum_key:
        return {}, "mboum_key_missing" if not mboum_key else "missing_symbol"
    base = (os.getenv("MBOUM_BASE_URL") or "https://api.mboum.com").strip().rstrip("/")
    req = urlrequest.Request(f"{base}/v1/markets/stock/quotes?ticker={symbol}", method="GET")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "OpenClaw-10-073/1.0")
    req.add_header("Authorization", f"Bearer {mboum_key}")
    try:
        with urlrequest.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urlerror.HTTPError as exc:
        return {}, f"mboum_quote_http:{exc.code}"
    except Exception as exc:
        return {}, f"mboum_quote_error:{exc.__class__.__name__}"
    body = payload.get("body") if isinstance(payload, dict) else None
    if not (isinstance(body, list) and body):
        return {}, "mboum_quote_empty"
    q = body[0]
    price = as_float(q.get("regularMarketPrice"))
    chg_pct = as_float(q.get("regularMarketChangePercent"))
    volume = as_float(q.get("regularMarketVolume"))
    avg_volume = as_float(q.get("averageDailyVolume3Month") or q.get("averageDailyVolume10Day"))
    vol_change_pct = None
    if volume is not None and avg_volume is not None and avg_volume != 0:
        vol_change_pct = ((volume - avg_volume) / avg_volume) * 100.0
    shares_outstanding = as_float(q.get("sharesOutstanding"))
    beta = as_float(q.get("beta"))
    return {
        "price": price,
        "price_change_pct": chg_pct,
        "volume": volume,
        "avg_volume": avg_volume,
        "vol_change_pct": vol_change_pct,
        "shares_outstanding": shares_outstanding,
        "beta": beta,
    }, None


def fetch_mboum_short_interest(symbol: str) -> tuple[dict | None, str | None]:
    """Call Mboum /v2/markets/stock/short-interest and return (data_dict, error).

    Docs: type=STOCKS required. Response fields per record:
      settlementDate, interest, avgDailyShareVolume, daysToCover
    """
    mboum_key = _get_mboum_key()
    if not symbol or not mboum_key:
        return None, "mboum_key_missing" if not mboum_key else "missing_symbol"
    base = (os.getenv("MBOUM_BASE_URL") or "https://api.mboum.com").strip().rstrip("/")
    url = f"{base}/v2/markets/stock/short-interest?ticker={symbol}&type=STOCKS"
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
    # body is a list of records sorted by settlementDate desc; take the most recent
    records = body if isinstance(body, list) else list(body.values()) if isinstance(body, dict) else []
    if not records:
        return None, "mboum_si_no_records"
    latest = records[0]
    # Parse interest as int (may be formatted with commas: "101,263,039")
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
        "short_pct_float": None,  # not returned by this endpoint; computed if float shares known
    }, None


def extract_fintel_symbol(context: dict[str, Any] | None) -> str | None:
    if not context:
        return None
    symbol = context.get("symbol") or context.get("symbol_input")
    if isinstance(symbol, str) and symbol.strip():
        return symbol.strip().upper()
    return None


def get_yfinance_snapshot(symbol: str | None) -> tuple[dict[str, float | None], str | None]:  # noqa: ARG001
    """yfinance disabled — returns empty snapshot without error."""
    return {}, None


def _get_yfinance_snapshot_DISABLED(symbol: str | None) -> tuple[dict[str, float | None], str | None]:
    if not symbol:
        return {}, "missing_symbol"
    if yf is None:
        return {}, "yfinance_not_installed"

    yf_err: str | None = None
    try:
        info = yf.Ticker(symbol).info or {}
    except Exception as exc:  # pragma: no cover - network/runtime dependent
        yf_err = f"yfinance_error:{exc.__class__.__name__}"
        info = {}  # fall through: yf.download() fallback still runs below

    aliases = [
        "shortPercentOfFloat",
        "shortPercentFloat",
        "shortPercentOfSharesOutstanding",
    ]
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

    # Fallback: yf.download() is crumb-free and works when .info crumb auth fails
    if price is None or volume is None:
        try:
            hist = yf.download(symbol, period="5d", progress=False, auto_adjust=True)
            if not hist.empty:
                last_row = hist.iloc[-1]
                prev_row = hist.iloc[-2] if len(hist) >= 2 else None
                if price is None:
                    raw_close = last_row.get("Close")
                    if raw_close is not None:
                        try:
                            price = float(raw_close.iloc[0]) if hasattr(raw_close, "iloc") else float(raw_close)
                        except (TypeError, ValueError):
                            pass
                if volume is None:
                    raw_vol = last_row.get("Volume")
                    if raw_vol is not None:
                        try:
                            volume = float(raw_vol.iloc[0]) if hasattr(raw_vol, "iloc") else float(raw_vol)
                        except (TypeError, ValueError):
                            pass
                if previous_close is None and prev_row is not None:
                    raw_prev = prev_row.get("Close")
                    if raw_prev is not None:
                        try:
                            previous_close = float(raw_prev.iloc[0]) if hasattr(raw_prev, "iloc") else float(raw_prev)
                        except (TypeError, ValueError):
                            pass
                # Recompute derived fields with download data
                if volume is not None and avg_volume is not None and avg_volume != 0:
                    vol_change_pct = ((volume - avg_volume) / avg_volume) * 100.0
                if price is not None and previous_close is not None and previous_close != 0:
                    price_change_pct = ((price - previous_close) / previous_close) * 100.0
        except Exception:
            pass

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

    if yf_err:
        return snapshot, yf_err
    if short_pct is None:
        return snapshot, "short_percent_of_float_unavailable"
    return snapshot, None


def format_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f}%"


def format_pp(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f}pp"


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


def verdict(abs_diff_pp: float | None) -> tuple[str, bool | None]:
    if abs_diff_pp is None:
        return "INSUFFICIENT_DATA", None
    if abs_diff_pp < MATCH_THRESHOLD_PP:
        return f"MATCH (<{MATCH_THRESHOLD_PP:.0f}pp)", True
    return f"MISMATCH (>={MATCH_THRESHOLD_PP:.0f}pp)", False


def run_single(symbol: str | None, context: dict[str, Any] | None, args: argparse.Namespace) -> dict[str, Any]:
    """Fetch and return payload for a single ticker."""
    fintel_short_pct = extract_fintel_short_interest_pct(context)
    fintel_source, fintel_proxy = extract_fintel_meta(context)
    fintel_price, fintel_price_source = extract_fintel_current_price(context)
    massive_price, massive_price_source = extract_massive_cross_ref_price(context)
    mboum_price, mboum_price_source_ctx = extract_mboum_price(context)
    # Mboum v2 short-interest (live, most authoritative SI source alongside Fintel)
    mboum_si_data, mboum_si_error = fetch_mboum_short_interest(symbol)
    mboum_si_pct = None
    mboum_si_dtc = None
    mboum_si_settle = None
    if mboum_si_data:
        raw_pct = mboum_si_data.get("short_pct_float")
        mboum_si_pct = normalize_percent(raw_pct) if raw_pct is not None else None
        mboum_si_dtc = mboum_si_data.get("days_to_cover")
        mboum_si_settle = mboum_si_data.get("settle_date")

    # Always attempt a live MBOUM quote call — gets price, vol, price_change_pct in one shot.
    mboum_quote, mboum_quote_error = fetch_mboum_quote_data(symbol)
    live_mboum_price = mboum_quote.get("price")
    live_mboum_error = mboum_quote_error
    if live_mboum_price is not None:
        mboum_price = live_mboum_price
        mboum_price_source_ctx = "mboum_realtime_live"
    # yfinance disabled — returns empty snapshot
    yfinance_snapshot, yfinance_error = get_yfinance_snapshot(symbol)
    yfinance_short_pct = yfinance_snapshot.get("short_percent_of_float_pct")
    days_to_cover = yfinance_snapshot.get("days_to_cover")
    # Volume/price_change from MBOUM quotes; fall back to empty yfinance snapshot
    volume = mboum_quote.get("volume") or yfinance_snapshot.get("volume")
    avg_volume = mboum_quote.get("avg_volume") or yfinance_snapshot.get("avg_volume")
    vol_change_pct = mboum_quote.get("vol_change_pct") or yfinance_snapshot.get("vol_change_pct")
    yfinance_price = yfinance_snapshot.get("price")
    price = (
        mboum_price if mboum_price is not None
        else fintel_price if fintel_price is not None
        else massive_price
    )
    price_source = (
        "mboum" if mboum_price is not None
        else (fintel_price_source if fintel_price is not None else massive_price_source)
    )
    price_change_pct = mboum_quote.get("price_change_pct") or yfinance_snapshot.get("price_change_pct")
    beta = mboum_quote.get("beta") or yfinance_snapshot.get("beta")
    si_change_pct = yfinance_snapshot.get("si_change_pct")
    massive_cross_ref_diff_pct = None
    if price is not None and massive_price is not None and price != 0:
        massive_cross_ref_diff_pct = ((massive_price - price) / price) * 100.0
    mboum_yf_diff_pct = None
    if mboum_price is not None and yfinance_price is not None and yfinance_price != 0:
        mboum_yf_diff_pct = ((mboum_price - yfinance_price) / yfinance_price) * 100.0

    # Priority for SI%: Fintel > yfinance (Mboum v2 gives shares count, not %; used for DTC only)
    if fintel_short_pct is not None:
        short_float_display_pct = fintel_short_pct
        short_float_source = "live Fintel proxy" if fintel_proxy else "live Fintel"
    else:
        short_float_display_pct = yfinance_short_pct  # None (yfinance disabled)
        short_float_source = "n/a"
    # Prefer Mboum v2 DTC over yfinance DTC if available
    if mboum_si_dtc is not None:
        days_to_cover = mboum_si_dtc

    cmo_signal = "CMO mixed watch trigger"
    if (si_change_pct is not None and si_change_pct > 0) and (vol_change_pct is not None and vol_change_pct > 0):
        cmo_signal = "CMO bullish >75 trigger"
    elif (si_change_pct is not None and si_change_pct < 0) and (vol_change_pct is not None and vol_change_pct < 0):
        cmo_signal = "CMO weak <50 no trigger"

    fresh_callout = "Fresh read -- watch for trigger."
    if cmo_signal == "CMO bullish >75 trigger":
        fresh_callout = "Fresh spike -- entry now 3-5d. PMCC? 📈"

    abs_diff_pp = None
    if short_float_display_pct is not None and yfinance_short_pct is not None:
        abs_diff_pp = abs(short_float_display_pct - yfinance_short_pct)

    verdict_text, is_match = verdict(abs_diff_pp)

    return {
        "code": "10-73",
        "data_sources": ["mboum_quotes", "mboum_v2_short_interest", "fintel_proxy"],
        "NOT_FROM": "finviz",
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "symbol": symbol,
        "fintel_short_interest_pct": fintel_short_pct,
        "fintel_short_interest_source": fintel_source,
        "fintel_short_interest_proxy": fintel_proxy,
        "fintel_current_price": fintel_price,
        "fintel_current_price_source": fintel_price_source,
        "massive_cross_ref_price": massive_price,
        "massive_cross_ref_source": massive_price_source,
        "massive_cross_ref_diff_pct": massive_cross_ref_diff_pct,
        "mboum_price": mboum_price,
        "mboum_price_source": mboum_price_source_ctx,
        "mboum_yf_diff_pct": mboum_yf_diff_pct,
        "mboum_si_shares": mboum_si_data.get("short_interest") if mboum_si_data else None,
        "mboum_si_avg_daily_vol": mboum_si_data.get("avg_daily_volume") if mboum_si_data else None,
        "mboum_si_days_to_cover": mboum_si_dtc,
        "mboum_si_settle_date": mboum_si_settle,
        "mboum_si_error": mboum_si_error,
        "short_float_display_pct": short_float_display_pct,
        "short_float_source": short_float_source,
        "yfinance_short_percent_of_float_pct": yfinance_short_pct,
        "yfinance_days_to_cover": days_to_cover,
        "yfinance_volume": volume,
        "yfinance_avg_volume": avg_volume,
        "yfinance_vol_change_pct": vol_change_pct,
        "price_display": price,
        "price_source": price_source,
        "yfinance_price": yfinance_price,
        "yfinance_price_change_pct": price_change_pct,
        "yfinance_beta": beta,
        "yfinance_si_change_pct": si_change_pct,
        "cmo_signal": cmo_signal,
        "fresh_callout": fresh_callout,
        "abs_diff_pp": abs_diff_pp,
        "match_threshold_pp": MATCH_THRESHOLD_PP,
        "is_match": is_match,
        "verdict": verdict_text,
        "yfinance_error": yfinance_error,
        "live_mboum_error": live_mboum_error,
    }


def print_single(payload: dict[str, Any]) -> None:
    symbol = payload.get("symbol") or "unknown"
    now_local = datetime.now().strftime("%H:%M")
    # Header with explicit source attribution so LLM cannot misrepresent
    print(f"10-73 {symbol} {now_local} | DATA: mboum+fintel | NOT finviz")
    print(f"  SI [{payload.get('short_float_source','?')}]: {format_pct(payload.get('short_float_display_pct'))}")
    mboum_si_settle = payload.get('mboum_si_settle_date') or 'n/a'
    mboum_si_shares = payload.get('mboum_si_shares')
    mboum_si_shares_str = format_number_compact(mboum_si_shares) if mboum_si_shares else 'n/a'
    print(f"  SI shares [mboum_v2 {mboum_si_settle}]: {mboum_si_shares_str}")
    print(f"  DTC [mboum_v2]:               {format_plain(payload.get('mboum_si_days_to_cover'))}")
    print(f"  Vol [mboum]:                  {format_number_compact(payload.get('yfinance_volume'))} ({format_signed_pct(payload.get('yfinance_vol_change_pct'))})")
    price_src = payload.get('price_source') or '?'
    print(f"  Price [{price_src}]:            {format_price(payload.get('price_display'))} ({format_signed_pct(payload.get('yfinance_price_change_pct'))})")
    print(f"  Beta [mboum]:                 {format_plain(payload.get('yfinance_beta'))}")
    print(f"  Signal:                       {payload.get('cmo_signal','?')}")
    si_src = payload.get('short_float_source') or 'n/a'
    print(f"  Verdict [{si_src} vs mboum]: {payload.get('verdict','?')} | diff={format_pp(payload.get('abs_diff_pp'))}")
    if payload.get('live_mboum_error'):
        print(f"  WARN mboum_quote: {payload['live_mboum_error']}")
    if payload.get('mboum_si_error') and 'no_data_returned' not in str(payload.get('mboum_si_error')):
        print(f"  WARN mboum_si: {payload['mboum_si_error']}")
    elif payload.get('mboum_si_error'):
        print(f"  NOTE mboum_si: no SI data in MBOUM for {symbol} (OTC/low-float?)")


def main() -> int:
    args = parse_args()
    load_workspace_env(ROOT)

    context = load_fintel_context()
    # Support multi-ticker: extract list from --symbol (comma-sep) and --query
    symbols = extract_all_symbols(args.query, args.symbol)
    # Fall back to symbol embedded in context if nothing found in args
    if not symbols:
        ctx_sym = extract_fintel_symbol(context)
        if ctx_sym:
            symbols = [ctx_sym]

    if not symbols:
        symbol = None
    else:
        symbol = symbols[0]  # used for the single-symbol context fetch path below
    # Keep backwards compat: single-symbol path uses existing context (already fetched for this symbol)
    # Multi-symbol paths call get_yfinance_snapshot per ticker (MBOUM context not refetched).
    _ = symbol  # single fallback retained for context extraction below
    del symbol  # avoid accidental reuse; use symbols list from here on

    # Build per-symbol payloads.
    # For the first (primary) symbol, use the existing Fintel context (already loaded above).
    # For additional symbols, context is not re-fetched (only yfinance + live MBOUM used).
    results: list[dict[str, Any]] = []
    for i, sym in enumerate(symbols or [None]):
        sym_context = context if i == 0 else None
        results.append(run_single(sym, sym_context, args))

    if args.json:
        if len(results) == 1:
            print(json.dumps(results[0], ensure_ascii=True))
        else:
            print(json.dumps({"code": "10-73", "tickers": results}, ensure_ascii=True))
    else:
        for payload in results:
            print_single(payload)
            if len(results) > 1:
                print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
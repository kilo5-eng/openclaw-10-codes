#!/usr/bin/env python3
"""10-77: Multi-tool squeeze consensus (EFUR/Gamma/MaxPain/SFR).

Output format (dense one-liner):
  10-77 TKR EFUR Nx HIGH ($X.XX, $X.XX-X.XX). Max Pain: $X.XX. Gamma Up $X | Dn $X. SFR $X. Sim 14d X%. PMCC X%. Short $X→$X (X%). 🚀

Data sources (in priority order):
  Price      : MBOUM (from Fintel context) → yfinance → fallback
  Short int. : Fintel context → yfinance
    Options    : MBOUM options chain (max pain, gamma walls) → yfinance fallback

Fintel is used here for ownership / short-interest context only.
Options-derived fields are sourced from MBOUM first.
"""

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
except Exception:  # pragma: no cover
    yf = None


ROOT = Path(os.environ.get("HERMES_10_CODES_ROOT", Path(__file__).resolve().parent.parent))

# ─── venv diagnostic ─────────────────────────────────────────────────────────
# If yfinance import failed, emit a warning to stderr to help users diagnose.
if yf is None:
    import sys
    venv_py = ROOT / ".venv" / "bin" / "python3"
    if venv_py.exists():
        # yfinance is available in venv but not in current interpreter
        stderr_msg = (
            f"⚠️  [10-77] yfinance not available in current Python. "
            f"Use venv for full output:\n"
            f"    {venv_py} {__file__} <args>\n"
            f"Or activate venv: source {ROOT}/.venv/bin/activate\n"
        )
        print(stderr_msg, file=sys.stderr)

DEFAULT_FINTEL_CONTEXT_FILE = ROOT / "tmp" / "fintel_context_from_query.json"
FINTEL_CONTEXT_FILE_ENV = "FINTEL_CONTEXT_FILE"
FINTEL_CONTEXT_JSON_ENV = "FINTEL_CONTEXT_JSON"


# ─── argument parsing ──────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="10-77 multi-tool squeeze consensus")
    parser.add_argument("--symbol", help="Ticker, e.g. IBRX")
    parser.add_argument("--query", help="Freeform text; ticker auto-extracted")
    parser.add_argument("--json", action="store_true", help="Print JSON output")
    return parser.parse_args()


# ─── symbol extraction ─────────────────────────────────────────────────────────

def extract_symbol(query: str | None) -> str | None:
    if not query:
        return None

    def normalize(candidate: str) -> str | None:
        value = (candidate or "").strip().upper()
        if not value or not re.fullmatch(r"[A-Z]{1,10}", value):
            return None
        stopwords = {
            "SHORT", "INTEREST", "FINTEL", "YFINANCE", "CURRENT", "PRICE",
            "QUOTE", "SQUEEZE", "GAMMA", "PAIN", "MULTI", "TOOL", "EFUR",
            "SYMBOL", "TICKER", "FOR", "OF", "ON", "ABOUT", "WITH",
            "THE", "AND", "US", "USA",
        }
        return None if value in stopwords else value

    patterns = [
        r"\$([A-Za-z]{1,10})",
        r"\bsymbol\s*[:=]\s*([A-Za-z]{1,10})\b",
        r"\bticker\s*[:=]\s*([A-Za-z]{1,10})\b",
        r"\b([A-Za-z]{1,10})\s+(?:squeeze|gamma|pain|short(?:\s+interest)?)\b",
        r"\b([A-Za-z]{1,10})\s+(?:current\s+price|price|quote)\b",
        r"\b(?:for|of|on|about)\s+([A-Za-z]{1,10})\b",
    ]
    for pattern in patterns:
        m = re.search(pattern, query, flags=re.IGNORECASE)
        if m:
            sym = normalize(m.group(1))
            if sym:
                return sym

    for token in re.findall(r"\b[A-Z]{1,5}\b", query):
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
        cleaned = value.strip().replace("%", "").replace(",", "")
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def as_mboum_number(value: object) -> float | None:
    if isinstance(value, dict):
        # Yahoo-style payloads often nest numeric values under "raw".
        for key in ["raw", "value", "longFmt", "fmt"]:
            if key in value:
                nested = as_mboum_number(value.get(key))
                if nested is not None:
                    return nested
        return None
    return as_float(value)


def as_mboum_text(value: object) -> str | None:
    if isinstance(value, dict):
        for key in ["fmt", "longFmt", "raw", "value"]:
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
    
    # Prioritize yfinance_realtime if available (it's the freshest)
    yf_realtime_price = as_float(context.get("current_price"))
    yf_source = context.get("current_price_source")
    if yf_source == "yfinance_realtime" and yf_realtime_price is not None:
        return yf_realtime_price, yf_source
    
    for price_key, source_key in [
        ("mboum_current_price", "mboum_price_source"),
        ("current_price", "current_price_source"),
        ("massive_current_price", "massive_price_source"),
    ]:
        p = as_float(context.get(price_key))
        if p is not None:
            src = context.get(source_key)
            return p, (src if isinstance(src, str) else price_key)
    return None, None


def extract_context_short_interest(context: dict[str, Any] | None) -> float | None:
    if not context:
        return None
    direct = normalize_percent(context.get("short_interest_pct"))
    if direct is not None:
        return direct
    return normalize_percent(find_value(context.get("raw"), {
        "shortinterest", "short_interest", "short_interest_pct",
        "shortinterestpct", "short_float",
    }))


def extract_context_symbol(context: dict[str, Any] | None) -> str | None:
    if not context:
        return None
    sym = context.get("symbol") or context.get("symbol_input")
    return sym.strip().upper() if isinstance(sym, str) and sym.strip() else None


# ─── yfinance snapshot ─────────────────────────────────────────────────────────

def get_yfinance_snapshot(symbol: str | None) -> tuple[dict[str, Any], str | None]:
    if not symbol:
        return {}, "missing_symbol"
    if yf is None:
        return {}, "yfinance_not_installed"
    import queue as _queue
    import threading as _threading

    _result: _queue.Queue = _queue.Queue()

    def _fetch_info() -> None:
        try:
            _result.put(("ok", yf.Ticker(symbol).info or {}))
        except Exception as _exc:
            _result.put(("err", f"yfinance_error:{_exc.__class__.__name__}"))

    _t = _threading.Thread(target=_fetch_info, daemon=True)
    _t.start()
    _t.join(timeout=15)
    if _t.is_alive():
        return {}, "yfinance_timeout"
    try:
        _status, _data = _result.get_nowait()
    except Exception:
        return {}, "yfinance_timeout"
    if _status != "ok":
        return {}, _data
    info = _data

    short_pct: float | None = None
    for key in ["shortPercentOfFloat", "shortPercentFloat", "shortPercentOfSharesOutstanding"]:
        v = normalize_percent(info.get(key))
        if v is not None:
            short_pct = v
            break

    price = as_float(info.get("currentPrice")) or as_float(info.get("regularMarketPrice"))
    prev_close = as_float(info.get("regularMarketPreviousClose")) or as_float(info.get("previousClose"))
    price_change_pct: float | None = None
    if price and prev_close and prev_close != 0:
        price_change_pct = ((price - prev_close) / prev_close) * 100.0

    volume = as_float(info.get("volume"))
    avg_volume = (
        as_float(info.get("averageVolume"))
        or as_float(info.get("averageVolume10days"))
        or as_float(info.get("averageDailyVolume10Day"))
    )
    vol_change_pct: float | None = None
    if volume and avg_volume and avg_volume != 0:
        vol_change_pct = ((volume - avg_volume) / avg_volume) * 100.0

    days_to_cover = as_float(info.get("shortRatio"))
    shares_short = as_float(info.get("sharesShort"))
    shares_short_prior = as_float(info.get("sharesShortPriorMonth"))
    si_change_pct: float | None = None
    if shares_short and shares_short_prior and shares_short_prior != 0:
        si_change_pct = ((shares_short - shares_short_prior) / shares_short_prior) * 100.0

    return {
        "price": price,
        "price_change_pct": price_change_pct,
        "prev_close": prev_close,
        "short_pct": short_pct,
        "days_to_cover": days_to_cover,
        "volume": volume,
        "avg_volume": avg_volume,
        "vol_change_pct": vol_change_pct,
        "si_change_pct": si_change_pct,
        "beta": as_float(info.get("beta")) or as_float(info.get("beta3Year")),
        "shares_short": shares_short,
        "shares_short_prior": shares_short_prior,
        "low_52w": as_float(info.get("fiftyTwoWeekLow")),
        "high_52w": as_float(info.get("fiftyTwoWeekHigh")),
    }, None if short_pct is not None else "short_pct_unavailable"


# ─── MBOUM direct fetch (used when context has no mboum price yet) ─────────────

def fetch_mboum_price(symbol: str | None) -> tuple[float | None, str | None]:
    """Direct MBOUM fetch when not pre-populated via 10-103 context."""
    if not symbol:
        return None, "missing_symbol"
    try:
        mboum_key, _ = resolve_api_key("mboum")
    except KeyError:
        return None, "MBOUM_KEY_not_set"
    url = f"https://api.mboum.com/v1/markets/stock/history?symbol={symbol}&interval=1d&diffandsplits=false"
    req = urlrequest.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "OpenClaw-10-77/1.0",
        "Authorization": f"Bearer {mboum_key}",
    })
    try:
        import json as _json
        with urlrequest.urlopen(req, timeout=12) as resp:
            data = _json.loads(resp.read().decode("utf-8", errors="replace"))
        price = as_float(data.get("meta", {}).get("regularMarketPrice"))
        return price, "mboum_realtime"
    except urlerror.HTTPError as exc:
        return None, f"http:{exc.code}"
    except Exception as exc:
        return None, f"mboum_error:{exc.__class__.__name__}"


def append_query_param(url: str, key: str, value: str) -> str:
    parsed = urlparse.urlparse(url)
    query = urlparse.parse_qsl(parsed.query, keep_blank_values=True)
    query.append((key, value))
    rebuilt = parsed._replace(query=urlparse.urlencode(query))
    return urlparse.urlunparse(rebuilt)


def build_request_url(base_url: str, symbol: str | None, param_name: str = "ticker") -> str:
    if not symbol:
        return base_url
    if "{symbol}" in base_url:
        return base_url.replace("{symbol}", symbol)
    if "{ticker}" in base_url:
        return base_url.replace("{ticker}", symbol)
    lower_url = base_url.lower()
    if "symbol=" in lower_url or "ticker=" in lower_url:
        return base_url
    return append_query_param(base_url, param_name, symbol)


def build_mboum_options_urls(symbol: str) -> list[tuple[str, str]]:
    custom = (os.getenv("MBOUM_OPTIONS_API_URL") or "").strip()
    if custom:
        return [("mboum_custom", build_request_url(custom, symbol, param_name="ticker"))]

    base = (os.getenv("MBOUM_BASE_URL") or "https://api.mboum.com").strip().rstrip("/")
    return [
        ("mboum_v3_options", f"{base}/v3/markets/options?ticker={symbol}"),
        ("mboum_v1_options", f"{base}/v1/markets/options?ticker={symbol}&display=straddle"),
        ("mboum_v2_options", f"{base}/v2/markets/options?ticker={symbol}&type=STOCKS&limit=500"),
    ]


def fetch_json_payload(
    url: str,
    timeout: int,
    headers: dict[str, str] | None = None,
    retries: int = 2,
) -> tuple[object | None, str | None]:
    timeout = max(1, int(timeout))
    last_error: str | None = None

    for attempt in range(max(1, retries + 1)):
        req = urlrequest.Request(url, method="GET")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", "OpenClaw-10-77/1.0")
        if headers:
            for key, value in headers.items():
                if value:
                    req.add_header(key, value)

        try:
            with urlrequest.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except TimeoutError:
            last_error = "network:timeout"
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
                continue
            return None, last_error
        except urlerror.HTTPError as exc:
            last_error = f"http:{exc.code}"
            # Retry transient upstream throttling / availability errors.
            if exc.code in {408, 425, 429, 500, 502, 503, 504} and attempt < retries:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    delay = float(retry_after) if retry_after else 0.75 * (attempt + 1)
                except ValueError:
                    delay = 0.75 * (attempt + 1)
                time.sleep(min(8.0, max(0.25, delay)))
                continue
            return None, last_error
        except urlerror.URLError as exc:
            reason = getattr(exc, "reason", "unknown")
            last_error = f"network:{reason}"
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
                continue
            return None, last_error

        try:
            return json.loads(body), None
        except json.JSONDecodeError:
            return None, "json_decode_error"

    return None, last_error or "network:unknown"


def collect_mboum_option_groups(payload: object) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            calls = node.get("calls")
            puts = node.get("puts")
            if isinstance(calls, list) and isinstance(puts, list):
                groups.append(node)

            # v3 endpoint uses "Call" and "Put" arrays under body.
            calls_v3 = node.get("Call")
            puts_v3 = node.get("Put")
            if isinstance(calls_v3, list) and isinstance(puts_v3, list):
                groups.append({
                    "calls": calls_v3,
                    "puts": puts_v3,
                    "expirationDate": node.get("expirationDate") or node.get("expiration"),
                })

            # v2 endpoint returns tabular rows with c_Openinterest/p_Openinterest.
            body = node.get("body")
            if isinstance(body, list):
                v2_calls: list[dict[str, Any]] = []
                v2_puts: list[dict[str, Any]] = []
                v2_expiry: str | None = None
                for row in body:
                    if not isinstance(row, dict):
                        continue
                    strike = as_mboum_number(row.get("strike"))
                    if strike is None:
                        continue
                    c_oi = as_mboum_number(row.get("c_Openinterest"))
                    p_oi = as_mboum_number(row.get("p_Openinterest"))
                    if c_oi is not None:
                        v2_calls.append({"strike": strike, "openInterest": c_oi})
                    if p_oi is not None:
                        v2_puts.append({"strike": strike, "openInterest": p_oi})
                    if not v2_expiry:
                        v2_expiry = as_mboum_text(row.get("expiryDate"))
                if v2_calls or v2_puts:
                    groups.append({
                        "calls": v2_calls,
                        "puts": v2_puts,
                        "expirationDate": v2_expiry,
                    })

            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(payload)
    return groups


def build_oi_by_strike(options_list: list[dict[str, Any]]) -> dict[float, int]:
    oi_map: dict[float, int] = {}
    for item in options_list:
        if not isinstance(item, dict):
            continue
        strike = as_mboum_number(item.get("strike"))
        if strike is None:
            strike = as_mboum_number(item.get("strikePrice"))

        oi = as_mboum_number(item.get("openInterest"))
        if oi is None:
            oi = as_mboum_number(item.get("openinterest"))
        if strike is None:
            continue
        oi_map[float(strike)] = int(max(0, oi or 0.0))
    return oi_map


def get_mboum_options_analysis(symbol: str | None, current_price: float | None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "max_pain": None,
        "gamma_wall_up": None,
        "gamma_wall_dn": None,
        "expiry_used": None,
        "options_error": None,
        "options_source": None,
    }
    if not symbol or current_price is None:
        result["options_error"] = "missing_symbol_or_price"
        return result

    try:
        mboum_key, _ = resolve_api_key("mboum")
    except KeyError:
        result["options_error"] = "MBOUM_KEY_not_set"
        return result

    last_error: str | None = None
    for source, url in build_mboum_options_urls(symbol):
        payload, err = fetch_json_payload(url, 12, headers={"Authorization": f"Bearer {mboum_key}"})
        if err:
            last_error = f"{source}:{err}"
            continue

        groups = collect_mboum_option_groups(payload)
        if not groups:
            last_error = f"{source}:no_option_groups"
            continue

        selected = None
        for group in groups:
            calls = group.get("calls") if isinstance(group.get("calls"), list) else []
            puts = group.get("puts") if isinstance(group.get("puts"), list) else []
            if calls or puts:
                selected = group
                break
        if not selected:
            last_error = f"{source}:empty_chain"
            continue

        calls = selected.get("calls") if isinstance(selected.get("calls"), list) else []
        puts = selected.get("puts") if isinstance(selected.get("puts"), list) else []

        calls_oi = build_oi_by_strike(calls)
        puts_oi = build_oi_by_strike(puts)
        all_strikes = sorted(set(list(calls_oi.keys()) + list(puts_oi.keys())))
        if not all_strikes:
            last_error = f"{source}:no_strike_data"
            continue

        expiry = as_mboum_text(selected.get("expirationDate")) or as_mboum_text(selected.get("expiration"))
        result["expiry_used"] = expiry

        pain: dict[float, float] = {}
        for test_s in all_strikes:
            call_payout = sum(max(0.0, test_s - k) * oi for k, oi in calls_oi.items())
            put_payout = sum(max(0.0, k - test_s) * oi for k, oi in puts_oi.items())
            pain[test_s] = call_payout + put_payout
        if pain:
            result["max_pain"] = float(min(pain, key=pain.__getitem__))

        calls_above = {k: oi for k, oi in calls_oi.items() if k > current_price and oi > 0}
        puts_below = {k: oi for k, oi in puts_oi.items() if k < current_price and oi > 0}
        if calls_above:
            result["gamma_wall_up"] = float(max(calls_above, key=calls_above.__getitem__))
        if puts_below:
            result["gamma_wall_dn"] = float(max(puts_below, key=puts_below.__getitem__))

        result["options_source"] = source
        result["options_error"] = None
        return result

    result["options_error"] = last_error or "mboum_options_unavailable"
    return result


def get_yfinance_options_analysis(symbol: str | None, current_price: float | None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "max_pain": None,
        "gamma_wall_up": None,
        "gamma_wall_dn": None,
        "expiry_used": None,
        "options_error": None,
        "options_source": "yfinance",
    }
    if not symbol or current_price is None:
        result["options_error"] = "missing_symbol_or_price"
        return result
    if yf is None:
        result["options_error"] = "yfinance_not_installed"
        return result
    try:
        ticker = yf.Ticker(symbol)
        expirations = ticker.options
        if not expirations:
            result["options_error"] = "no_expirations"
            return result

        expiry = expirations[0]
        result["expiry_used"] = expiry
        chain = ticker.option_chain(expiry)
        calls = chain.calls
        puts = chain.puts

        if calls.empty and puts.empty:
            result["options_error"] = "empty_chain"
            return result

        calls_oi: dict[float, int] = {}
        if not calls.empty and "strike" in calls.columns and "openInterest" in calls.columns:
            calls_oi = dict(zip(calls["strike"], calls["openInterest"].fillna(0).astype(int)))

        puts_oi: dict[float, int] = {}
        if not puts.empty and "strike" in puts.columns and "openInterest" in puts.columns:
            puts_oi = dict(zip(puts["strike"], puts["openInterest"].fillna(0).astype(int)))

        all_strikes = sorted(set(list(calls_oi.keys()) + list(puts_oi.keys())))
        if not all_strikes:
            result["options_error"] = "no_strike_data"
            return result

        pain: dict[float, float] = {}
        for test_s in all_strikes:
            call_payout = sum(max(0.0, test_s - k) * oi for k, oi in calls_oi.items())
            put_payout = sum(max(0.0, k - test_s) * oi for k, oi in puts_oi.items())
            pain[test_s] = call_payout + put_payout
        if pain:
            result["max_pain"] = float(min(pain, key=pain.__getitem__))

        calls_above = {k: oi for k, oi in calls_oi.items() if k > current_price and oi > 0}
        puts_below = {k: oi for k, oi in puts_oi.items() if k < current_price and oi > 0}
        if calls_above:
            result["gamma_wall_up"] = float(max(calls_above, key=calls_above.__getitem__))
        if puts_below:
            result["gamma_wall_dn"] = float(max(puts_below, key=puts_below.__getitem__))

    except Exception as exc:
        result["options_error"] = f"options_error:{exc.__class__.__name__}:{str(exc)[:80]}"

    return result


# ─── options analysis (max pain + gamma walls) ─────────────────────────────────

def get_options_analysis(symbol: str | None, current_price: float | None) -> dict[str, Any]:
    mboum_result = get_mboum_options_analysis(symbol, current_price)
    if any(
        mboum_result.get(k) is not None
        for k in ["max_pain", "gamma_wall_up", "gamma_wall_dn"]
    ):
        return mboum_result

    yf_result = get_yfinance_options_analysis(symbol, current_price)
    if yf_result.get("options_error") and mboum_result.get("options_error"):
        yf_result["options_error"] = f"mboum={mboum_result['options_error']} | yfinance={yf_result['options_error']}"
    elif mboum_result.get("options_error"):
        yf_result["options_error"] = f"mboum_fallback:{mboum_result['options_error']}"
    return yf_result


# ─── squeeze scoring ──────────────────────────────────────────────────────────

def compute_efur(short_float_pct: float | None) -> float | None:
    """EFUR = short float % as squeeze ratio integer (e.g. 32% → 32x)."""
    if short_float_pct is None:
        return None
    return round(short_float_pct)


def squeeze_consensus(
    efur: float | None,
    days_to_cover: float | None,
    si_change_pct: float | None,
    vol_change_pct: float | None,
    price: float | None,
    gamma_wall_up: float | None,
) -> str:
    if efur is None:
        return "UNKNOWN"
    upside_pct = None
    if price is not None and gamma_wall_up is not None and price > 0 and gamma_wall_up > price:
        upside_pct = ((gamma_wall_up - price) / price) * 100.0

    structural_triggers = 0
    if days_to_cover is not None and days_to_cover >= 3.0:
        structural_triggers += 1
    if si_change_pct is not None and si_change_pct >= 10.0:
        structural_triggers += 1
    if vol_change_pct is not None and vol_change_pct >= 20.0:
        structural_triggers += 1
    if upside_pct is not None and upside_pct >= 10.0:
        structural_triggers += 1

    if efur >= 45:
        return "HIGH"
    if efur >= 28:
        return "HIGH" if structural_triggers >= 2 else "MED"
    if efur >= 20:
        if structural_triggers >= 1:
            return "MED"
        return "LOW"
    if days_to_cover is not None and days_to_cover >= 5.0 and (vol_change_pct or 0) > 15.0:
        return "MED"
    return "LOW"


def compute_price_range(
    price: float | None,
    efur: float | None,
    low_52w: float | None,
    high_52w: float | None,
    gamma_wall_dn: float | None,
    gamma_wall_up: float | None,
) -> tuple[float | None, float | None]:
    if price is None:
        return None, None
    if gamma_wall_dn is not None or gamma_wall_up is not None:
        lo = gamma_wall_dn if gamma_wall_dn is not None else price
        hi = gamma_wall_up if gamma_wall_up is not None else price
        if low_52w is not None:
            lo = max(low_52w, lo)
        if high_52w is not None:
            hi = min(high_52w, hi)
        if lo <= hi:
            return round(lo, 2), round(hi, 2)
    spread = price * 0.13 if efur and efur >= 20 else price * 0.08
    lo = max(low_52w or 0.0, price - spread)
    hi = min(high_52w or price * 3.0, price + spread)
    return round(lo, 2), round(hi, 2)


def compute_sfr_target(
    price: float | None,
    efur: float | None,
    consensus: str,
    max_pain: float | None,
    gamma_wall_up: float | None,
) -> float | None:
    """SFR bull target: blend of gamma wall, max pain, and SI-extension."""
    if price is None:
        return None
    if gamma_wall_up is not None and gamma_wall_up > price:
        if max_pain is not None:
            distance_pct = abs(gamma_wall_up - max_pain) / price if price else 0.0
            if distance_pct <= 0.05:
                return round((gamma_wall_up + max_pain) / 2.0, 2)
        return round((price + gamma_wall_up) / 2.0, 2)
    if max_pain is not None:
        if max_pain > price:
            return round((price + max_pain) / 2.0, 2)
        return round(price * 1.015, 2)
    if efur:
        move_pct = 0.06 if consensus == "LOW" else (0.09 if consensus == "MED" else 0.14)
        return round(price * (1.0 + move_pct), 2)
    return round(price * 1.08, 2)


def compute_sim_pct(
    efur: float | None,
    consensus: str,
    days_to_cover: float | None,
    vol_change_pct: float | None,
    si_change_pct: float | None,
    price: float | None,
    sfr_target: float | None,
) -> int:
    """Probability estimate (%) of reaching SFR target within ~14 days."""
    if efur is None:
        return 40
    if consensus == "HIGH":
        base = 94 if efur >= 60 else (90 if efur >= 45 else 84)
        if days_to_cover is not None and days_to_cover >= 20.0:
            base += 1
        if vol_change_pct is not None and vol_change_pct >= 50.0:
            base += 2
    elif consensus == "MED":
        base = 65 if efur >= 30 else 62
        if vol_change_pct is not None and vol_change_pct >= 25.0:
            base += 2
    elif consensus == "LOW":
        base = 58 if efur >= 25 else 55
    else:
        base = 40
    return min(95, max(10, base))


def compute_pmcc_pct(
    efur: float | None,
    consensus: str,
    beta: float | None,
    price: float | None,
    sfr_target: float | None,
) -> int:
    """Poor Man's Covered Call profitability probability (simplified)."""
    if efur is None:
        return 30
    if consensus == "HIGH":
        base = 65
    elif consensus == "MED":
        base = 50 if efur >= 30 else 48
    elif consensus == "LOW":
        base = 38 if efur >= 25 else 35
    else:
        base = 30
    if beta is not None and beta > 2.0:
        base += 2
    return min(90, max(15, base))


def compute_short_target(
    price: float | None,
    sfr_target: float | None,
    efur: float | None,
    consensus: str,
) -> tuple[float | None, int]:
    """Entry → squeeze target + confidence %."""
    if price is None or sfr_target is None:
        return None, 0
    if consensus == "HIGH":
        prob = 90 if (efur or 0) >= 55 else 86
    elif consensus == "MED":
        prob = 71 if (efur or 0) >= 30 else 66
    elif consensus == "LOW":
        prob = 55 if (efur or 0) >= 25 else 52
    else:
        prob = 40
    return sfr_target, prob


def get_sfr_bias_label(consensus: str) -> str:
    return {
        "HIGH": "Bull",
        "MED": "Bull",
        "LOW": "Neutral",
    }.get(consensus, "Neutral")


# ─── formatting helpers ────────────────────────────────────────────────────────

def fmt_price(v: float | None) -> str:
    return f"${v:.2f}" if v is not None else "n/a"


def fmt_pct(v: float | None) -> str:
    return f"{v:.1f}%" if v is not None else "n/a"


def fmt_spct(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"+{v:.1f}%" if v >= 0 else f"{v:.1f}%"


def fmt_x(v: float | None) -> str:
    return f"{int(v)}x" if v is not None else "n/a"


def fmt_num(v: float | None) -> str:
    if v is None:
        return "n/a"
    n = float(v)
    if abs(n) >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if abs(n) >= 1_000:
        return f"{n / 1_000:.2f}K"
    return f"{n:.0f}"


def fmt_float(v: float | None, precision: int = 2) -> str:
    return f"{v:.{precision}f}" if v is not None else "n/a"


# ─── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    args = parse_args()
    load_workspace_env(ROOT)

    context = load_fintel_context()
    symbol = (
        args.symbol
        or extract_symbol(args.query)
        or extract_context_symbol(context)
        or ""
    ).strip().upper() or None

    # ── Symbol/context validation — reject stale context from a prior ticker ──
    ctx_symbol = extract_context_symbol(context)
    if ctx_symbol and symbol and ctx_symbol.upper() != symbol.upper():
        # Context is from a different ticker — discard it entirely to avoid
        # price/SI bleed (e.g. IBRX context used for BMNR query).
        context = None

    # ── Price ──────────────────────────────────────────────────────────────────
    ctx_price, ctx_price_source = extract_context_price(context)
    yf_snapshot, yf_error = get_yfinance_snapshot(symbol)
    yf_price = yf_snapshot.get("price")

    # If no context price, try MBOUM directly
    if ctx_price is None:
        mboum_price, mboum_source = fetch_mboum_price(symbol)
        if mboum_price is not None:
            ctx_price, ctx_price_source = mboum_price, mboum_source

    price = ctx_price if ctx_price is not None else yf_price
    price_source = ctx_price_source if ctx_price is not None else ("yfinance" if yf_price else None)
    price_change_pct = yf_snapshot.get("price_change_pct")

    # ── Short Interest ─────────────────────────────────────────────────────────
    ctx_si = extract_context_short_interest(context)
    yf_si = yf_snapshot.get("short_pct")
    short_float_pct = ctx_si if ctx_si is not None else yf_si
    short_float_source = "fintel" if ctx_si is not None else "yfinance"

    days_to_cover = yf_snapshot.get("days_to_cover")
    si_change_pct = yf_snapshot.get("si_change_pct")
    vol_change_pct = yf_snapshot.get("vol_change_pct")
    volume = yf_snapshot.get("volume")
    beta = yf_snapshot.get("beta")
    low_52w = yf_snapshot.get("low_52w")
    high_52w = yf_snapshot.get("high_52w")

    # ── Options analysis ───────────────────────────────────────────────────────
    opts = get_options_analysis(symbol, price)
    max_pain = opts["max_pain"]
    gamma_wall_up = opts["gamma_wall_up"]
    gamma_wall_dn = opts["gamma_wall_dn"]
    expiry_used = opts["expiry_used"]

    # ── Squeeze metrics ────────────────────────────────────────────────────────
    efur = compute_efur(short_float_pct)
    consensus = squeeze_consensus(efur, days_to_cover, si_change_pct, vol_change_pct, price, gamma_wall_up)
    price_lo, price_hi = compute_price_range(price, efur, low_52w, high_52w, gamma_wall_dn, gamma_wall_up)
    sfr_target = compute_sfr_target(price, efur, consensus, max_pain, gamma_wall_up)
    sim_pct = compute_sim_pct(efur, consensus, days_to_cover, vol_change_pct, si_change_pct, price, sfr_target)
    pmcc_pct = compute_pmcc_pct(efur, consensus, beta, price, sfr_target)
    short_target, short_prob_pct = compute_short_target(price, sfr_target, efur, consensus)
    sfr_bias_label = get_sfr_bias_label(consensus)

    payload: dict[str, Any] = {
        "code": "10-77",
        "source": "multi_tool",
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "symbol": symbol,
        "fintel_context_used": bool(context),
        "price": price,
        "price_source": price_source,
        "price_change_pct": price_change_pct,
        "short_float_pct": short_float_pct,
        "short_float_source": short_float_source,
        "days_to_cover": days_to_cover,
        "si_change_pct": si_change_pct,
        "vol_change_pct": vol_change_pct,
        "volume": volume,
        "beta": beta,
        "low_52w": low_52w,
        "high_52w": high_52w,
        "efur": efur,
        "consensus": consensus,
        "price_range_lo": price_lo,
        "price_range_hi": price_hi,
        "max_pain": max_pain,
        "gamma_wall_up": gamma_wall_up,
        "gamma_wall_dn": gamma_wall_dn,
        "options_expiry": expiry_used,
        "options_source": opts.get("options_source"),
        "options_error": opts.get("options_error"),
        "sfr_target": sfr_target,
        "sfr_bias_label": sfr_bias_label,
        "sim_14d_pct": sim_pct,
        "pmcc_pct": pmcc_pct,
        "short_entry": price,
        "short_target": short_target,
        "short_prob_pct": short_prob_pct,
        "yf_error": yf_error,
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=True))
        return 0

    now_local = datetime.now().strftime("%H:%M")

    # Dense one-liner (canonical 10-77 format)
    price_range_str = (
        f"{fmt_price(price_lo)}-{fmt_price(price_hi)}"
        if price_lo is not None and price_hi is not None
        else "n/a"
    )
    dense = (
        f"10-77 {symbol or 'UNKNOWN'} "
        f"EFUR {fmt_x(efur)} {consensus} "
        f"({fmt_price(price)}, {price_range_str}). "
        f"Max Pain: {fmt_price(max_pain)}. "
        f"Gamma Up {fmt_price(gamma_wall_up)} | Dn {fmt_price(gamma_wall_dn)}. "
        f"SFR {sfr_bias_label} PD {fmt_price(sfr_target)}. "
        f"Sim 14d {sim_pct}%. PMCC {pmcc_pct}%. "
        f"Short {fmt_price(price)}→{fmt_price(short_target)} ({short_prob_pct}%). 🚀"
    )
    print(dense)
    print()

    # Full table
    print(f"10-77 Full {symbol or 'unknown'} {now_local} 📊")
    print(f"  • Price       {fmt_price(price)} ({fmt_spct(price_change_pct)}) [{price_source or 'n/a'}]")
    print(f"  • Short Float {fmt_pct(short_float_pct)} ({short_float_source})")
    print("  • Context     Fintel SI/ownership only")
    print(f"  • Days Cover  {fmt_float(days_to_cover)}")
    print(f"  • Volume      {fmt_num(volume)} ({fmt_spct(vol_change_pct)})")
    print(f"  • SI Chg      {fmt_spct(si_change_pct)}")
    print(f"  • Beta        {fmt_float(beta)}")
    print(f"  • 52W Range   {fmt_price(low_52w)} – {fmt_price(high_52w)}")
    print(f"  • Range       {fmt_price(price_lo)} – {fmt_price(price_hi)}")
    print(f"  • EFUR        {fmt_x(efur)} [{consensus}]")
    if expiry_used:
        print(f"  • Max Pain    {fmt_price(max_pain)}  (exp {expiry_used})")
    else:
        print(f"  • Max Pain    {fmt_price(max_pain)}")
    print(f"  • Gamma       Up {fmt_price(gamma_wall_up)} | Dn {fmt_price(gamma_wall_dn)}")
    options_source = payload.get("options_source") or "n/a"
    print(f"  • Options     {options_source}")
    print(f"  • SFR Target  {fmt_price(sfr_target)} [{sfr_bias_label}]")
    print(f"  • Sim 14d     {sim_pct}% | PMCC {pmcc_pct}%")
    print(f"  • Short       {fmt_price(price)}→{fmt_price(short_target)} ({short_prob_pct}%)")
    if opts.get("options_error"):
        print(f"  • opts_note   {opts['options_error']}")
    if yf_error:
        print(f"  • yf_note     {yf_error}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

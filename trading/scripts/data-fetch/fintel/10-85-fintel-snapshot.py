#!/usr/bin/env python3
"""10-103: Fintel normalized snapshot for downstream 10-codes."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

try:
    import yfinance as yf  # type: ignore
except Exception:  # pragma: no cover
    yf = None

from env_utils import load_workspace_env, require_env


ROOT = Path(os.environ.get("HERMES_10_CODES_ROOT", Path(__file__).resolve().parent.parent))
DEFAULT_TIMEOUT_SECONDS = 12
DEFAULT_OUTPUT_PATH = ROOT / "tmp" / "fintel_10_103_latest.json"
DEFAULT_SYMBOL = (os.getenv("FINTEL_DEFAULT_SYMBOL") or "IBRX").strip().upper()
DEFAULT_FINTEL_API_URL = "https://api.fintel.io/web/v/0.0/ss/us"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch and normalize a Fintel snapshot")
    parser.add_argument("--symbol", help="Ticker/symbol to request, e.g. IBRX")
    parser.add_argument("--query", help="Freeform text; symbol is auto-extracted if present")
    parser.add_argument("--json", action="store_true", help="Print normalized JSON")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH), help="Path to write normalized JSON")
    parser.add_argument("--no-write", action="store_true", help="Skip writing output file")
    return parser.parse_args()


def extract_symbol(query: str | None) -> str | None:
    if not query:
        return None

    def normalize_candidate(candidate: str) -> str | None:
        value = (candidate or "").strip().upper()
        if not value or not re.fullmatch(r"[A-Z]{1,10}", value):
            return None
        stopwords = {
            "FINTEL",
            "SHORT",
            "INTEREST",
            "CURRENT",
            "PRICE",
            "QUOTE",
            "OWNERSHIP",
            "HOLDERS",
            "HOLDER",
            "INSIDER",
            "TRADING",
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
        r"\b([A-Za-z]{1,10})\s+(?:short(?:\s+interest)?|holders?|ownership|fintel)\b",
        r"\b([A-Za-z]{1,10})\s+(?:current\s+price|price|quote)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, query, flags=re.IGNORECASE)
        if match:
            symbol = normalize_candidate(match.group(1))
            if symbol:
                return symbol

    # Last-resort heuristic: use an all-caps token that looks ticker-like.
    for token in re.findall(r"\b[A-Z]{1,5}\b", query):
        symbol = normalize_candidate(token)
        if symbol:
            return symbol
    return None


def append_query_param(url: str, key: str, value: str) -> str:
    parsed = urlparse.urlparse(url)
    query = urlparse.parse_qsl(parsed.query, keep_blank_values=True)
    query.append((key, value))
    rebuilt = parsed._replace(query=urlparse.urlencode(query))
    return urlparse.urlunparse(rebuilt)


def build_request_url(base_url: str, symbol: str | None) -> str:
    country = (os.getenv("FINTEL_COUNTRY") or "us").strip().lower()
    if "{country}" in base_url:
        base_url = base_url.replace("{country}", country)

    if not symbol:
        return base_url
    if "{symbol}" in base_url:
        return base_url.replace("{symbol}", symbol)

    lower_url = base_url.lower()
    if "symbol=" in lower_url or "ticker=" in lower_url:
        return base_url

    return append_query_param(base_url, "symbol", symbol)


def upsert_query_param(url: str, key: str, value: str) -> str:
    parsed = urlparse.urlparse(url)
    query = [(k, v) for k, v in urlparse.parse_qsl(parsed.query, keep_blank_values=True) if k.lower() != key.lower()]
    query.append((key, value))
    rebuilt = parsed._replace(query=urlparse.urlencode(query))
    return urlparse.urlunparse(rebuilt)


def redact_query_param(url: str, key: str) -> str:
    parsed = urlparse.urlparse(url)
    query = []
    for name, value in urlparse.parse_qsl(parsed.query, keep_blank_values=True):
        query.append((name, "<redacted>" if name.lower() == key.lower() else value))
    rebuilt = parsed._replace(query=urlparse.urlencode(query))
    return urlparse.urlunparse(rebuilt)


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


def as_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    return str(value)


def normalize_percent(value: object) -> float | None:
    pct = as_float(value)
    if pct is None:
        return None
    if -1.0 <= pct <= 1.0:
        return pct * 100.0
    return pct


def extract_short_interest_pct(payload: object) -> float | None:
    return as_float(
        find_value(
            payload,
            {
                "shortinterest",
                "short_interest",
                "short_interest_pct",
                "shortinterestpct",
                "short_float",
                "shortpercentoffloat",
                "shortpercentfloat",
                "short_percent_of_float",
                "shortpercent",
            },
        )
    )


def extract_short_volume_ratio_pct(payload: object) -> float | None:
    ratio = as_float(
        find_value(
            payload,
            {
                "shortvolumeratio",
                "short_volume_ratio",
            },
        )
    )
    if ratio is None:
        return None
    if -1.0 <= ratio <= 1.0:
        return ratio * 100.0
    return ratio


def extract_current_price(payload: object) -> float | None:
    return as_float(
        find_value(
            payload,
            {
                "currentprice",
                "current_price",
                "price",
                "lastprice",
                "last_price",
                "regularmarketprice",
                "close",
            },
        )
    )


def build_massive_price_urls(symbol: str) -> list[tuple[str, str]]:
    custom_url = (os.getenv("MASSIVE_PRICE_API_URL") or os.getenv("POLYGON_PRICE_API_URL") or "").strip()
    if custom_url:
        return [("massive_custom", build_request_url(custom_url, symbol))]

    hosts = []
    configured_host = (os.getenv("MASSIVE_BASE_URL") or os.getenv("POLYGON_BASE_URL") or "").strip().rstrip("/")
    if configured_host:
        hosts.append(configured_host)
    for default_host in ["https://api.massive.com", "https://api.polygon.io"]:
        if default_host not in hosts:
            hosts.append(default_host)

    urls: list[tuple[str, str]] = []
    for host in hosts:
        urls.append(("massive_snapshot", f"{host}/v2/snapshot/locale/us/markets/stocks/tickers/{symbol}"))
        urls.append(("massive_last_trade", f"{host}/v2/last/trade/{symbol}"))
        urls.append(("massive_prev_close", f"{host}/v2/aggs/ticker/{symbol}/prev?adjusted=true"))
    return urls


def extract_massive_current_price(payload: object) -> tuple[float | None, str | None]:
    if not isinstance(payload, dict):
        return None, None

    ticker = payload.get("ticker")
    if isinstance(ticker, dict):
        last_trade = ticker.get("lastTrade") or ticker.get("last_trade")
        if isinstance(last_trade, dict):
            price = as_float(last_trade.get("p") or last_trade.get("price"))
            if price is not None:
                return price, "last_trade"

        session = ticker.get("day")
        if isinstance(session, dict):
            price = as_float(session.get("c") or session.get("close"))
            if price is not None:
                return price, "day_close"

        minute_bar = ticker.get("min")
        if isinstance(minute_bar, dict):
            price = as_float(minute_bar.get("c") or minute_bar.get("close"))
            if price is not None:
                return price, "minute_close"

    results = payload.get("results")
    if isinstance(results, dict):
        price = as_float(results.get("p") or results.get("price"))
        if price is not None:
            return price, "last_trade"

    if isinstance(results, list):
        for entry in results:
            if not isinstance(entry, dict):
                continue
            price = as_float(entry.get("p") or entry.get("price"))
            if price is not None:
                return price, "last_trade"
            price = as_float(entry.get("c") or entry.get("close"))
            if price is not None:
                return price, "prev_close"

    return None, None


def build_mboum_price_url(symbol: str) -> str:
    custom = (os.getenv("MBOUM_PRICE_API_URL") or "").strip()
    if custom:
        return build_request_url(custom, symbol)
    base = (os.getenv("MBOUM_BASE_URL") or "https://api.mboum.com").strip().rstrip("/")
    return f"{base}/v1/markets/stock/history?symbol={symbol}&interval=1d&diffandsplits=false"


def extract_mboum_meta_price(payload: object) -> tuple[float | None, str | None]:
    if not isinstance(payload, dict):
        return None, None
    meta = payload.get("meta")
    if isinstance(meta, dict):
        price = as_float(meta.get("regularMarketPrice"))
        if price is not None:
            return price, "mboum_realtime"
    # fallback: top-level regularMarketPrice
    price = as_float(payload.get("regularMarketPrice"))
    if price is not None:
        return price, "mboum_realtime"
    return None, None


def fetch_json_payload(url: str, timeout: int, headers: dict[str, str] | None = None) -> tuple[object | None, str | None]:
    """Fetch JSON with retry+backoff for transient socket/SSL timeouts."""
    import time
    import socket
    
    max_retries = 3
    base_wait = 0.5
    
    for attempt in range(max_retries):
        try:
            req = urlrequest.Request(url, method="GET")
            req.add_header("Accept", "application/json")
            req.add_header("User-Agent", "OpenClaw-10-103/1.0")
            if headers:
                for key, value in headers.items():
                    if value:
                        req.add_header(key, value)
            
            with urlrequest.urlopen(req, timeout=max(1, timeout)) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except socket.timeout as exc:
            if attempt < max_retries - 1:
                wait_time = base_wait * (2 ** attempt)
                time.sleep(wait_time)
                continue
            return None, f"socket_timeout_after_{max_retries}_retries"
        except urlerror.URLError as exc:
            if isinstance(exc.reason, socket.timeout):
                if attempt < max_retries - 1:
                    wait_time = base_wait * (2 ** attempt)
                    time.sleep(wait_time)
                    continue
                return None, f"socket_timeout_after_{max_retries}_retries"
            return None, f"network:{exc.reason}"
        except urlerror.HTTPError as exc:
            return None, f"http:{exc.code}"

        try:
            return json.loads(body), None
        except json.JSONDecodeError:
            return {"text": body}, None
    
    return None, "fetch_json_payload_exhausted_retries"


def fetch_mboum_short_interest(symbol: str, timeout: int) -> tuple[dict[str, object] | None, str | None]:
    mboum_key = (os.getenv("MBOUM_KEY") or "").strip()
    if not symbol or not mboum_key:
        return None, "mboum_key_missing" if not mboum_key else "missing_symbol"
    base = (os.getenv("MBOUM_BASE_URL") or "https://api.mboum.com").strip().rstrip("/")
    url = f"{base}/v2/markets/stock/short-interest?ticker={symbol}&type=STOCKS"
    payload, err = fetch_json_payload(url, timeout, headers={"Authorization": f"Bearer {mboum_key}"})
    if err:
        return None, f"mboum_si_{err}"
    if not isinstance(payload, dict):
        return None, "mboum_si_bad_response"
    body = payload.get("body")
    if not body:
        return None, "mboum_si_empty_body"
    records = body if isinstance(body, list) else list(body.values()) if isinstance(body, dict) else []
    if not records:
        return None, "mboum_si_no_records"
    latest = records[0] if isinstance(records[0], dict) else {}
    return {
        "settlement_date": latest.get("settlementDate"),
        "short_interest_shares": as_float(str(latest.get("interest", "")).replace(",", "")),
        "avg_daily_volume": as_float(str(latest.get("avgDailyShareVolume", "")).replace(",", "")),
        "days_to_cover": as_float(latest.get("daysToCover")),
        "source": "mboum_v2_short_interest",
    }, None


def fetch_yfinance_stats(symbol: str) -> tuple[dict[str, object] | None, str | None]:
    if not symbol:
        return None, "missing_symbol"
    if yf is None:
        return None, "yfinance_not_installed"
    try:
        info = yf.Ticker(symbol).info or {}
    except Exception as exc:  # pragma: no cover
        return None, f"yfinance_error:{exc.__class__.__name__}"
    return {
        "current_price": as_float(info.get("currentPrice")),
        "float_shares": as_float(info.get("floatShares")),
        "shares_outstanding": as_float(info.get("sharesOutstanding")),
        "institutional_hold_pct": normalize_percent(info.get("heldPercentInstitutions")),
        "insider_hold_pct_yf": normalize_percent(info.get("heldPercentInsiders")),
        "shares_short": as_float(info.get("sharesShort")),
        "shares_short_prior": as_float(info.get("sharesShortPriorMonth")),
        "days_to_cover_yf": as_float(info.get("shortRatio")),
        "source": "yfinance",
    }, None


def fetch_fintel_owners(symbol: str, api_key: str, timeout: int) -> tuple[dict[str, object] | None, str | None]:
    slug = symbol.lower()
    url = f"https://api.fintel.io/web/v/0.0/so/us/{slug}"
    payload, err = fetch_json_payload(url, timeout, headers={"X-API-Key": api_key})
    if err:
        return None, f"fintel_owners_{err}"
    if not isinstance(payload, dict):
        return None, "fintel_owners_bad_response"
    owners = payload.get("owners") if isinstance(payload.get("owners"), list) else []
    top = []
    for owner in owners[:5]:
        if not isinstance(owner, dict):
            continue
        name = as_text(owner.get("name"))
        pct = as_float(owner.get("ownershipPercent"))
        if name and pct is not None:
            top.append({"name": name, "ownership_percent": pct})
    return {
        "owners_count": len(owners),
        "top_holders": top,
        "owners_endpoint": url,
        "owners_source": "fintel_owners",
    }, None


def fetch_fintel_insiders(symbol: str, api_key: str, timeout: int) -> tuple[dict[str, object] | None, str | None]:
    slug = symbol.lower()
    url = f"https://api.fintel.io/web/v/0.0/n/us/{slug}"
    payload, err = fetch_json_payload(url, timeout, headers={"X-API-Key": api_key})
    if err:
        return None, f"fintel_insiders_{err}"
    if not isinstance(payload, dict):
        return None, "fintel_insiders_bad_response"
    insiders = payload.get("insiders") if isinstance(payload.get("insiders"), list) else []
    latest_trade = None
    for insider in insiders:
        if not isinstance(insider, dict):
            continue
        latest_trade = {
            "name": as_text(insider.get("name")),
            "code": as_text(insider.get("code")),
            "shares": as_float(insider.get("shares")),
            "transaction_date": as_text(insider.get("transactionDate")),
        }
        break
    return {
        "insider_hold_pct": as_float(payload.get("insiderOwnershipPercentFloat")),
        "latest_insider_trade": latest_trade,
        "insiders_count": len(insiders),
        "insiders_endpoint": url,
        "insiders_source": "fintel_insiders",
    }, None


def normalize_payload(payload: object, symbol_input: str | None, endpoint: str) -> dict[str, object]:
    symbol = as_text(find_value(payload, {"symbol", "ticker"})) or symbol_input
    rank = as_float(find_value(payload, {"rank", "squeeze_rank", "ranking"}))
    score = as_float(find_value(payload, {"score", "squeeze_score", "rating"}))
    short_interest_pct = as_float(
        find_value(
            payload,
            {
                "shortinterest",
                "short_interest",
                "short_interest_pct",
                "shortinterestpct",
                "short_float",
            },
        )
    )
    borrow_fee_pct = as_float(
        find_value(
            payload,
            {
                "borrowfee",
                "borrow_fee",
                "borrow_fee_pct",
                "borrowfeepct",
                "cost_to_borrow",
                "ctb",
            },
        )
    )
    current_price = extract_current_price(payload)
    updated_at = as_text(find_value(payload, {"updatedat", "lastupdated", "date", "timestamp"}))
    owners_count = None
    if isinstance(payload, dict) and isinstance(payload.get("owners"), list):
        owners_count = len(payload["owners"])

    return {
        "source": "fintel.io",
        "endpoint": endpoint,
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "symbol_input": symbol_input,
        "symbol": symbol,
        "rank": rank,
        "score": score,
        "short_interest_pct": short_interest_pct,
        "borrow_fee_pct": borrow_fee_pct,
        "current_price": current_price,
        "updated_at": updated_at,
        "owners_count": owners_count,
        "raw": payload,
    }


def summarize(normalized: dict[str, object]) -> str:
    parts: list[str] = []
    for key in [
        "symbol",
        "current_price",
        "owners_count",
        "rank",
        "score",
        "short_interest_pct",
        "short_interest_source",
        "borrow_fee_pct",
    ]:
        value = normalized.get(key)
        if value is not None:
            parts.append(f"{key}={value}")
    return ", ".join(parts) if parts else "normalized snapshot captured"


def format_text_report(normalized: dict[str, object], output_path: str | None = None) -> str:
    symbol = normalized.get("symbol") or normalized.get("symbol_input") or "?"
    lines = [f"10-103 {symbol} Snapshot"]

    field_map = [
        ("current_price", "Price"),
        ("current_price_source", "Price Src"),
        ("short_interest_pct", "SI/Float %"),
        ("short_interest_source", "SI Src"),
        ("short_interest_shares", "Short Shares"),
        ("days_to_cover", "DTC"),
        ("float_shares", "Float"),
        ("institutional_hold_pct", "Inst Hold %"),
        ("insider_hold_pct", "Insider Hold %"),
        ("short_volume_ratio_pct", "Short Vol %"),
        ("borrow_fee_pct", "Borrow Fee %"),
        ("owners_count", "Owners"),
        ("rank", "Rank"),
        ("score", "Score"),
        ("endpoint", "Endpoint"),
    ]
    for key, label in field_map:
        value = normalized.get(key)
        if value is not None:
            lines.append(f"  • {label:<15} {value}")

    top_holders = normalized.get("top_holders")
    if isinstance(top_holders, list) and top_holders:
        pretty = ", ".join(
            f"{item.get('name')} {round(float(item.get('ownership_percent', 0.0)), 2)}%"
            for item in top_holders[:3] if isinstance(item, dict) and item.get('name')
        )
        if pretty:
            lines.append(f"  • Top Holders     {pretty}")

    latest_trade = normalized.get("latest_insider_trade")
    if isinstance(latest_trade, dict) and latest_trade.get("name"):
        trade_desc = f"{latest_trade.get('name')} {latest_trade.get('code') or ''} {latest_trade.get('shares') or ''} {latest_trade.get('transaction_date') or ''}".strip()
        lines.append(f"  • Insider Trade   {trade_desc}")

    if output_path:
        lines.append(f"  • Output          {output_path}")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    load_workspace_env(ROOT)

    api_key = require_env("FINTEL_API_KEY", "Set it in the 10-codes .env")
    base_url = (os.getenv("FINTEL_API_URL") or "").strip()
    if not base_url:
        base_url = DEFAULT_FINTEL_API_URL
    if "REPLACE_WITH_YOUR_PATH" in base_url:
        base_url = DEFAULT_FINTEL_API_URL

    symbol = (
        args.symbol
        or extract_symbol(args.query)
        or os.getenv("FINTEL_SYMBOL")
        or DEFAULT_SYMBOL
        or ""
    ).strip().upper() or None
    request_url = build_request_url(base_url, symbol)

    payload, fetch_error = fetch_json_payload(
        request_url,
        args.timeout,
        headers={
            "Authorization": f"Bearer {api_key}",
            "X-API-Key": api_key,
        },
    )
    if fetch_error:
        normalized = normalize_payload({}, symbol, request_url)
        normalized["primary_endpoint_error"] = fetch_error
    else:
        normalized = normalize_payload(payload, symbol, request_url)

    if normalized.get("short_interest_pct") is not None:
        normalized["short_interest_source"] = "primary_endpoint"
    if normalized.get("current_price") is not None:
        normalized["current_price_source"] = "primary_endpoint"

    # Primary endpoint is short-volume style data; extract the latest ratio explicitly.
    raw_data = normalized.get("raw")
    if isinstance(raw_data, dict) and isinstance(raw_data.get("data"), list) and raw_data.get("data"):
        latest_row = raw_data["data"][0] if isinstance(raw_data["data"][0], dict) else {}
        normalized["short_volume_ratio_pct"] = normalize_percent(latest_row.get("shortVolumeRatio"))
        normalized["short_volume_shares"] = as_float(latest_row.get("shortVolume"))
        normalized["total_volume"] = as_float(latest_row.get("totalVolume"))
        normalized["short_volume_date"] = as_text(latest_row.get("marketDate"))
        if normalized.get("short_volume_ratio_pct") is not None and normalized.get("short_interest_pct") is None:
            normalized["short_interest_source"] = "short_volume_proxy_only"

    # Enrich with Fintel ownership/insider endpoints, yfinance stats, and Mboum SI shares/DTC.
    if symbol:
        owners_payload, owners_error = fetch_fintel_owners(symbol, api_key, args.timeout)
        if owners_payload:
            normalized.update(owners_payload)
        elif owners_error:
            normalized["owners_error"] = owners_error

        insider_payload, insider_error = fetch_fintel_insiders(symbol, api_key, args.timeout)
        if insider_payload:
            normalized.update(insider_payload)
        elif insider_error:
            normalized["insider_error"] = insider_error

        yf_payload, yf_error = fetch_yfinance_stats(symbol)
        if yf_payload:
            # Extract current price first (highest priority realtime source)
            if yf_payload.get("current_price") is not None:
                if normalized.get("current_price") is None:
                    normalized["current_price"] = yf_payload.get("current_price")
                    normalized["current_price_source"] = "yfinance_realtime"
            for key in ["float_shares", "shares_outstanding", "institutional_hold_pct", "shares_short", "days_to_cover_yf"]:
                if yf_payload.get(key) is not None:
                    normalized[key] = yf_payload.get(key)
            if normalized.get("short_interest_shares") is None and yf_payload.get("shares_short") is not None:
                normalized["short_interest_shares"] = yf_payload.get("shares_short")
                normalized["short_interest_shares_source"] = "yfinance"
            if normalized.get("days_to_cover") is None and yf_payload.get("days_to_cover_yf") is not None:
                normalized["days_to_cover"] = yf_payload.get("days_to_cover_yf")
            if normalized.get("insider_hold_pct") is None and yf_payload.get("insider_hold_pct_yf") is not None:
                normalized["insider_hold_pct"] = yf_payload.get("insider_hold_pct_yf")
            if normalized.get("short_interest_pct") is None and yf_payload.get("shares_short") is not None and yf_payload.get("float_shares") not in (None, 0):
                normalized["short_interest_pct"] = (float(yf_payload["shares_short"]) / float(yf_payload["float_shares"])) * 100.0
                normalized["short_interest_source"] = "yfinance_derived"
        elif yf_error:
            normalized["yfinance_error"] = yf_error

        mboum_si_payload, mboum_si_error = fetch_mboum_short_interest(symbol, args.timeout)
        if mboum_si_payload:
            normalized.update(mboum_si_payload)
            if normalized.get("short_interest_pct") is None and mboum_si_payload.get("short_interest_shares") is not None and normalized.get("float_shares") not in (None, 0):
                normalized["short_interest_pct"] = (float(mboum_si_payload["short_interest_shares"]) / float(normalized["float_shares"])) * 100.0
                normalized["short_interest_source"] = "mboum_v2_plus_yfinance_float"
            if normalized.get("days_to_cover") is None and normalized.get("days_to_cover_yf") is not None:
                normalized["days_to_cover"] = normalized.get("days_to_cover_yf")
        elif mboum_si_error:
            normalized["mboum_si_error"] = mboum_si_error

    secondary_base_url = (os.getenv("FINTEL_SHORT_INTEREST_API_URL") or "").strip()
    if secondary_base_url:
        if "REPLACE_WITH_YOUR_PATH" in secondary_base_url:
            normalized["short_interest_error"] = "secondary_endpoint_placeholder"
        else:
            secondary_url = build_request_url(secondary_base_url, symbol)
            normalized["short_interest_endpoint"] = secondary_url

            secondary_payload, secondary_error = fetch_json_payload(
                secondary_url,
                args.timeout,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "X-API-Key": api_key,
                },
            )
            if secondary_error:
                normalized["short_interest_error"] = secondary_error
            else:
                secondary_short_interest_pct = extract_short_interest_pct(secondary_payload)
                secondary_short_volume_ratio_pct = extract_short_volume_ratio_pct(secondary_payload)
                normalized["short_interest_pct_secondary"] = secondary_short_interest_pct
                normalized["short_volume_ratio_pct_secondary"] = secondary_short_volume_ratio_pct
                if secondary_short_interest_pct is not None:
                    normalized["short_interest_pct"] = secondary_short_interest_pct
                    normalized["short_interest_source"] = "secondary_endpoint"
                    normalized["short_interest_proxy"] = False
                    normalized.pop("short_interest_error", None)
                elif normalized.get("short_interest_pct") is None and secondary_short_volume_ratio_pct is not None:
                    normalized["short_interest_pct"] = secondary_short_volume_ratio_pct
                    normalized["short_interest_source"] = "secondary_short_volume_ratio_proxy"
                    normalized["short_interest_proxy"] = True
                    normalized.pop("short_interest_error", None)
                elif normalized.get("short_interest_pct") is None:
                    normalized["short_interest_error"] = "secondary_value_missing"

    price_base_url = (os.getenv("FINTEL_PRICE_API_URL") or "").strip()
    if price_base_url:
        if "REPLACE_WITH_YOUR_PATH" in price_base_url:
            normalized["current_price_error"] = "price_endpoint_placeholder"
        else:
            price_url = build_request_url(price_base_url, symbol)
            normalized["current_price_endpoint"] = price_url

            price_payload, price_error = fetch_json_payload(
                price_url,
                args.timeout,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "X-API-Key": api_key,
                },
            )
            if price_error:
                normalized["current_price_error"] = price_error
            else:
                fintel_price = extract_current_price(price_payload)
                normalized["current_price_secondary"] = fintel_price
                if fintel_price is not None:
                    normalized["current_price"] = fintel_price
                    normalized["current_price_source"] = "price_endpoint"
                    normalized.pop("current_price_error", None)
                elif normalized.get("current_price") is None:
                    normalized["current_price_error"] = "price_value_missing"

    mboum_key = (os.getenv("MBOUM_KEY") or "").strip()
    if symbol and mboum_key:
        mboum_url = build_mboum_price_url(symbol)
        normalized["mboum_price_endpoint"] = mboum_url  # key is in Authorization header, not URL

        mboum_payload, mboum_error = fetch_json_payload(
            mboum_url, args.timeout,
            headers={"Authorization": f"Bearer {mboum_key}"},
        )
        if mboum_error:
            normalized["mboum_price_error"] = mboum_error
        else:
            mboum_price, mboum_detail = extract_mboum_meta_price(mboum_payload)
            normalized["mboum_current_price"] = mboum_price
            normalized["mboum_price_source"] = mboum_detail or "mboum"
            if mboum_price is not None:
                if normalized.get("current_price") is None:
                    normalized["current_price"] = mboum_price
                    normalized["current_price_source"] = "mboum_realtime"
                    normalized.pop("current_price_error", None)
                else:
                    primary_price = as_float(normalized.get("current_price"))
                    if primary_price is not None:
                        normalized["current_price_cross_ref"] = mboum_price
                        normalized["current_price_cross_ref_source"] = "mboum"
                        if primary_price != 0:
                            normalized["current_price_cross_ref_diff_pct"] = (
                                (mboum_price - primary_price) / primary_price
                            ) * 100.0
                normalized.pop("mboum_price_error", None)
            else:
                normalized["mboum_price_error"] = "price_value_missing"

    massive_key = (os.getenv("MASSIVE_KEY") or os.getenv("POLYGON_API_KEY") or "").strip()
    if symbol and massive_key:
        massive_headers = {
            "Authorization": f"Bearer {massive_key}",
            "X-API-Key": massive_key,
        }
        for massive_source, base_massive_url in build_massive_price_urls(symbol):
            massive_url = upsert_query_param(base_massive_url, "apiKey", massive_key)
            normalized["massive_price_endpoint"] = redact_query_param(massive_url, "apiKey")

            massive_payload, massive_error = fetch_json_payload(massive_url, args.timeout, headers=massive_headers)
            if massive_error:
                normalized["massive_price_error"] = massive_error
                continue

            massive_price, massive_detail = extract_massive_current_price(massive_payload)
            normalized["massive_current_price"] = massive_price
            normalized["massive_current_price_detail"] = massive_detail
            normalized["massive_price_source"] = massive_source
            if massive_price is not None:
                if normalized.get("current_price") is None:
                    normalized["current_price"] = massive_price
                    normalized["current_price_source"] = f"{massive_source}_backup"
                    normalized.pop("current_price_error", None)
                else:
                    primary_price = as_float(normalized.get("current_price"))
                    if primary_price is not None:
                        normalized["current_price_cross_ref"] = massive_price
                        normalized["current_price_cross_ref_source"] = massive_source
                        if primary_price != 0:
                            normalized["current_price_cross_ref_diff_pct"] = ((massive_price - primary_price) / primary_price) * 100.0
                normalized.pop("massive_price_error", None)
                break

            normalized["massive_price_error"] = "price_value_missing"

    if not args.no_write:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(normalized, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(normalized, ensure_ascii=True))
    else:
        print(format_text_report(normalized, None if args.no_write else args.output))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

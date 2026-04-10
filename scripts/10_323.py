#!/usr/bin/env python3
"""10-323: Options CSP/Call/Put strategy evaluator with full Greeks engine.

Full-featured engine: IV smile interpolation, Black-Scholes Greeks with
finite-difference verification, Monte Carlo projections, technical
indicators (SMA/MACD/RSI/BB/ATR), and CSP + Long Call + Long Put recommendations.

Uses paid APIs prioritized: MBOUM → Fintel (paid) → yfinance fallback for spot pricing and option chain data.

Examples:
  --query \"BMNU\"                                    # full analysis from live data
  --query \"AAPL\"                                    # full analysis for any ticker
  --query \"ticker=AAPL spot=190 strike=200 premium=3.1 dte=30 mode=call\"
  --query \"BMNU strike=2.5 premium=0.10 dte=30\"   # spot auto-fetched
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import warnings
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib import error as urlerror
from urllib import request as urlrequest

warnings.filterwarnings("ignore")

ROOT = Path(os.environ.get("HERMES_10_CODES_ROOT", Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(ROOT / "scripts"))

from env_utils import load_workspace_env
from api_config import resolve_api_key

load_workspace_env(ROOT)
load_workspace_env(ROOT.parent)

try:
    import numpy as np
    from scipy.stats import norm as sp_norm
    from scipy.interpolate import interp1d
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False

try:
    import yfinance as yf
except Exception:
    yf = None


# ─── Known leveraged / high-vol ETF tickers for risk warnings ─────────────
_LEVERAGED_ETFS = {
    "BMNU", "BITU", "BITX", "CONL", "SBIT", "TQQQ", "SQQQ",
    "UVXY", "SVXY", "SPXU", "UPRO", "LABU", "LABD", "NUGT",
    "DUST", "JNUG", "FNGU", "FNGD", "SOXL", "SOXS",
}


@dataclass
class StrategyInputs:
    ticker: str
    spot: float
    strike: float
    premium: float
    dte: int
    mode: str = "call"               # "call", "csp", "put", or "both"
    yield_bias: str = "auto"         # "seller", "buyer", or "auto"
    spot_source: str = "default"     # "query", "mboum", "yfinance", or "default"
    premium_source: str = "default"  # "query", "chain", or "default"
    strike_source: str = "default"   # "query", "chain", or "default"
    chain_expiry: str = ""           # expiry date used from options chain
    iv_mid: Optional[float] = None   # implied vol from chain (mid of ATM call+put)
    _cached_chain: Any = field(default=None, repr=False)  # avoid double fetch


@dataclass
class GreeksResult:
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    rho: float = 0.0


@dataclass
class TradeRecommendation:
    action: str              # "SELL CASH-SECURED PUT", "BUY CALL", or "BUY PUT"
    strike: float
    premium: float
    pop: Optional[float]     # probability of profit %
    breakeven: float
    smile_iv: Optional[float]
    greeks: GreeksResult
    fd_greeks: Optional[GreeksResult]  # finite-diff verified
    max_loss: Optional[float] = None
    cash_required: Optional[float] = None
    volume: int = 0
    open_interest: int = 0
    ann_yield_pct: Optional[float] = None  # seller yield (credit/collateral annualized)


@dataclass
class TechnicalSignals:
    trend: str = "UNKNOWN"
    momentum: str = "UNKNOWN"
    vol_regime: str = "UNKNOWN"
    recent_return_30d: float = 0.0
    hist_vol_30d: Optional[float] = None  # annualized 30d historical vol
    mc_expected: Optional[float] = None
    mc_lower_15: Optional[float] = None
    mc_upper_85: Optional[float] = None


@dataclass
class StrategyOutput:
    code: str
    ticker: str
    mode: str
    verdict: str
    rationale: str
    probability_otm: float
    breakeven: float
    max_profit_per_share: float
    max_loss_per_share: float
    annualized_yield_pct: float
    assignment_risk: str
    signals: Optional[TechnicalSignals] = None
    csp_rec: Optional[TradeRecommendation] = None
    call_rec: Optional[TradeRecommendation] = None
    put_rec: Optional[TradeRecommendation] = None
    is_leveraged_etf: bool = False


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _extract_float(query: str, keys: list[str]) -> Optional[float]:
    for key in keys:
        m = re.search(rf"(?:^|\\s){re.escape(key)}\\s*=\\s*([-+]?\\d+(?:\\.\\d+)?)", query, flags=re.IGNORECASE)
        if m:
            return float(m.group(1))
    return None


def _extract_int(query: str, keys: list[str]) -> Optional[int]:
    value = _extract_float(query, keys)
    if value is None:
        return None
    return int(round(value))


def _extract_text(query: str, keys: list[str]) -> Optional[str]:
    for key in keys:
        m = re.search(rf"(?:^|\\s){re.escape(key)}\\s*=\\s*([A-Za-z0-9_\\-\\.] )", query, flags=re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def _fallback_ticker(query: str) -> str:
    tokens = re.findall(r"\\b[A-Z]{{1,5}}\\b", query.upper())
    for tok in tokens:
        if tok not in {"CSP", "CALL", "PUT", "MODE", "DTE", "SPOT", "STRIKE", "PREMIUM", "TICKER", "BOTH"}:
            return tok
    return "UNKNOWN"


# BLACK-SCHOLES GREEKS ENGINE
def _bs_greeks(S: float, K: float, T: float, r: float, sigma: float,
               q: float = 0.0, option_type: str = "call") -> GreeksResult:
    if T <= 0 or sigma <= 0:
        return GreeksResult(delta=1.0 if option_type == "call" else 0.0)

    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT

    if HAS_SCIPY:
        cdf = sp_norm.cdf
        pdf_val = sp_norm.pdf
    else:
        def cdf(x):
            return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
        def pdf_val(x):
            return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)

    eq = math.exp(-q * T)
    er = math.exp(-r * T)

    if option_type == "call":
        delta = eq * cdf(d1)
        theta = (-(S * eq * pdf_val(d1) * sigma) / (2 * sqrtT)
                 - r * K * er * cdf(d2) + q * S * eq * cdf(d1))
        rho = K * T * er * cdf(d2)
    else:
        delta = -eq * cdf(-d1)
        theta = (-(S * eq * pdf_val(d1) * sigma) / (2 * sqrtT)
                 + r * K * er * cdf(-d2) - q * S * eq * cdf(-d1))
        rho = -K * T * er * cdf(-d2)

    gamma = eq * pdf_val(d1) / (S * sigma * sqrtT)
    vega = S * eq * pdf_val(d1) * sqrtT

    return GreeksResult(
        delta=round(delta, 4),
        gamma=round(gamma, 4),
        theta=round(theta / 365, 3),   # per-day
        vega=round(vega / 100, 3),     # per 1% move
        rho=round(rho / 100, 3),
    )


def _finite_diff_greeks(S: float, K: float, T: float, r: float, sigma: float,
                        q: float = 0.0, option_type: str = "call",
                        h: float = 0.001) -> GreeksResult:
    base = _bs_greeks(S, K, T, r, sigma, q, option_type)
    up = _bs_greeks(S + h, K, T, r, sigma, q, option_type).delta
    dn = _bs_greeks(S - h, K, T, r, sigma, q, option_type).delta
    delta_num = (up - dn) / (2 * h)
    gamma_num = (up - 2 * base.delta + dn) / (h ** 2)
    v_up = _bs_greeks(S, K, T, r, sigma + 0.01, q, option_type).delta
    v_dn = _bs_greeks(S, K, T, r, sigma - 0.01, q, option_type).delta
    vega_num = (v_up - v_dn) / 0.02 * 100
    return GreeksResult(
        delta=round(delta_num, 4),
        gamma=round(gamma_num, 4),
        vega=round(vega_num / 100, 3),
    )


def _build_iv_smile(chain_opts: list[dict], spot: float) -> Optional[Any]:
    pairs = []
    for opt in chain_opts:
        iv = opt.get("iv")
        strike = opt.get("strike")
        if iv is not None and strike is not None and iv > 0 and spot > 0:
            pairs.append((strike / spot, iv))
    if len(pairs) < 2:
        return None
    pairs.sort(key=lambda p: p[0])
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    if HAS_SCIPY:
        return interp1d(xs, ys, kind="linear", fill_value=(ys[0], ys[-1]), bounds_error=False)
    def _lerp(moneyness: float) -> float:
        if moneyness <= xs[0]:
            return ys[0]
        if moneyness >= xs[-1]:
            return ys[-1]
        for i in range(len(xs) - 1):
            if xs[i] <= moneyness <= xs[i + 1]:
                t = (moneyness - xs[i]) / (xs[i + 1] - xs[i])
                return ys[i] + t * (ys[i + 1] - ys[i])
        return ys[-1]
    return _lerp


def _smile_iv_at(iv_smile, strike: float, spot: float, fallback: float = 0.40) -> float:
    if iv_smile is None or spot <= 0:
        return fallback
    try:
        v = float(iv_smile(strike / spot))
        return v if v > 0 else fallback
    except Exception:
        return fallback


def _compute_technicals(ticker: str) -> Optional[TechnicalSignals]:
    if yf is None:
        return None
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="6mo")
        if hist.empty or len(hist) < 30:
            return None
    except Exception:
        return None

    close = hist["Close"]
    sig = TechnicalSignals()

    # SMA / MACD
    sma20 = close.rolling(20).mean()
    sma50 = close.rolling(50).mean()
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    macd_signal = macd.ewm(span=9, adjust=False).mean()

    # RSI
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
    rs = gain / loss.replace(0, 1e-9)
    rsi = 100 - 100 / (1 + rs)

    # Bollinger Bands
    bb_mid = close.rolling(20).mean()
    bb_std = close.rolling(20).std()
    bb_upper = bb_mid + 2 * bb_std
    bb_lower = bb_mid - 2 * bb_std

    latest_close = float(close.iloc[-1])
    latest_sma50 = float(sma50.iloc[-1]) if not math.isnan(float(sma50.iloc[-1])) else latest_close
    latest_macd = float(macd.iloc[-1]) if not math.isnan(float(macd.iloc[-1])) else 0.0
    latest_macd_sig = float(macd_signal.iloc[-1]) if not math.isnan(float(macd_signal.iloc[-1])) else 0.0
    latest_rsi = float(rsi.iloc[-1]) if not math.isnan(float(rsi.iloc[-1])) else 50.0
    latest_bb_upper = float(bb_upper.iloc[-1]) if not math.isnan(float(bb_upper.iloc[-1])) else latest_close * 1.1
    latest_bb_lower = float(bb_lower.iloc[-1]) if not math.isnan(float(bb_lower.iloc[-1])) else latest_close * 0.9

    # Trend
    if latest_close > latest_sma50 and latest_macd > latest_macd_sig:
        sig.trend = "BULLISH"
    else:
        sig.trend = "BEARISH/NEUTRAL"

    # Momentum
    if 55 < latest_rsi < 70:
        sig.momentum = "STRONG"
    elif latest_rsi < 40:
        sig.momentum = "WEAK"
    else:
        sig.momentum = "NEUTRAL"

    # Vol regime
    if latest_close > latest_bb_upper:
        sig.vol_regime = "EXPANSION"
    elif latest_close < latest_bb_lower:
        sig.vol_regime = "CONTRACTED"
    else:
        sig.vol_regime = "NORMAL"

    # 30d return
    if len(close) >= 30:
        sig.recent_return_30d = round((float(close.iloc[-1]) / float(close.iloc[-30]) - 1) * 100, 1)

    # Monte Carlo 30-day projection
    try:
        hist_vol = float(close.pct_change().std()) * math.sqrt(252)
        sig.hist_vol_30d = round(hist_vol, 4)
        iv_approx = 1.5 * hist_vol
        daily_vol = iv_approx / math.sqrt(252)
        if HAS_SCIPY:
            rng = np.random.default_rng()
            draws = rng.normal(0, daily_vol, (10000, 30))
            sim_prices = latest_close * np.exp(np.cumsum(draws, axis=1)[:, -1])
            sig.mc_expected = round(float(np.mean(sim_prices)), 2)
            sig.mc_lower_15 = round(float(np.percentile(sim_prices, 15)), 2)
            sig.mc_upper_85 = round(float(np.percentile(sim_prices, 85)), 2)
        else:
            # Simple log-normal estimate without numpy
            import random
            sims = []
            for _ in range(5000):
                cum = sum(random.gauss(0, daily_vol) for _ in range(30))
                sims.append(latest_close * math.exp(cum))
            sims.sort()
            sig.mc_expected = round(sum(sims) / len(sims), 2)
            sig.mc_lower_15 = round(sims[int(0.15 * len(sims))], 2)
            sig.mc_upper_85 = round(sims[int(0.85 * len(sims))], 2)
    except Exception:
        pass

    return sig


def _fetch_spot_mboum(ticker: str) -> tuple[Optional[float], str]:
    if ticker in {"UNKNOWN", ""}:
        return None, "missing_symbol"
    try:
        mboum_key, _ = resolve_api_key("mboum")
    except KeyError:
        return None, "mboum_key_missing"
    url = f"https://api.mboum.com/v1/markets/stock/history?symbol={ticker}&interval=1d&diffandsplits=false"
    req = urlrequest.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "OpenClaw-10-323/1.0",
        "Authorization": f"Bearer {mboum_key}",
    })
    try:
        with urlrequest.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        price = data.get("meta", {}).get("regularMarketPrice")
        if price is not None:
            return float(price), "mboum"
        return None, "mboum_no_price"
    except urlerror.HTTPError as exc:
        return None, f"mboum_http:{exc.code}"
    except Exception as exc:
        return None, f"mboum_err:{exc.__class__.__name__}"


def _fetch_spot_fintel(ticker: str) -> tuple[Optional[float], str]:
    if ticker in {"UNKNOWN", ""}:
        return None, "missing_symbol"
    try:
        fintel_key, _ = resolve_api_key("fintel")
    except KeyError:
        return None, "fintel_key_missing"
    url = f"https://api.fintel.io/web/v/0.0/so/us/{ticker}"
    req = urlrequest.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "OpenClaw-10-323/1.0",
        "X-API-Key": fintel_key,
    })
    try:
        with urlrequest.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        price = data.get("lastPrice") or data.get("price") or data.get("close")
        if price is not None:
            return float(price), "fintel"
        return None, "fintel_no_price"
    except urlerror.HTTPError as exc:
        return None, f"fintel_http:{exc.code}"
    except Exception as exc:
        return None, f"fintel_err:{exc.__class__.__name__}"


def _fetch_spot_yfinance(ticker: str) -> Optional[float]:
    if ticker in {"UNKNOWN", ""} or yf is None:
        return None
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="1d")
        if hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception:
        return None


def _fetch_spot(ticker: str) -> tuple[float, str]:
    price, tag = _fetch_spot_mboum(ticker)
    if price is not None and price > 0:
        return price, "mboum"
    price, tag = _fetch_spot_fintel(ticker)
    if price is not None and price > 0:
        return price, "fintel"
    yf_price = _fetch_spot_yfinance(ticker)
    if yf_price is not None and yf_price > 0:
        return yf_price, "yfinance"
    return 100.0, "default"


def main():
    args = parse_args()
    inputs = parse_inputs(args.query)
    result = evaluate(inputs)
    if args.json:
        out = asdict(result)
        out["telegram_message"] = format_telegram(result, inputs)
        print(json.dumps(out, indent=2, ensure_ascii=True, default=str))
    else:
        print(format_telegram(result, inputs))
    return 0


if __name__ == "__main__":
    sys.exit(main())

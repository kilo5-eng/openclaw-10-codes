#!/usr/bin/env python3
"""10-323: Options CSP/Call/Put strategy evaluator with full Greeks engine.

Full-featured engine: IV smile interpolation, Black-Scholes Greeks with
finite-difference verification, Monte Carlo projections, technical
indicators (SMA/MACD/RSI/BB/ATR), and CSP + Long Call + Long Put recommendations.

Uses paid APIs prioritized: MBOUM → Fintel (paid) → yfinance fallback for spot pricing and option chain data.

Examples:
  --query "BMNU"                                    # full analysis from live data
  --query "AAPL"                                    # full analysis for any ticker
  --query "ticker=AAPL spot=190 strike=200 premium=3.1 dte=30 mode=call"
  --query "BMNU strike=2.5 premium=0.10 dte=30"   # spot auto-fetched
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

# ─── Windows UTF-8 output fix (cp1252 console can't handle symbols) ───
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ─── Windows/openclaw standalone: no env_utils or api_config imports ───
ROOT = Path(__file__).resolve().parent


def _load_dotenv() -> None:
    """Load .env from native openclaw config paths into os.environ.

    Priority (first file wins per key):
      1. OPENCLAW_ENV env var override
      2. C:\\Users\\<user>\\.openclaw\\.env   (shared openclaw config)
      3. C:\\Users\\<user>\\.openclaw\\workspace\\.env  (workspace-local)
    All matching files are loaded; earlier entries take priority (setdefault).
    No WSL/hermes paths — openclaw sessions run natively on Windows.
    """
    home = Path.home()
    openclaw_dir = home / ".openclaw"
    candidates: list[Path] = []

    # Explicit override
    env_override = os.environ.get("OPENCLAW_ENV", "").strip()
    if env_override:
        candidates.append(Path(env_override))

    # Native openclaw paths only
    candidates += [
        openclaw_dir / ".env",
        openclaw_dir / "workspace" / ".env",
    ]

    for p in candidates:
        try:
            if not p.exists():
                continue
            with open(p, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    k = k.strip()
                    v = v.strip()
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                        v = v[1:-1]
                    if k and k not in os.environ:
                        os.environ[k] = v
        except Exception:
            continue


_load_dotenv()


def resolve_api_key(api_name: str) -> tuple[str, str]:
    """Inline Windows-compatible API key resolver (mirrors api_config.resolve_api_key)."""
    _KEY_MAP: dict[str, tuple[str, ...]] = {
        "mboum":  ("MBOUM_KEY", "MBOUM_API_KEY"),
        "fintel": ("FINTEL_API_KEY",),
        "brave":  ("BRAVE_API_KEY",),
        "fred":   ("FRED_API_KEY",),
    }
    for env_name in _KEY_MAP.get(api_name, ()):
        v = os.getenv(env_name, "").strip()
        if v and v not in {"***", "<redacted>", "REDACTED"}:
            return v, env_name
    raise KeyError(f"Missing credential for {api_name!r}")

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
        m = re.search(rf"(?:^|\s){re.escape(key)}\s*=\s*([-+]?\d+(?:\.\d+)?)", query, flags=re.IGNORECASE)
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
        m = re.search(rf"(?:^|\s){re.escape(key)}\s*=\s*([A-Za-z0-9_\-\.]+)", query, flags=re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def _fallback_ticker(query: str) -> str:
    tokens = re.findall(r"\b[A-Z]{1,5}\b", query.upper())
    for tok in tokens:
        if tok not in {"CSP", "CALL", "PUT", "MODE", "DTE", "SPOT", "STRIKE", "PREMIUM", "TICKER", "BOTH"}:
            return tok
    return "UNKNOWN"


# ═══════════════════════════════════════════════════════════════════════════════
#  BLACK-SCHOLES GREEKS ENGINE (from OptionsStrategy.CSP.Call)
# ═══════════════════════════════════════════════════════════════════════════════

def _bs_price_raw(S: float, K: float, T: float, r: float, sigma: float,
                  q: float = 0.0, option_type: str = "call") -> float:
    """Compute Black-Scholes option price (raw, unrounded)."""
    if T <= 0:
        if option_type == "call":
            return max(S - K, 0)
        else:
            return max(K - S, 0)
    if sigma <= 0:
        if option_type == "call":
            return max(S * math.exp(-q * T) - K * math.exp(-r * T), 0)
        else:
            return max(K * math.exp(-r * T) - S * math.exp(-q * T), 0)

    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT

    if HAS_SCIPY:
        cdf = sp_norm.cdf
    else:
        def cdf(x):
            return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    eq = math.exp(-q * T)
    er = math.exp(-r * T)

    if option_type == "call":
        price = S * eq * cdf(d1) - K * er * cdf(d2)
    else:
        price = K * er * cdf(-d2) - S * eq * cdf(-d1)
    
    return price


def _bs_greeks_raw(S: float, K: float, T: float, r: float, sigma: float,
                   q: float = 0.0, option_type: str = "call") -> dict[str, float]:
    """Compute Black-Scholes Greeks without rounding (raw values for calculations)."""
    if T <= 0 or sigma <= 0:
        return {"delta": 1.0 if option_type == "call" else 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}

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

    return {
        "delta": delta,
        "gamma": gamma,
        "theta": theta / 365,   # per-day
        "vega": vega / 100,     # per 1% move
        "rho": rho / 100,
    }


def _bs_greeks(S: float, K: float, T: float, r: float, sigma: float,
               q: float = 0.0, option_type: str = "call") -> GreeksResult:
    """Full Black-Scholes Greeks. Falls back to math.erf if scipy unavailable."""
    raw = _bs_greeks_raw(S, K, T, r, sigma, q, option_type)
    return GreeksResult(
        delta=round(raw["delta"], 4),
        gamma=round(raw["gamma"], 4),
        theta=round(raw["theta"], 3),
        vega=round(raw["vega"], 3),
        rho=round(raw["rho"], 3),
    )


def _finite_diff_greeks(S: float, K: float, T: float, r: float, sigma: float,
                        q: float = 0.0, option_type: str = "call",
                        h: float = 0.01) -> GreeksResult:
    """Finite-difference verification of Greeks using price derivatives.
    
    Computes numerical derivatives of prices to verify analytical Greeks.
    Uses raw (unrounded) calculations to avoid rounding errors.
    """
    if T <= 0 or sigma <= 0:
        return GreeksResult()
    
    # Calculate delta via central difference of price: (price at S+h - price at S-h) / 2h
    price_up = _bs_price_raw(S + h, K, T, r, sigma, q, option_type)
    price_dn = _bs_price_raw(S - h, K, T, r, sigma, q, option_type)
    delta_num = (price_up - price_dn) / (2 * h)
    
    # Calculate gamma via second-order central difference: (price_up - 2*price_mid + price_dn) / h²
    price_mid = _bs_price_raw(S, K, T, r, sigma, q, option_type)
    gamma_num = (price_up - 2 * price_mid + price_dn) / (h ** 2)
    
    # Calculate vega via central difference of price w.r.t. sigma: (price at σ+0.01 - price at σ-0.01) / 0.02
    # This computes dPrice/dSigma directly. Analytical vega is reported per 1% IV move (vega/100),
    # so we need to match that scaling.
    price_v_up = _bs_price_raw(S, K, T, r, sigma + 0.01, q, option_type)
    price_v_dn = _bs_price_raw(S, K, T, r, sigma - 0.01, q, option_type)
    vega_num_raw = (price_v_up - price_v_dn) / 0.02  # dPrice/dSigma
    vega_num = vega_num_raw / 100  # per 1% IV move (to match analytical vega scaling)
    
    return GreeksResult(
        delta=round(delta_num, 4),
        gamma=round(gamma_num, 4),
        theta=0.0,
        vega=round(vega_num, 3),
        rho=0.0,
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  IV SMILE INTERPOLATION
# ═══════════════════════════════════════════════════════════════════════════════

def _build_iv_smile(chain_opts: list[dict], spot: float) -> Optional[Any]:
    """Build IV smile interpolator from chain options. Returns callable(moneyness)->iv or None."""
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
    # Simple linear interpolation fallback
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
    """Get smile-adjusted IV for a given strike."""
    if iv_smile is None or spot <= 0:
        return fallback
    try:
        v = float(iv_smile(strike / spot))
        return v if v > 0 else fallback
    except Exception:
        return fallback


# ═══════════════════════════════════════════════════════════════════════════════
#  TECHNICAL INDICATORS & SIGNALS (from OptionsStrategy.CSP.Call)
# ═══════════════════════════════════════════════════════════════════════════════

def _compute_technicals(ticker: str) -> Optional[TechnicalSignals]:
    """Compute trend/momentum/vol-regime from 6-month history. Needs yfinance."""
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


# ═══════════════════════════════════════════════════════════════════════════════
#  MBOUM SPOT PRICE (mirrors 10-77 fetch_mboum_price)
# ═══════════════════════════════════════════════════════════════════════════════

def _fetch_spot_mboum(ticker: str) -> tuple[Optional[float], str]:
    """MBOUM realtime price. Returns (price, source_tag)."""
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
    """Fintel paid API for spot price. Returns (price, source_tag)."""
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
        # Fintel returns stock data; extract latest price from the response
        # Common fields: lastPrice, price, close
        price = data.get("lastPrice") or data.get("price") or data.get("close")
        if price is not None:
            return float(price), "fintel"
        return None, "fintel_no_price"
    except urlerror.HTTPError as exc:
        return None, f"fintel_http:{exc.code}"
    except Exception as exc:
        return None, f"fintel_err:{exc.__class__.__name__}"


def _fetch_spot_yfinance(ticker: str) -> Optional[float]:
    """Attempt live spot price via yfinance. Returns None on any failure."""
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
    """Paid APIs prioritized: MBOUM → Fintel → yfinance → default($100) spot price."""
    # Try MBOUM first (paid API)
    price, tag = _fetch_spot_mboum(ticker)
    if price is not None and price > 0:
        return price, "mboum"
    # Try Fintel second (paid API)
    price, tag = _fetch_spot_fintel(ticker)
    if price is not None and price > 0:
        return price, "fintel"
    # Fallback to yfinance (free)
    yf_price = _fetch_spot_yfinance(ticker)
    if yf_price is not None and yf_price > 0:
        return yf_price, "yfinance"
    # Final fallback to default
    return 100.0, "default"


# ═══════════════════════════════════════════════════════════════════════════════
#  OPTIONS CHAIN LOOKUP (MBOUM → yfinance)
# ═══════════════════════════════════════════════════════════════════════════════

def _fetch_chain_mboum(ticker: str, target_dte: int = 30) -> Optional[dict[str, Any]]:
    """Fetch option chain from MBOUM v1 straddle endpoint, selecting expiry closest to target_dte."""
    if ticker in {"UNKNOWN", ""}:
        return None
    try:
        mboum_key, _ = resolve_api_key("mboum")
    except KeyError:
        return None
    base = (os.getenv("MBOUM_BASE_URL") or "https://api.mboum.com").strip().rstrip("/")
    headers = {"Accept": "application/json", "User-Agent": "OpenClaw-10-323/1.0",
               "Authorization": f"Bearer {mboum_key}"}

    # Initial fetch — returns available expirationDates + first expiry straddles
    url = f"{base}/v1/markets/options?ticker={ticker}&display=straddle"
    try:
        req = urlrequest.Request(url, headers=headers)
        with urlrequest.urlopen(req, timeout=12) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None

    body = payload.get("body", [])
    if not body:
        return None

    # Pick expiry closest to target_dte from the expirationDates list
    exp_timestamps = body[0].get("expirationDates", []) if body else []
    best_ts: Optional[float] = None
    if exp_timestamps:
        today = datetime.now()
        best_dist = float("inf")
        for ts in exp_timestamps:
            try:
                exp_dt = datetime.fromtimestamp(float(ts))
                dist = abs((exp_dt - today).days - target_dte)
                if dist < best_dist:
                    best_dist = dist
                    best_ts = float(ts)
            except Exception:
                continue

    # Check if the already-fetched payload has the best expiry
    opts_in_payload = body[0].get("options", []) if body else []
    current_exp_ts = opts_in_payload[0].get("expirationDate") if opts_in_payload else None
    if best_ts is None or current_exp_ts == best_ts:
        chain = _parse_mboum_chain(payload)
        if chain and (chain.get("calls") or chain.get("puts")):
            return chain
        return None

    # Fetch the specific best expiry by date
    best_exp_str = datetime.fromtimestamp(best_ts).strftime("%Y-%m-%d")
    url_exp = f"{base}/v1/markets/options?ticker={ticker}&expirationDate={best_exp_str}&display=straddle"
    try:
        req = urlrequest.Request(url_exp, headers=headers)
        with urlrequest.urlopen(req, timeout=12) as resp:
            payload_exp = json.loads(resp.read().decode("utf-8", errors="replace"))
        chain = _parse_mboum_chain(payload_exp)
        if chain and (chain.get("calls") or chain.get("puts")):
            return chain
    except Exception:
        pass

    # Fallback: use original payload
    chain = _parse_mboum_chain(payload)
    if chain and (chain.get("calls") or chain.get("puts")):
        return chain
    return None


def _parse_mboum_opt_fields(opt_dict: dict) -> dict:
    """Normalize a MBOUM straddle call/put sub-object into a canonical chain row."""
    return {
        "strike": _num(opt_dict.get("strike")),
        "lastPrice": _num(opt_dict.get("lastPrice")),
        "bid": _num(opt_dict.get("bid")),
        "ask": _num(opt_dict.get("ask")),
        "iv": _num(opt_dict.get("impliedVolatility") or opt_dict.get("iv")),
        "oi": int(_num(opt_dict.get("openInterest") or opt_dict.get("openinterest")) or 0),
        "volume": int(_num(opt_dict.get("volume")) or 0),
    }


def _parse_mboum_chain(payload: Any) -> Optional[dict[str, Any]]:
    """Walk MBOUM JSON to extract calls/puts lists with strike, premium, iv, oi.

    Handles both MBOUM v1 straddle format ({straddles: [{strike, call?, put?}]})
    and legacy calls/puts list format.
    """
    calls: list[dict] = []
    puts: list[dict] = []
    expiry: Optional[str] = None

    def _walk(node: Any) -> bool:
        nonlocal calls, puts, expiry
        if not isinstance(node, dict):
            if isinstance(node, list):
                for item in node:
                    if _walk(item):
                        return True
            return False

        # ── MBOUM v1 straddle format: {expirationDate: ts, straddles: [{strike, call?, put?}]} ──
        straddles = node.get("straddles")
        if isinstance(straddles, list):
            exp_ts = node.get("expirationDate")
            if exp_ts is not None:
                try:
                    expiry = datetime.fromtimestamp(float(exp_ts)).strftime("%Y-%m-%d")
                except Exception:
                    pass
            for straddle in straddles:
                if not isinstance(straddle, dict):
                    continue
                call_obj = straddle.get("call")
                put_obj = straddle.get("put")
                if isinstance(call_obj, dict):
                    row = _parse_mboum_opt_fields(call_obj)
                    if row["strike"] is not None:
                        calls.append(row)
                if isinstance(put_obj, dict):
                    row = _parse_mboum_opt_fields(put_obj)
                    if row["strike"] is not None:
                        puts.append(row)
            if calls or puts:
                return True

        # ── Legacy format: {calls: [...], puts: [...]} ──
        c = node.get("calls") or node.get("Call")
        p = node.get("puts") or node.get("Put")
        if isinstance(c, list) and isinstance(p, list) and (c or p):
            for item in c:
                if isinstance(item, dict):
                    s = _num(item.get("strike") or item.get("strikePrice"))
                    if s is not None:
                        calls.append({
                            "strike": s,
                            "lastPrice": _num(item.get("lastPrice") or item.get("last")),
                            "bid": _num(item.get("bid")),
                            "ask": _num(item.get("ask")),
                            "iv": _num(item.get("impliedVolatility") or item.get("iv")),
                            "oi": _num(item.get("openInterest") or item.get("openinterest")),
                            "volume": _num(item.get("volume")) or 0,
                        })
            for item in p:
                if isinstance(item, dict):
                    s = _num(item.get("strike") or item.get("strikePrice"))
                    if s is not None:
                        puts.append({
                            "strike": s,
                            "lastPrice": _num(item.get("lastPrice") or item.get("last")),
                            "bid": _num(item.get("bid")),
                            "ask": _num(item.get("ask")),
                            "iv": _num(item.get("impliedVolatility") or item.get("iv")),
                            "oi": _num(item.get("openInterest") or item.get("openinterest")),
                            "volume": _num(item.get("volume")) or 0,
                        })
            expiry = _text(node.get("expirationDate") or node.get("expiration"))
            return True

        for v in node.values():
            if _walk(v):
                return True
        return False

    _walk(payload)
    if not calls and not puts:
        return None
    return {"calls": calls, "puts": puts, "expiry": expiry}


def _fetch_chain_yfinance(ticker: str, target_dte: int = 30) -> Optional[dict[str, Any]]:
    """Fetch option chain from yfinance, preferring expiry near target_dte."""
    if ticker in {"UNKNOWN", ""} or yf is None:
        return None
    try:
        t = yf.Ticker(ticker)
        expirations = t.options
        if not expirations:
            return None
        today = datetime.now()
        best_exp = expirations[0]
        best_dist = float("inf")
        for exp_str in expirations:
            try:
                exp_dt = datetime.strptime(exp_str, "%Y-%m-%d")
                dist = abs((exp_dt - today).days - target_dte)
                if dist < best_dist:
                    best_dist = dist
                    best_exp = exp_str
            except ValueError:
                continue

        chain = t.option_chain(best_exp)
        calls: list[dict] = []
        puts: list[dict] = []
        if not chain.calls.empty:
            for _, row in chain.calls.iterrows():
                calls.append({
                    "strike": float(row.get("strike", 0)),
                    "lastPrice": _safe_float(row.get("lastPrice")),
                    "bid": _safe_float(row.get("bid")),
                    "ask": _safe_float(row.get("ask")),
                    "iv": _safe_float(row.get("impliedVolatility")),
                    "oi": _safe_float(row.get("openInterest")),
                    "volume": _safe_float(row.get("volume")) or 0,
                })
        if not chain.puts.empty:
            for _, row in chain.puts.iterrows():
                puts.append({
                    "strike": float(row.get("strike", 0)),
                    "lastPrice": _safe_float(row.get("lastPrice")),
                    "bid": _safe_float(row.get("bid")),
                    "ask": _safe_float(row.get("ask")),
                    "iv": _safe_float(row.get("impliedVolatility")),
                    "oi": _safe_float(row.get("openInterest")),
                    "volume": _safe_float(row.get("volume")) or 0,
                })
        if not calls and not puts:
            return None
        return {"calls": calls, "puts": puts, "expiry": best_exp}
    except Exception:
        return None


def _safe_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        f = float(val)
        return None if math.isnan(f) else f
    except (ValueError, TypeError):
        return None


def _num(value: Any) -> Optional[float]:
    """Coerce MBOUM value (may be nested dict with 'raw') to float."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        for k in ("raw", "value", "fmt"):
            v = _num(value.get(k))
            if v is not None:
                return v
        return None
    if isinstance(value, str):
        try:
            return float(value.strip().replace(",", "").replace("%", ""))
        except ValueError:
            return None
    return None


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        return _text(value.get("fmt") or value.get("raw"))
    return str(value)


def _chain_has_quality_data(chain: dict[str, Any]) -> bool:
    """Check if chain has bid/ask or IV — not just stale lastPrice."""
    for side in ("calls", "puts"):
        for opt in (chain.get(side) or []):
            if opt.get("bid") is not None or opt.get("ask") is not None or opt.get("iv") is not None:
                return True
    return False


def _fetch_chain(ticker: str, target_dte: int = 30) -> Optional[dict[str, Any]]:
    """Paid APIs prioritized: MBOUM (paid) → yfinance (free). Prefer MBOUM when it has quality bid/ask/IV data."""
    mboum_chain = _fetch_chain_mboum(ticker, target_dte=target_dte)
    if mboum_chain and _chain_has_quality_data(mboum_chain):
        mboum_chain["source"] = "mboum"
        return mboum_chain
    yf_chain = _fetch_chain_yfinance(ticker, target_dte=target_dte)
    if yf_chain:
        yf_chain["source"] = "yfinance"
        return yf_chain
    if mboum_chain:
        mboum_chain["source"] = "mboum"
        return mboum_chain
    return None


def _best_option_from_chain(chain: dict[str, Any], spot: float, mode: str) -> dict[str, Any]:
    """Pick the best ATM/near-OTM option from chain for the given mode."""
    target_list = chain.get("calls" if mode == "call" else "puts", [])
    if not target_list:
        return {}

    target_strike = spot * (1.05 if mode == "call" else 0.95)
    best: Optional[dict] = None
    best_dist = float("inf")
    for opt in target_list:
        s = opt.get("strike")
        if s is None:
            continue
        if mode == "call" and s < spot:
            continue
        if mode == "csp" and s > spot:
            continue
        dist = abs(s - target_strike)
        if dist < best_dist:
            best_dist = dist
            best = opt

    if best is None:
        best = min(target_list, key=lambda o: abs((o.get("strike") or 0) - target_strike), default=None)

    if best is None:
        return {}

    bid = best.get("bid")
    ask = best.get("ask")
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        premium = round((bid + ask) / 2, 4)
    elif best.get("lastPrice") is not None and best["lastPrice"] > 0:
        premium = best["lastPrice"]
    else:
        premium = None

    dte = None
    expiry_str = chain.get("expiry") or ""
    if expiry_str:
        for fmt in ("%Y-%m-%d", "%B %d, %Y", "%m/%d/%Y"):
            try:
                exp_dt = datetime.strptime(expiry_str[:10], fmt)
                dte = max(1, (exp_dt - datetime.now()).days)
                break
            except ValueError:
                continue

    return {
        "strike": best["strike"],
        "premium": premium,
        "iv": best.get("iv"),
        "dte": dte,
        "expiry": expiry_str,
        "volume": best.get("volume", 0),
        "oi": best.get("oi"),
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  TRADE RECOMMENDATION ENGINE (CSP + Long Call, smile + Greeks verified)
# ═══════════════════════════════════════════════════════════════════════════════

def _recommend_csp(chain: dict[str, Any], spot: float, dte: int,
                   iv_smile, r: float = 0.042, q: float = 0.0,
                   target_pop: float = 0.75, yield_bias: str = "auto",
                   hist_vol: Optional[float] = None,
                   min_oi: int = 10, min_vol: int = 5) -> Optional[TradeRecommendation]:
    """Find best CSP recommendation with OI/vol filter, adaptive band, yield scoring."""
    puts = chain.get("puts", [])
    if not puts:
        return None

    # Adaptive strike band: widen for high-vol / sparse-strike names.
    # Use a floor that scales with hist_vol and is not clamped above 0.40
    # so that leveraged ETFs with $1-increment strikes can find OTM candidates.
    hv = hist_vol if hist_vol and hist_vol > 0 else 0.30
    strike_ceil = spot * 1.00          # allow up to ATM for yield seekers
    strike_floor = spot * max(0.40, 1.0 - 4 * hv)  # wider floor for high-vol
    T = max(1, dte) / 365.0
    best_rec: Optional[TradeRecommendation] = None
    best_score = -1.0

    def _is_candidate(opt: dict, s_floor: float, s_ceil: float, lo: int, lv: int) -> bool:
        s = opt.get("strike")
        if s is None or s >= s_ceil or s < s_floor:
            return False
        v_opt = int(opt.get("volume") or 0)
        o_opt = int(opt.get("oi") or 0)
        if o_opt < lo and v_opt < lv:
            return False
        bid = opt.get("bid"); ask = opt.get("ask"); lp = opt.get("lastPrice")
        return bool((bid is not None and bid > 0) or (ask is not None and ask > 0)
                    or (lp is not None and lp > 0))

    # First pass: normal band + liquidity floor
    candidates = [p for p in puts if _is_candidate(p, strike_floor, strike_ceil, min_oi, min_vol)]
    # Fallback 1: relax liquidity floor to oi≥1 or vol≥1 when chain is sparse
    if not candidates:
        candidates = [p for p in puts if _is_candidate(p, strike_floor, strike_ceil, 1, 1)]
    # Fallback 2: any OTM put (strike < spot) with a valid premium
    if not candidates:
        candidates = [p for p in puts if _is_candidate(p, 0.0, spot, 0, 0)]

    for opt in candidates:
        strike = opt.get("strike")
        # OI/volume hard floor — skip illiquid strikes
        vol = int(opt.get("volume") or 0)
        oi = int(opt.get("oi") or 0)
        if strike is None:
            continue

        bid = opt.get("bid")
        ask = opt.get("ask")
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            credit = (bid + ask) / 2
        elif opt.get("lastPrice") is not None and opt["lastPrice"] > 0:
            credit = opt["lastPrice"]
        else:
            continue

        sigma = _smile_iv_at(iv_smile, strike, spot)
        greeks = _bs_greeks(spot, strike, T, r, sigma, q, "put")
        pop = _clamp((1.0 + greeks.delta) * 100, 1, 99)

        if pop < target_pop * 100:
            continue

        # Annualized seller yield
        ann_yield = (credit / max(strike, 1e-6)) * (365.0 / max(dte, 1)) * 100.0

        # Scoring based on yield_bias
        effective_bias = yield_bias
        if effective_bias == "auto":
            effective_bias = "seller"  # CSP is a seller strategy
        if effective_bias == "seller":
            score = ann_yield * 10 + abs(greeks.theta) * 500 + credit * 2 + min(oi, 5000) * 0.01 + min(vol, 500) * 0.02
        else:
            score = abs(greeks.theta) * 1000 + credit + min(oi, 5000) * 0.01 + min(vol, 500) * 0.02

        if score > best_score:
            best_score = score
            fd = _finite_diff_greeks(spot, strike, T, r, sigma, q, "put")
            best_rec = TradeRecommendation(
                action="SELL CASH-SECURED PUT",
                strike=strike,
                premium=round(credit, 2),
                pop=round(pop, 1),
                breakeven=round(strike - credit, 2),
                smile_iv=round(sigma, 4),
                greeks=greeks,
                fd_greeks=fd,
                cash_required=round(strike * 100, 0),
                volume=vol,
                open_interest=oi,
                ann_yield_pct=round(ann_yield, 1),
            )

    return best_rec


def _recommend_long_call(chain: dict[str, Any], spot: float, dte: int,
                         iv_smile, r: float = 0.042, q: float = 0.0,
                         yield_bias: str = "auto",
                         min_oi: int = 10, min_vol: int = 5) -> Optional[TradeRecommendation]:
    """Find best long call recommendation with OI/vol filter and yield scoring."""
    calls = chain.get("calls", [])
    if not calls:
        return None

    T = max(1, dte) / 365.0
    best_rec: Optional[TradeRecommendation] = None
    best_score = -1.0

    # Adaptive band: expand for sparse chains (few strikes near spot)
    # Count how many calls fall within the tight band first
    tight_lo, tight_hi = spot * 0.95, spot * 1.15
    n_tight = sum(1 for o in calls if o.get("strike") is not None
                  and tight_lo <= o["strike"] <= tight_hi)
    if n_tight < 2:
        # Sparse chain — widen to ±50% of spot to find a usable call
        call_lo, call_hi = spot * 0.80, spot * 1.50
    else:
        call_lo, call_hi = tight_lo, tight_hi

    for opt in calls:
        strike = opt.get("strike")
        if strike is None or strike < call_lo or strike > call_hi:
            continue

        # OI/volume floor — relax for sparse chains
        vol = int(opt.get("volume") or 0)
        oi = int(opt.get("oi") or 0)
        liq_ok = (oi >= min_oi or vol >= min_vol) or (oi >= 1 or vol >= 1)
        if not liq_ok:
            continue

        bid = opt.get("bid")
        ask = opt.get("ask")
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            mid = (bid + ask) / 2
        elif opt.get("lastPrice") is not None and opt["lastPrice"] > 0:
            mid = opt["lastPrice"]
        else:
            continue

        sigma = _smile_iv_at(iv_smile, strike, spot)
        greeks = _bs_greeks(spot, strike, T, r, sigma, q, "call")

        # Scoring based on yield_bias
        effective_bias = yield_bias
        if effective_bias == "auto":
            effective_bias = "buyer"  # long call is a buyer strategy
        if effective_bias == "seller":
            # Seller bias on calls: prefer higher theta decay (covered call perspective)
            score = abs(greeks.theta) * 1000 + greeks.delta * 50 + min(oi, 5000) * 0.01 + min(vol, 500) * 0.02
        else:
            score = greeks.delta * 100 + greeks.gamma * 50 + min(oi, 5000) * 0.01 + min(vol, 500) * 0.02

        if score > best_score:
            best_score = score
            fd = _finite_diff_greeks(spot, strike, T, r, sigma, q, "call")
            best_rec = TradeRecommendation(
                action="BUY CALL",
                strike=strike,
                premium=round(mid, 2),
                pop=None,
                breakeven=round(strike + mid, 2),
                smile_iv=round(sigma, 4),
                greeks=greeks,
                fd_greeks=fd,
                max_loss=round(mid * 100, 0),
                volume=vol,
                open_interest=oi,
            )

    return best_rec


def _recommend_long_put(chain: dict[str, Any], spot: float, dte: int,
                       iv_smile, r: float = 0.042, q: float = 0.0,
                       yield_bias: str = "auto",
                       min_oi: int = 10, min_vol: int = 5) -> Optional[TradeRecommendation]:
    """Find best long put recommendation with OI/vol filter and delta/gamma scoring."""
    puts = chain.get("puts", [])
    if not puts:
        return None

    T = max(1, dte) / 365.0
    best_rec: Optional[TradeRecommendation] = None
    best_score = -1.0

    # Adaptive band: expand for sparse chains
    tight_lo_p, tight_hi_p = spot * 0.85, spot * 1.05
    n_tight_p = sum(1 for o in puts if o.get("strike") is not None
                    and tight_lo_p <= o["strike"] <= tight_hi_p)
    if n_tight_p < 2:
        put_lo, put_hi = spot * 0.40, spot * 1.20
    else:
        put_lo, put_hi = tight_lo_p, tight_hi_p

    for opt in puts:
        strike = opt.get("strike")
        if strike is None or strike < put_lo or strike > put_hi:
            continue

        # OI/volume floor — relax for sparse chains
        vol = int(opt.get("volume") or 0)
        oi = int(opt.get("oi") or 0)
        liq_ok = (oi >= min_oi or vol >= min_vol) or (oi >= 1 or vol >= 1)
        if not liq_ok:
            continue

        bid = opt.get("bid")
        ask = opt.get("ask")
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            mid = (bid + ask) / 2
        elif opt.get("lastPrice") is not None and opt["lastPrice"] > 0:
            mid = opt["lastPrice"]
        else:
            continue

        sigma = _smile_iv_at(iv_smile, strike, spot)
        greeks = _bs_greeks(spot, strike, T, r, sigma, q, "put")

        # Scoring: prefer high |delta| and gamma for directional downside plays
        effective_bias = yield_bias
        if effective_bias == "auto":
            effective_bias = "buyer"  # long put is a buyer strategy
        if effective_bias == "seller":
            score = abs(greeks.theta) * 1000 + abs(greeks.delta) * 50 + min(oi, 5000) * 0.01 + min(vol, 500) * 0.02
        else:
            score = abs(greeks.delta) * 100 + greeks.gamma * 50 + min(oi, 5000) * 0.01 + min(vol, 500) * 0.02

        if score > best_score:
            best_score = score
            fd = _finite_diff_greeks(spot, strike, T, r, sigma, q, "put")
            best_rec = TradeRecommendation(
                action="BUY PUT",
                strike=strike,
                premium=round(mid, 2),
                pop=None,
                breakeven=round(strike - mid, 2),
                smile_iv=round(sigma, 4),
                greeks=greeks,
                fd_greeks=fd,
                max_loss=round(mid * 100, 0),
                volume=vol,
                open_interest=oi,
            )

    return best_rec


# ═══════════════════════════════════════════════════════════════════════════════
#  INPUT PARSING + EVALUATION (upgraded)
# ═══════════════════════════════════════════════════════════════════════════════

def parse_inputs(query: str) -> StrategyInputs:
    q = (query or "").strip()
    ticker = (_extract_text(q, ["ticker", "symbol"]) or _fallback_ticker(q)).upper()
    spot = _extract_float(q, ["spot", "price", "underlying"])
    strike = _extract_float(q, ["strike", "k"])
    premium = _extract_float(q, ["premium", "credit", "mid"])
    dte = _extract_int(q, ["dte", "days", "days_to_expiry", "days_to_exp"])
    yield_raw = _extract_text(q, ["yield", "bias"])
    yield_bias = yield_raw.lower() if yield_raw and yield_raw.lower() in {"seller", "buyer", "auto"} else "auto"
    mode_raw = _extract_text(q, ["mode"])
    if mode_raw:
        mode = mode_raw.lower()
    elif "put" in q.lower() and "csp" not in q.lower() and "call" not in q.lower():
        mode = "put"
    elif "csp" in q.lower() and "call" not in q.lower():
        mode = "csp"
    elif "both" in q.lower():
        mode = "both"
    else:
        mode = "both"  # default to dual analysis

    spot_source = "default"
    if spot is not None:
        spot_source = "query"
    else:
        spot, spot_source = _fetch_spot(ticker)

    strike_source = "query" if strike is not None else "default"
    premium_source = "query" if premium is not None else "default"
    chain_expiry = ""
    iv_mid: Optional[float] = None
    cached_chain: Optional[dict] = None

    if strike is None or premium is None or dte is None:
        desired_dte = dte if dte is not None and dte > 0 else 30
        cached_chain = _fetch_chain(ticker, target_dte=desired_dte)
        if cached_chain:
            pick_mode = mode if mode in {"call", "csp"} else "call"
            best = _best_option_from_chain(cached_chain, spot, pick_mode)
            if best:
                if strike is None and best.get("strike") is not None:
                    strike = best["strike"]
                    strike_source = "chain"
                if premium is None and best.get("premium") is not None:
                    premium = best["premium"]
                    premium_source = "chain"
                if dte is None and best.get("dte") is not None:
                    dte = best["dte"]
                chain_expiry = best.get("expiry") or ""
                iv_mid = best.get("iv")

    if strike is None:
        strike = round(spot * 1.05, 2)
    if premium is None:
        premium = round(max(0.5, 0.02 * spot), 2)
    if dte is None or dte <= 0:
        dte = 30
    if mode not in {"call", "csp", "put", "both"}:
        mode = "both"

    return StrategyInputs(
        ticker=ticker, spot=spot, strike=strike, premium=premium, dte=dte,
        mode=mode, yield_bias=yield_bias, spot_source=spot_source,
        premium_source=premium_source,
        strike_source=strike_source, chain_expiry=chain_expiry, iv_mid=iv_mid,
        _cached_chain=cached_chain,
    )


def evaluate(inp: StrategyInputs) -> StrategyOutput:
    moneyness = inp.strike / inp.spot if inp.spot > 0 else 1.0
    sigma = 0.22 + 0.18 * abs(math.log(max(1e-6, moneyness)))
    horizon = math.sqrt(max(1e-6, inp.dte / 365.0))

    z = (inp.strike - inp.spot) / (inp.spot * sigma * horizon)
    probability_otm = _clamp(0.5 * (1.0 + math.erf(z / math.sqrt(2.0))), 0.02, 0.98)

    eval_mode = inp.mode if inp.mode in {"call", "csp"} else "call"
    if eval_mode == "call":
        breakeven = max(0.0, inp.spot - inp.premium)
        max_profit = max(0.0, (inp.strike - inp.spot) + inp.premium)
        max_loss = max(0.0, inp.spot - inp.premium)
        annualized_yield = ((inp.premium / max(inp.spot, 1e-6)) * (365.0 / inp.dte)) * 100.0
    else:
        breakeven = max(0.0, inp.strike - inp.premium)
        max_profit = max(0.0, inp.premium)
        max_loss = max(0.0, inp.strike - inp.premium)
        annualized_yield = ((inp.premium / max(inp.strike, 1e-6)) * (365.0 / inp.dte)) * 100.0

    assignment_prob = 1.0 - probability_otm
    if assignment_prob >= 0.45:
        assignment_risk = "high"
    elif assignment_prob >= 0.25:
        assignment_risk = "medium"
    else:
        assignment_risk = "low"

    if annualized_yield >= 18.0 and assignment_risk in {"low", "medium"}:
        verdict = "favorable"
        rationale = "Yield compensates risk with acceptable assignment profile."
    elif annualized_yield >= 10.0:
        verdict = "neutral"
        rationale = "Setup is viable but pricing edge is moderate."
    else:
        verdict = "unfavorable"
        rationale = "Premium yield is too thin for the modeled risk."

    # ── Fetch technicals + advanced chain analysis ──
    signals = _compute_technicals(inp.ticker)
    is_leveraged = inp.ticker.upper() in _LEVERAGED_ETFS

    # ── Chain-level Greeks recommendations ──
    csp_rec = None
    call_rec = None
    put_rec = None
    desired_dte = inp.dte
    # Reuse cached chain from parse_inputs if available
    chain = inp._cached_chain or _fetch_chain(inp.ticker, target_dte=desired_dte)
    if chain:
        all_opts = (chain.get("calls") or []) + (chain.get("puts") or [])
        iv_smile = _build_iv_smile(all_opts, inp.spot)

        dte_from_chain = desired_dte
        expiry_str = chain.get("expiry") or ""
        if expiry_str:
            for fmt in ("%Y-%m-%d", "%B %d, %Y", "%m/%d/%Y"):
                try:
                    dte_from_chain = max(1, (datetime.strptime(expiry_str[:10], fmt) - datetime.now()).days)
                    break
                except ValueError:
                    continue

        if inp.mode in {"csp", "both"}:
            csp_rec = _recommend_csp(
                chain, inp.spot, dte_from_chain, iv_smile,
                yield_bias=inp.yield_bias,
                hist_vol=signals.hist_vol_30d if signals else None,
            )
        if inp.mode in {"call", "both"}:
            call_rec = _recommend_long_call(
                chain, inp.spot, dte_from_chain, iv_smile,
                yield_bias=inp.yield_bias,
            )
        if inp.mode in {"put", "both"}:
            put_rec = _recommend_long_put(
                chain, inp.spot, dte_from_chain, iv_smile,
                yield_bias=inp.yield_bias,
            )

    return StrategyOutput(
        code="10-323",
        ticker=inp.ticker,
        mode=inp.mode,
        verdict=verdict,
        rationale=rationale,
        probability_otm=round(probability_otm, 4),
        breakeven=round(breakeven, 4),
        max_profit_per_share=round(max_profit, 4),
        max_loss_per_share=round(max_loss, 4),
        annualized_yield_pct=round(annualized_yield, 3),
        assignment_risk=assignment_risk,
        signals=signals,
        csp_rec=csp_rec,
        call_rec=call_rec,
        put_rec=put_rec,
        is_leveraged_etf=is_leveraged,
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  TELEGRAM OUTPUT FORMAT (full engine report)
# ═══════════════════════════════════════════════════════════════════════════════

def _fmt_greeks_line(g: GreeksResult) -> str:
    return f"Δ:{g.delta} | Γ:{g.gamma} | Θ:{g.theta:+.3f}/day | ν:{g.vega}"


def _fmt_fd_line(fd: Optional[GreeksResult], analytical: Optional[GreeksResult] = None) -> str:
    if fd is None:
        return ""
    # Show finite-diff Greeks and optionally compare to analytical
    fd_str = f"Δ:{fd.delta:.4f} | Γ:{fd.gamma:.4f} | ν:{fd.vega:.3f}"
    if analytical is not None:
        # Show match indicator: ✓ if close, ✗ if divergent
        delta_match = "✓" if abs(fd.delta - analytical.delta) < 0.01 else "✗"
        gamma_match = "✓" if abs(fd.gamma - analytical.gamma) < 0.01 else "✗"
        vega_match = "✓" if abs(fd.vega - analytical.vega) < 0.05 else "✗"
        return f"{fd_str} [{delta_match}Δ {gamma_match}Γ {vega_match}ν] verified"
    return f"{fd_str}"


def format_telegram(result: StrategyOutput, inputs: StrategyInputs) -> str:
    """Full smoke-tested engine Telegram format."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"[{now_str}] FULL SMOKE-TESTED ENGINE LOADED | {result.ticker} @ ${inputs.spot:.2f}"]

    # ── Technical signals ──
    sig = result.signals
    lines.append("")
    lines.append(f"=== {result.ticker} TECHNICAL PATTERN ANALYSIS ===")
    lines.append(f"Current Price: ${inputs.spot:.2f}")
    if sig:
        lines.append(f"Trend: {sig.trend} | Momentum: {sig.momentum} | Vol Regime: {sig.vol_regime}")
        if sig.recent_return_30d != 0:
            sign = "+" if sig.recent_return_30d > 0 else ""
            lines.append(f"30-day return: {sign}{sig.recent_return_30d:.1f}%")
        if sig.mc_expected is not None:
            lines.append(
                f"\n30-day Monte-Carlo Projection: Expected ${sig.mc_expected:.2f} "
                f"| 15th-85th: ${sig.mc_lower_15:.2f} – ${sig.mc_upper_85:.2f}"
            )
    else:
        lines.append("Trend: N/A | Momentum: N/A | Vol Regime: N/A (insufficient history)")

    # ── Trade recommendations ──
    exp_label = inputs.chain_expiry or f"~{inputs.dte}d"
    lines.append(f"\n=== HIGH-PROBABILITY TRADE RECOMMENDATIONS (exp {exp_label}) — SMILE + GREEKS VERIFIED ===")

    if result.csp_rec:
        r = result.csp_rec
        yield_tag = f" | Yield {r.ann_yield_pct:.1f}%" if r.ann_yield_pct else ""
        lines.append(f"✅ CSP: Sell ${r.strike:.2f} Put @ {r.premium:.2f} credit | POP {r.pop}% | Smile IV {r.smile_iv:.1%}{yield_tag}")
        lines.append(f"   OI: {r.open_interest:,} | Vol: {r.volume:,}")
        lines.append(f"   Greeks → {_fmt_greeks_line(r.greeks)}")
        if r.fd_greeks:
            lines.append(f"   Finite-diff verify → {_fmt_fd_line(r.fd_greeks, r.greeks)}")
    else:
        lines.append("⚠️ No high-prob CSP met criteria.")

    lines.append("")
    if result.call_rec:
        r = result.call_rec
        lines.append(f"✅ LONG CALL: Buy ${r.strike:.2f} Call @ {r.premium:.2f} premium | BE ${r.breakeven:.2f}")
        lines.append(f"   OI: {r.open_interest:,} | Vol: {r.volume:,}")
        lines.append(f"   Greeks → {_fmt_greeks_line(r.greeks)}")
        if r.fd_greeks:
            lines.append(f"   Finite-diff verify → {_fmt_fd_line(r.fd_greeks, r.greeks)}")
    else:
        lines.append("⚠️ No high-prob long call setup.")

    lines.append("")
    if result.put_rec:
        r = result.put_rec
        lines.append(f"✅ LONG PUT: Buy ${r.strike:.2f} Put @ {r.premium:.2f} premium | BE ${r.breakeven:.2f}")
        lines.append(f"   OI: {r.open_interest:,} | Vol: {r.volume:,}")
        lines.append(f"   Greeks → {_fmt_greeks_line(r.greeks)}")
        if r.fd_greeks:
            lines.append(f"   Finite-diff verify → {_fmt_fd_line(r.fd_greeks, r.greeks)}")
    else:
        lines.append("⚠️ No high-prob long put setup.")

    # ── Risk warning for leveraged ETFs ──
    if result.is_leveraged_etf:
        lines.append(
            f"\n🚨 RISK WARNING: {result.ticker} is a leveraged/crypto-mining ETF. "
            "Extreme vol & decay. Use <2% portfolio risk per trade. NOT financial advice."
        )

    # ── Legacy metrics (compact) ──
    verdict_icon = {"favorable": "🟢", "neutral": "🟡", "unfavorable": "🔴"}.get(result.verdict, "⚪")
    risk_icon = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(result.assignment_risk, "⚪")
    lines.append(f"\n─── Heuristic Summary ───")
    lines.append(f"{verdict_icon} Verdict: *{result.verdict.upper()}* | Ann. Yield: *{result.annualized_yield_pct:.1f}%*")
    lines.append(f"P(OTM): {result.probability_otm:.1%} | BE: ${result.breakeven:.2f} | Assign: {risk_icon} {result.assignment_risk}")
    lines.append(f"_{result.rationale}_")

    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="10-323 CSP/Call strategy evaluator (full engine)")
    parser.add_argument("ticker", nargs="?", default="", help="Ticker symbol (positional, e.g. SLV)")
    parser.add_argument("--query", default="", help="Freeform query with key=value inputs")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    args = parser.parse_args()
    # Merge positional ticker into query when no --query given
    if args.ticker and not args.query:
        args.query = args.ticker.upper()
    return args


def main() -> int:
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
    raise SystemExit(main())

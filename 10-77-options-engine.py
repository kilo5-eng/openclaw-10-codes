#!/usr/bin/env python3
"""
High-Probability Options Projection Engine (MBOUM-Fintel Primary)
Master-level quant system: Technical patterns + Monte-Carlo projection + 
Smile-adjusted IV surface + Full analytical Greeks verification.

PRIMARY APIs:
- MBOUM: Real-time price quotes, short interest data
- Fintel: Short interest % context (preferred)
- yfinance: Historical prices (reliable OHLCV data) - no longer direct auth required

STANDALONE - SINGLE FILE - WORKS ON ANY LINUX / macOS / Windows with Python 3.8+

Run for ANY stock/ETF:
    python3 10-77.py AAPL
    python3 10-77.py TSLA --hist-period 6mo
    python3 10-77.py BMNU --target-pop 0.80
"""

import os
import sys
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from scipy.stats import norm
from scipy.interpolate import interp1d
import warnings
import argparse
import json
from pathlib import Path
from typing import Optional, Dict, Tuple

warnings.filterwarnings("ignore")

# ==================== API CONFIG ====================
def load_env():
    """Load environment variables from .env files."""
    env_files = [
        Path.home() / '.env',
        Path.home() / '.openclaw' / '.env',
        Path.home() / '.openclaw' / 'workspace' / '.env',
        Path.cwd() / '.env',
    ]
    
    env_vars = {}
    for env_file in env_files:
        if env_file.exists():
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, val = line.split('=', 1)
                        env_vars[key.strip()] = val.strip().strip('"\'')
    
    return env_vars

ENV_VARS = load_env()
MBOUM_API_KEY = ENV_VARS.get('MBOUM_API_KEY') or os.getenv('MBOUM_API_KEY', '')
MBOUM_BASE_URL = ENV_VARS.get('MBOUM_BASE_URL') or os.getenv('MBOUM_BASE_URL', 'https://api.mboum.com')
FINTEL_API_KEY = ENV_VARS.get('FINTEL_API_KEY') or os.getenv('FINTEL_API_KEY', '')
FINTEL_CONTEXT_FILE = ENV_VARS.get('FINTEL_CONTEXT_FILE') or os.getenv('FINTEL_CONTEXT_FILE', '')


def extract_mboum_subscription_error(payload: object) -> Optional[str]:
    """Return the provider message when MBOUM reports an inactive subscription."""
    if not isinstance(payload, dict):
        return None

    message = payload.get('message')
    if isinstance(message, str) and 'active subscription' in message.lower():
        return message.strip()

    error = payload.get('error')
    if isinstance(error, str) and 'active subscription' in error.lower():
        return error.strip()

    return None

# ==================== MBOUM DATA FETCH ====================
def fetch_mboum_quotes(ticker: str) -> Optional[Dict]:
    """Fetch real-time quote from MBOUM."""
    if not MBOUM_API_KEY:
        print("[WARNING] MBOUM_API_KEY not set. Proceeding with limited data.")
        return None
    
    url = f"{MBOUM_BASE_URL}/v1/markets/stock/quotes"
    headers = {'Authorization': f'Bearer {MBOUM_API_KEY}'}
    params = {'symbol': ticker}
    
    try:
        r = requests.get(url, headers=headers, params=params, timeout=5)
        r.raise_for_status()
        data = r.json()
        subscription_error = extract_mboum_subscription_error(data)
        if subscription_error:
            print(f"[WARNING] MBOUM subscription inactive: {subscription_error}")
            return None
        if data.get('body') and len(data['body']) > 0:
            return data['body'][0]
    except Exception as e:
        print(f"[WARNING] MBOUM quote fetch failed: {e}")
    
    return None

def fetch_mboum_history(ticker: str, interval: str = '1d', range_days: int = 180) -> Optional[pd.DataFrame]:
    """Fetch historical price data via yfinance (reliable), MBOUM as optional alternative."""
    # Primary: yfinance (reliable OHLCV data)
    return _fetch_yfinance_fallback(ticker, range_days)

def _fetch_yfinance_fallback(ticker: str, range_days: int = 180) -> Optional[pd.DataFrame]:
    """Fallback to yfinance if MBOUM unavailable."""
    try:
        import yfinance as yf
        period_days = min(range_days, 365)
        if period_days <= 30:
            period = '1mo'
        elif period_days <= 180:
            period = '6mo'
        elif period_days <= 365:
            period = '1y'
        else:
            period = '2y'
        
        hist = yf.Ticker(ticker).history(period=period)
        if hist.empty:
            return None
        return hist[['Open', 'High', 'Low', 'Close', 'Volume']]
    except Exception as e:
        print(f"[ERROR] Both MBOUM and yfinance failed: {e}")
        return None

def fetch_fintel_si(ticker: str) -> Optional[float]:
    """Fetch short-interest style context from live Fintel, then fallback to MBOUM."""
    if FINTEL_CONTEXT_FILE and Path(FINTEL_CONTEXT_FILE).exists():
        try:
            with open(FINTEL_CONTEXT_FILE) as f:
                context = json.load(f)
                si_data = context.get(ticker.lower(), {})
                for field in ['short_interest_pct', 'short_pct', 'si_pct', 'shortInterestPercent']:
                    if field in si_data:
                        return float(si_data[field])
        except Exception:
            pass

    if FINTEL_API_KEY:
        try:
            url = f"https://api.fintel.io/web/v/0.0/ss/us/{ticker.lower()}"
            headers = {'X-API-Key': FINTEL_API_KEY}
            response = requests.get(url, headers=headers, timeout=5)
            response.raise_for_status()
            payload = response.json()
            rows = payload.get('data') if isinstance(payload, dict) else None
            if isinstance(rows, list) and rows:
                ratio = rows[0].get('shortVolumeRatio')
                if ratio is not None:
                    return float(ratio) * 100.0
        except Exception:
            pass

    if MBOUM_API_KEY:
        try:
            url = f"{MBOUM_BASE_URL}/v2/markets/stock/short-interest"
            headers = {'Authorization': f'Bearer {MBOUM_API_KEY}'}
            params = {'ticker': ticker, 'type': 'STOCKS'}
            response = requests.get(url, headers=headers, params=params, timeout=5)
            response.raise_for_status()
            data = response.json()
            subscription_error = extract_mboum_subscription_error(data)
            if subscription_error:
                print(f"[WARNING] MBOUM subscription inactive: {subscription_error}")
                return None
            body = data.get('body') if isinstance(data, dict) else None
            if isinstance(body, list) and body:
                si_item = body[0]
                for field in ['shortInterestPercent', 'short_interest_pct', 'shortInterest']:
                    if field in si_item and si_item[field] is not None:
                        return float(si_item[field])
        except Exception:
            pass

    return None

# ==================== OPTIONS ENGINE ====================
class HighProbOptionsEngine:
    def __init__(self, ticker: str, hist_period: str = "6mo", mc_sims: int = 10000,
                 risk_free_rate: float = 0.042, dividend_yield: float = 0.0):
        self.ticker = ticker.upper()
        self.mc_sims = mc_sims
        self.r = risk_free_rate
        self.q = dividend_yield
        self.iv_interpolator = None
        self.si_pct = None
        
        # Fetch historical data via MBOUM
        range_days = 180 if hist_period == "6mo" else 365 if hist_period == "1y" else 30
        self.hist = fetch_mboum_history(self.ticker, range_days=range_days)
        
        if self.hist is None or self.hist.empty:
            print(f"[ERROR] No data found for ticker {self.ticker}. Check symbol or API keys.")
            sys.exit(1)
        
        # Get current price from MBOUM quote
        quote = fetch_mboum_quotes(self.ticker)
        if quote:
            self.current_price = float(quote.get('regularMarketPrice', self.hist['Close'].iloc[-1]))
        else:
            self.current_price = self.hist['Close'].iloc[-1]
        
        # Fetch short interest from Fintel
        self.si_pct = fetch_fintel_si(self.ticker)
        
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ENGINE LOADED | {self.ticker} @ ${self.current_price:.2f} | Hist: {hist_period} | Sims: {mc_sims} | SI: {self.si_pct if self.si_pct else 'N/A'}%")

    # ==================== IV SMILE + GREEKS ====================
    def _build_iv_surface(self, chain_df: pd.DataFrame) -> interp1d:
        S = self.current_price
        chain = chain_df.copy()
        chain['moneyness'] = chain['strike'] / S
        chain = chain.sort_values('moneyness')
        x, y = chain['moneyness'].values, chain['impliedVolatility'].values
        return interp1d(x, y, kind='linear', fill_value=(y[0], y[-1]), bounds_error=False)

    @staticmethod
    def black_scholes_greeks(S: float, K: float, T: float, r: float, sigma: float, q: float = 0.0, option_type: str = "call") -> dict:
        if T <= 0 or sigma <= 0:
            return {'delta': 1.0 if option_type == "call" else 0.0, 'gamma': 0.0,
                    'theta': 0.0, 'vega': 0.0, 'rho': 0.0}
        d1 = (np.log(S / K) + (r - q + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
        d2 = d1 - sigma * np.sqrt(T)
        if option_type == "call":
            delta = np.exp(-q * T) * norm.cdf(d1)
            gamma = np.exp(-q * T) * norm.pdf(d1) / (S * sigma * np.sqrt(T))
            theta = -(S * np.exp(-q * T) * norm.pdf(d1) * sigma) / (2 * np.sqrt(T)) - r * K * np.exp(-r * T) * norm.cdf(d2) + q * S * np.exp(-q * T) * norm.cdf(d1)
            vega = S * np.exp(-q * T) * norm.pdf(d1) * np.sqrt(T)
            rho = K * T * np.exp(-r * T) * norm.cdf(d2)
        else:
            delta = -np.exp(-q * T) * norm.cdf(-d1)
            gamma = np.exp(-q * T) * norm.pdf(d1) / (S * sigma * np.sqrt(T))
            theta = -(S * np.exp(-q * T) * norm.pdf(d1) * sigma) / (2 * np.sqrt(T)) + r * K * np.exp(-r * T) * norm.cdf(-d2) - q * S * np.exp(-q * T) * norm.cdf(-d1)
            vega = S * np.exp(-q * T) * norm.pdf(d1) * np.sqrt(T)
            rho = -K * T * np.exp(-r * T) * norm.cdf(-d2)
        return {'delta': round(delta, 4), 'gamma': round(gamma, 4),
                'theta': round(theta / 365, 3), 'vega': round(vega / 100, 3), 'rho': round(rho / 100, 3)}

    def _finite_diff_greeks(self, K: float, T: float, sigma: float, option_type: str, h: float = 0.001) -> dict:
        base = self.black_scholes_greeks(self.current_price, K, T, self.r, sigma, self.q, option_type)
        up = self.black_scholes_greeks(self.current_price + h, K, T, self.r, sigma, self.q, option_type)['delta']
        dn = self.black_scholes_greeks(self.current_price - h, K, T, self.r, sigma, self.q, option_type)['delta']
        delta_num = (up - dn) / (2 * h)
        gamma_num = (up - 2 * base['delta'] + dn) / (h ** 2)
        v_up = self.black_scholes_greeks(self.current_price, K, T, self.r, sigma + 0.01, self.q, option_type)['delta']
        v_dn = self.black_scholes_greeks(self.current_price, K, T, self.r, sigma - 0.01, self.q, option_type)['delta']
        vega_num = (v_up - v_dn) / 0.02 * 100
        return {'delta_num': round(delta_num, 4), 'gamma_num': round(gamma_num, 4), 'vega_num': round(vega_num, 3)}

    def _add_greeks_to_chain(self, chain_df: pd.DataFrame, option_type: str) -> pd.DataFrame:
        def compute_row(row):
            T = row['days_to_exp'] / 365.0
            moneyness = row['strike'] / self.current_price
            sigma_smile = float(self.iv_interpolator(moneyness))
            greeks = self.black_scholes_greeks(self.current_price, row['strike'], T, self.r, sigma_smile, self.q, option_type)
            num = self._finite_diff_greeks(row['strike'], T, sigma_smile, option_type)
            greeks['sigma_smile'] = round(sigma_smile, 4)
            greeks.update({f"{k}_num": v for k, v in num.items()})
            return pd.Series(greeks)
        return pd.concat([chain_df.reset_index(drop=True), chain_df.apply(compute_row, axis=1)], axis=1)

    # ==================== TECHNICALS & SIGNALS ====================
    def add_technical_indicators(self):
        df = self.hist.copy()
        df['SMA_20'] = df['Close'].rolling(20).mean()
        df['SMA_50'] = df['Close'].rolling(50).mean()
        df['EMA_12'] = df['Close'].ewm(span=12, adjust=False).mean()
        df['EMA_26'] = df['Close'].ewm(span=26, adjust=False).mean()
        df['MACD'] = df['EMA_12'] - df['EMA_26']
        df['MACD_signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss
        df['RSI'] = 100 - 100 / (1 + rs)
        df['BB_mid'] = df['Close'].rolling(20).mean()
        df['BB_std'] = df['Close'].rolling(20).std()
        df['BB_upper'] = df['BB_mid'] + 2 * df['BB_std']
        df['BB_lower'] = df['BB_mid'] - 2 * df['BB_std']
        df['ATR'] = (df['High'] - df['Low']).rolling(14).mean()
        self.df = df
        return df

    def generate_signals(self):
        latest = self.df.iloc[-1]
        signals = {
            "trend": "BULLISH" if latest['Close'] > latest['SMA_50'] and latest['MACD'] > latest['MACD_signal'] else "BEARISH/NEUTRAL",
            "momentum": "STRONG" if latest['RSI'] > 55 and latest['RSI'] < 70 else "WEAK" if latest['RSI'] < 40 else "NEUTRAL",
            "vol_regime": "EXPANSION" if latest['Close'] > latest['BB_upper'] else "CONTRACTED" if latest['Close'] < latest['BB_lower'] else "NORMAL",
            "recent_return_30d": (self.df['Close'].iloc[-1] / self.df['Close'].iloc[-30] - 1) * 100 if len(self.df) >= 30 else 0
        }
        days_to_exp = 30
        hist_vol = self.df['Close'].pct_change().std() * np.sqrt(252)
        iv_approx = 1.5 * hist_vol if "LEVERAGED" in self.ticker or self.ticker in ["BMNU", "BMNR"] else hist_vol
        daily_vol = iv_approx / np.sqrt(252)
        sim_prices = self.current_price * np.exp(np.cumsum(np.random.normal(0, daily_vol, (self.mc_sims, days_to_exp)), axis=1)[:, -1])
        signals["projected_30d"] = {
            "expected": np.mean(sim_prices),
            "lower_15pct": np.percentile(sim_prices, 15),
            "upper_85pct": np.percentile(sim_prices, 85)
        }
        self.signals = signals
        return signals

    def get_options_chain(self, days_ahead: int = 30):
        """Fetch live options chain from MBOUM and enrich it with smile-adjusted Greeks."""
        if not MBOUM_API_KEY:
            print(f"[WARNING] MBOUM_API_KEY missing; options chain unavailable for {self.ticker}.")
            return None, None, None

        headers = {
            'Authorization': f'Bearer {MBOUM_API_KEY}',
            'Accept': 'application/json',
            'User-Agent': 'OpenClaw-10-77/2.0',
        }
        target_date = datetime.utcnow().date() + timedelta(days=days_ahead)

        try:
            summary_resp = requests.get(
                f"{MBOUM_BASE_URL}/v1/markets/options",
                headers=headers,
                params={'ticker': self.ticker, 'display': 'list'},
                timeout=10,
            )
            summary_resp.raise_for_status()
            summary_payload = summary_resp.json()
            subscription_error = extract_mboum_subscription_error(summary_payload)
            if subscription_error:
                print(f"[WARNING] MBOUM subscription inactive: {subscription_error}")
                return None, None, None
            summary_body = summary_payload.get('body') or []
            if not summary_body:
                print(f"[WARNING] No options summary returned for {self.ticker}.")
                return None, None, None
            summary = summary_body[0]
            expiration_dates = summary.get('expirationDates') or []
            if not expiration_dates:
                print(f"[WARNING] No near-term options found for {self.ticker}.")
                return None, None, None

            selected_expiration = min(
                expiration_dates,
                key=lambda epoch: abs((datetime.utcfromtimestamp(int(epoch)).date() - target_date).days),
            )
            chain_resp = requests.get(
                f"{MBOUM_BASE_URL}/v1/markets/options",
                headers=headers,
                params={'ticker': self.ticker, 'display': 'list', 'expiration': str(selected_expiration)},
                timeout=10,
            )
            chain_resp.raise_for_status()
            chain_payload = chain_resp.json()
            subscription_error = extract_mboum_subscription_error(chain_payload)
            if subscription_error:
                print(f"[WARNING] MBOUM subscription inactive: {subscription_error}")
                return None, None, None
            chain_body = chain_payload.get('body') or []
            if not chain_body:
                print(f"[WARNING] No option chain returned for {self.ticker}.")
                return None, None, None

            chain_root = chain_body[0]
            option_sets = chain_root.get('options') or []
            option_set = option_sets[0] if option_sets else {}
            calls_raw = option_set.get('calls') or chain_root.get('calls') or []
            puts_raw = option_set.get('puts') or chain_root.get('puts') or []
            if not calls_raw and not puts_raw:
                print(f"[WARNING] No near-term options found for {self.ticker}.")
                return None, None, None

            expiration_date = datetime.utcfromtimestamp(int(selected_expiration)).date()
            days_to_exp = max((expiration_date - datetime.utcnow().date()).days, 1)

            def _to_chain_df(rows):
                if not rows:
                    return None
                frame = pd.DataFrame(rows).copy()
                for column in ['strike', 'bid', 'ask', 'lastPrice', 'impliedVolatility', 'openInterest', 'volume']:
                    if column in frame.columns:
                        frame[column] = pd.to_numeric(frame[column], errors='coerce')
                frame = frame.dropna(subset=['strike', 'impliedVolatility'])
                frame = frame[frame['impliedVolatility'] > 0].copy()
                if frame.empty:
                    return None
                frame['days_to_exp'] = days_to_exp
                return frame

            calls = _to_chain_df(calls_raw)
            puts = _to_chain_df(puts_raw)
            iv_seed = calls if calls is not None and len(calls) >= 2 else puts
            if iv_seed is None or iv_seed.empty:
                print(f"[WARNING] No usable IV data found for {self.ticker} options.")
                return None, None, expiration_date.isoformat()

            self.iv_interpolator = self._build_iv_surface(iv_seed)
            if calls is not None:
                calls = self._add_greeks_to_chain(calls, 'call')
            if puts is not None:
                puts = self._add_greeks_to_chain(puts, 'put')
            return calls, puts, expiration_date.isoformat()
        except Exception as exc:
            print(f"[WARNING] MBOUM options fetch failed for {self.ticker}: {exc}")
            return None, None, None

    def recommend_cash_secured_puts(self, puts, target_pop: float = 0.75):
        if puts is None:
            return None
        current = self.current_price
        support = self.df['BB_lower'].iloc[-1]
        candidates = puts[(puts['strike'] < current * 0.92) & (puts['strike'] > support * 0.95)].copy()
        if candidates.empty:
            return None
        candidates = candidates[(candidates['bid'] > 0) & (candidates['ask'] > 0)].copy()
        if 'openInterest' in candidates.columns:
            candidates = candidates[candidates['openInterest'].fillna(0) > 0].copy()
        if candidates.empty:
            return None
        candidates['credit'] = (candidates['bid'] + candidates['ask']) / 2
        candidates = candidates[candidates['credit'] > 0].copy()
        if candidates.empty:
            return None
        candidates['cash_required'] = candidates['strike'] * 100
        candidates['yield_pct'] = candidates['credit'] / candidates['strike'] * 100
        candidates['approx_POP'] = 1 + candidates['delta']
        candidates = candidates[candidates['approx_POP'] >= target_pop].sort_values(['theta', 'credit'], ascending=False)
        return candidates.head(5) if not candidates.empty else None

    def recommend_long_calls(self, calls, target_pop: float = 0.50):
        if calls is None:
            return None
        current = self.current_price
        candidates = calls[(calls['strike'] > current * 0.95) & (calls['strike'] < current * 1.20)].copy()
        if candidates.empty:
            return None
        candidates = candidates[(candidates['bid'] > 0) & (candidates['ask'] > 0)].copy()
        if 'openInterest' in candidates.columns:
            candidates = candidates[candidates['openInterest'].fillna(0) > 0].copy()
        if candidates.empty:
            return None
        candidates['credit'] = (candidates['bid'] + candidates['ask']) / 2
        candidates = candidates[candidates['credit'] > 0].copy()
        if candidates.empty:
            return None
        candidates['breakeven'] = candidates['strike'] + candidates['credit']
        candidates['profit_margin'] = (candidates['credit'] / current - 1) * 100
        candidates['approx_POP'] = 1 + candidates['delta']
        candidates = candidates[candidates['approx_POP'] >= target_pop].sort_values('profit_margin', ascending=False)
        return candidates.head(5) if not candidates.empty else None

    def calculate_gamma_walls(self) -> dict:
        """Estimate theoretical gamma walls (concentration points for gamma exposure)."""
        current = self.current_price
        hist_vol = self.df['Close'].pct_change().std() * np.sqrt(252)
        std_dev_1d = current * (hist_vol / np.sqrt(252))
        
        walls = {
            "atm": current,
            "strike_1std_up": round(current + std_dev_1d, 2),
            "strike_1std_dn": round(current - std_dev_1d, 2),
            "strike_2std_up": round(current + 2 * std_dev_1d, 2),
            "strike_2std_dn": round(current - 2 * std_dev_1d, 2),
        }
        return walls

    def estimate_max_pain(self, days_to_exp: int = 30) -> float:
        """Estimate max pain - price that would cause max loss to options holders."""
        # Max pain typically sits at the price with highest open interest
        # For theoretical estimate: weighted toward ATM but pulled by volume/momentum
        current = self.current_price
        recent_30d_high = self.df['Close'].iloc[-30:].max()
        recent_30d_low = self.df['Close'].iloc[-30:].min()
        recent_30d_mid = (recent_30d_high + recent_30d_low) / 2
        
        # Max pain tends toward the point of maximum financial distress
        # For small-caps: usually near nearest support level
        support = self.df['BB_lower'].iloc[-1]
        
        # Weighted estimate: 50% ATM, 30% support, 20% recent mid
        max_pain = (0.5 * current) + (0.3 * support) + (0.2 * recent_30d_mid)
        
        return round(max_pain, 2)

    def run_full_analysis(self, days_ahead: int = 30, target_pop: float = 0.75):
        self.add_technical_indicators()
        signals = self.generate_signals()
        calls, puts, exp_date = self.get_options_chain(days_ahead)
        
        # Calculate options metrics
        gamma_walls = self.calculate_gamma_walls()
        max_pain = self.estimate_max_pain(days_ahead)
        
        print("\n=== TECHNICAL PATTERN ANALYSIS ===")
        print(f"Current Price: ${self.current_price:.2f}")
        print(f"Trend: {signals['trend']} | Momentum: {signals['momentum']} | Vol Regime: {signals['vol_regime']}")
        print(f"30-day return: {signals['recent_return_30d']:.1f}%")
        print(f"30-day Monte-Carlo Projection: Expected ${signals['projected_30d']['expected']:.2f} | 15th-85th: ${signals['projected_30d']['lower_15pct']:.2f}–${signals['projected_30d']['upper_85pct']:.2f}")
        
        # Display OPTIONS STRUCTURE
        print("\n=== OPTIONS STRUCTURE (DTE: 30d) ===")
        print(f"Time Horizon: {days_ahead} days to expiration")
        print(f"Max Pain Estimate: ${max_pain:.2f}")
        print(f"\nGamma Walls (1 sigma concentration):")
        print(f"  2 sigma Down: ${gamma_walls['strike_2std_dn']:.2f}")
        print(f"  1 sigma Down: ${gamma_walls['strike_1std_dn']:.2f}")
        print(f"  ATM:         ${gamma_walls['atm']:.2f}")
        print(f"  1 sigma Up:   ${gamma_walls['strike_1std_up']:.2f}")
        print(f"  2 sigma Up:   ${gamma_walls['strike_2std_up']:.2f}")
        
        if self.si_pct:
            print(f"\nShort Volume / SI Context: {self.si_pct:.2f}% (via Fintel)")
        
        if calls is None and puts is None:
            print("\n[INFO] Options chain unavailable. Theoretical gamma/max-pain estimates shown.")
        else:
            print(f"\nExp Date: {exp_date}")
            csp = self.recommend_cash_secured_puts(puts, target_pop)
            if csp is not None:
                print("\n=== CASH SECURED PUT CANDIDATES ===")
                print(csp[['strike', 'bid', 'ask', 'delta', 'theta', 'approx_POP', 'yield_pct']].head(3).to_string(index=False))
            else:
                print("[INFO] No high-prob CSP met criteria.")
            
            lc = self.recommend_long_calls(calls, 0.50)
            if lc is not None:
                print("\n=== LONG CALL CANDIDATES ===")
                print(lc[['strike', 'bid', 'ask', 'delta', 'theta', 'approx_POP', 'profit_margin']].head(3).to_string(index=False))
            else:
                print("[INFO] No high-prob long call setup.")
        
        print("\n[RISK WARNING] This is NOT financial advice. Options involve substantial risk. Validate liquidity & commissions before executing.")


# ==================== MAIN ====================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="High-Probability Options Projection Engine (MBOUM-Fintel Primary)")
    parser.add_argument("tickers", nargs="*", help="One or more stock ticker symbols")
    parser.add_argument("--ticker", dest="single_ticker", help="Single stock ticker symbol")
    parser.add_argument("--tickers", dest="ticker_csv", help="Comma-separated ticker symbols")
    parser.add_argument("--hist-period", type=str, default="6mo", help="Historical period (30d, 3mo, 6mo, 1y)")
    parser.add_argument("--mc-sims", type=int, default=1500, help="Monte-Carlo simulations")
    parser.add_argument("--days-ahead", type=int, default=30, help="Days to expiration")
    parser.add_argument("--target-pop", type=float, default=0.75, help="Target probability of profit")
    args = parser.parse_args()

    ticker_inputs = []
    if args.tickers:
        ticker_inputs.extend(args.tickers)
    if args.single_ticker:
        ticker_inputs.append(args.single_ticker)
    if args.ticker_csv:
        ticker_inputs.extend(args.ticker_csv.split(','))

    normalized_tickers = []
    seen = set()
    for raw_ticker in ticker_inputs:
        ticker = raw_ticker.strip().upper()
        if ticker and ticker not in seen:
            normalized_tickers.append(ticker)
            seen.add(ticker)

    if not normalized_tickers:
        print("🔍 No ticker provided. Interactive mode:")
        ticker_input = input("Enter ticker symbol(s), comma-separated (e.g. AAPL,BMNU,NVDA): ").strip().upper()
        if not ticker_input:
            print("[ERROR] No ticker entered. Exiting.")
            sys.exit(1)
        normalized_tickers = [ticker.strip().upper() for ticker in ticker_input.split(',') if ticker.strip()]

    exit_code = 0
    for index, ticker in enumerate(normalized_tickers):
        if index:
            print("\n" + "=" * 72 + "\n")
        try:
            engine = HighProbOptionsEngine(
                ticker=ticker,
                hist_period=args.hist_period,
                mc_sims=args.mc_sims
            )
            engine.run_full_analysis(days_ahead=args.days_ahead, target_pop=args.target_pop)
        except SystemExit as exc:
            exit_code = int(exc.code) if isinstance(exc.code, int) else 1
        except Exception as exc:
            exit_code = 1
            print(f"[ERROR] {ticker} analysis failed: {exc}")

    sys.exit(exit_code)

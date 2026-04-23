#!/usr/bin/env python3
"""
High-Probability Options Projection Engine.

Primary source: MBOUM live options chain.
Fallback source: yfinance when MBOUM is unavailable.
"""

import argparse
import datetime
import json
import os
from pathlib import Path

import numpy as np
import requests
from scipy.stats import norm

try:
    import yfinance as yf
except ImportError:
    yf = None


def load_env() -> None:
    candidates = [
        Path.home() / '.openclaw' / '.env',
        Path.home() / '.openclaw' / 'workspace' / '.env',
        Path.home() / '.config' / 'openclaw' / 'keys.env',
    ]
    for env_path in candidates:
        if not env_path.exists():
            continue
        for raw_line in env_path.read_text(encoding='utf-8').splitlines():
            line = raw_line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def black_scholes_greeks(spot_price, strike_price, years_to_expiry, rate, sigma, option_type='call'):
    d1 = (np.log(spot_price / strike_price) + (rate + sigma ** 2 / 2) * years_to_expiry) / (sigma * np.sqrt(years_to_expiry))
    d2 = d1 - sigma * np.sqrt(years_to_expiry)
    if option_type == 'call':
        delta = norm.cdf(d1)
        gamma = norm.pdf(d1) / (spot_price * sigma * np.sqrt(years_to_expiry))
        theta = -(spot_price * norm.pdf(d1) * sigma / (2 * np.sqrt(years_to_expiry))) - rate * strike_price * np.exp(-rate * years_to_expiry) * norm.cdf(d2)
        vega = spot_price * norm.pdf(d1) * np.sqrt(years_to_expiry)
        rho = strike_price * years_to_expiry * np.exp(-rate * years_to_expiry) * norm.cdf(d2)
    else:
        delta = -norm.cdf(-d1)
        gamma = norm.pdf(d1) / (spot_price * sigma * np.sqrt(years_to_expiry))
        theta = -(spot_price * norm.pdf(d1) * sigma / (2 * np.sqrt(years_to_expiry))) + rate * strike_price * np.exp(-rate * years_to_expiry) * norm.cdf(-d2)
        vega = spot_price * norm.pdf(d1) * np.sqrt(years_to_expiry)
        rho = -strike_price * years_to_expiry * np.exp(-rate * years_to_expiry) * norm.cdf(-d2)
    return {
        'delta': float(delta),
        'gamma': float(gamma),
        'theta': float(theta),
        'vega': float(vega),
        'rho': float(rho),
    }


def monte_carlo_projection(spot_price, sigma, rate, years_to_expiry, steps, sims):
    dt = years_to_expiry / steps
    paths = np.exp((rate - 0.5 * sigma ** 2) * dt + sigma * np.sqrt(dt) * np.random.normal(size=(steps, sims)))
    paths = np.cumprod(paths, axis=0) * spot_price
    ending_prices = paths[-1]
    return {
        'mean': float(np.mean(ending_prices)),
        '95_ci_low': float(np.percentile(ending_prices, 2.5)),
        '95_ci_high': float(np.percentile(ending_prices, 97.5)),
    }


def _mboum_headers():
    api_key = os.getenv('MBOUM_API_KEY', '').strip()
    if not api_key:
        raise RuntimeError('MBOUM_API_KEY missing')
    return {
        'Accept': 'application/json',
        'Authorization': f'Bearer {api_key}',
        'User-Agent': 'OpenClaw-10-323/2.0',
    }


def _request_mboum_options(symbol, expiration=None):
    params = {'ticker': symbol, 'display': 'list'}
    if expiration is not None:
        params['expiration'] = str(expiration)
    response = requests.get(
        'https://api.mboum.com/v1/markets/options',
        headers=_mboum_headers(),
        params=params,
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    body = payload.get('body') or []
    if not body:
        raise RuntimeError('MBOUM returned empty options payload')
    return body[0]


def _epoch_to_date(epoch_seconds):
    return datetime.datetime.utcfromtimestamp(int(epoch_seconds)).date()


def _pick_expiration(expiration_dates, target_date):
    if not expiration_dates:
        raise RuntimeError('No option expirations available')
    return min(expiration_dates, key=lambda item: abs((_epoch_to_date(item) - target_date).days))


def _extract_current_price(quote):
    for field in ('regularMarketPrice', 'ask', 'bid', 'regularMarketPreviousClose'):
        value = quote.get(field)
        if value is not None:
            return float(value)
    raise RuntimeError('No usable quote price in MBOUM payload')
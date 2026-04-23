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


_CRYPTO_KEY_MAP = {
    'BTC': 'bitcoin/BTC',
    'ETH': 'ethereum/ETH',
    'SOL': 'solana/SOL',
    'XRP': 'ripple/XRP',
    'ADA': 'cardano/ADA',
    'DOGE': 'dogecoin/DOGE',
    'LTC': 'litecoin/LTC',
    'BNB': 'binancecoin/BNB',
    'AVAX': 'avalanche/AVAX',
    'DOT': 'polkadot/DOT',
    'LINK': 'chainlink/LINK',
}


def _normalize_crypto_symbol(symbol):
    candidate = symbol.strip().upper()
    if '/' in candidate:
        candidate = candidate.split('/')[0]
    if '-' in candidate:
        candidate = candidate.split('-')[0]
    return candidate


def _is_likely_crypto_symbol(symbol):
    upper = symbol.strip().upper()
    normalized = _normalize_crypto_symbol(upper)
    return '/' in upper or upper.endswith('-USD') or normalized in _CRYPTO_KEY_MAP


def _crypto_key_candidates(symbol):
    upper = symbol.strip().upper()
    normalized = _normalize_crypto_symbol(upper)
    candidates = []
    mapped = _CRYPTO_KEY_MAP.get(normalized)
    if mapped:
        candidates.append(mapped)
    candidates.append(f'{normalized.lower()}/{normalized}')
    if '/' in symbol:
        candidates.append(symbol)
    seen = set()
    unique = []
    for item in candidates:
        key = item.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(key)
    return unique


def _extract_crypto_price(payload):
    body = payload.get('body')
    records = []
    if isinstance(body, list):
        records = body
    elif isinstance(body, dict):
        records = [body]
    for record in records:
        for field in ('regularMarketPrice', 'price', 'lastPrice', 'close', 'ask', 'bid'):
            value = _safe_float(record.get(field))
            if value is not None and value > 0:
                return value
    return None


def _request_mboum_crypto_spot(symbol):
    if not _is_likely_crypto_symbol(symbol):
        return None, {'status': 'not-crypto'}

    last_error = None
    for key_candidate in _crypto_key_candidates(symbol):
        try:
            response = requests.get(
                'https://api.mboum.com/v1/crypto/quotes',
                headers=_mboum_headers(),
                params={'key': key_candidate},
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
            price = _extract_crypto_price(payload)
            if price is not None:
                return price, {'status': 'ok', 'key': key_candidate, 'source': 'mboum_crypto_quotes'}
            last_error = 'MBOUM crypto quotes returned no usable price'
        except Exception as exc:
            last_error = str(exc)

    return None, {'status': 'unavailable', 'error': last_error}


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


def _select_atm_contract(contracts, spot_price):
    if not contracts:
        return None
    return min(contracts, key=lambda contract: abs(float(contract.get('strike', 0.0)) - spot_price))


def _safe_float(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _filter_display_contracts(contracts):
    filtered = []
    for contract in contracts:
        bid = _safe_float(contract.get('bid'), 0.0)
        ask = _safe_float(contract.get('ask'), 0.0)
        open_interest = _safe_float(contract.get('openInterest'), 0.0)
        if bid <= 0 or ask <= 0 or open_interest <= 0:
            continue
        filtered.append(contract)
    return filtered


def analyze_with_mboum(symbol, target_date, target_pop):
    summary = _request_mboum_options(symbol)
    chosen_expiration = _pick_expiration(summary.get('expirationDates') or [], target_date)
    chain = _request_mboum_options(symbol, expiration=chosen_expiration)
    quote = chain.get('quote') or summary.get('quote') or {}
    option_sets = chain.get('options') or []
    option_set = option_sets[0] if option_sets else {}
    calls = option_set.get('calls') or chain.get('calls') or []
    puts = option_set.get('puts') or chain.get('puts') or []
    if not calls and not puts:
        raise RuntimeError('No calls or puts returned by MBOUM')

    current_price = _extract_current_price(quote)
    spot_source = 'options_quote'
    crypto_price, crypto_meta = _request_mboum_crypto_spot(symbol)
    if crypto_price is not None:
        current_price = crypto_price
        spot_source = 'crypto_quote'

    atm_call = _select_atm_contract(calls, current_price)
    atm_put = _select_atm_contract(puts, current_price)
    contracts_for_iv = [contract for contract in (atm_call, atm_put) if contract is not None]
    if not contracts_for_iv:
        raise RuntimeError('No ATM contracts available from MBOUM chain')
    iv_values = [_safe_float(contract.get('impliedVolatility')) for contract in contracts_for_iv]
    iv_values = [value for value in iv_values if value is not None and value > 0]
    atm_iv = float(sum(iv_values) / len(iv_values)) if iv_values else 0.8
    atm_contract = contracts_for_iv[0]

    expiration_date = _epoch_to_date(chosen_expiration)
    years_to_expiry = max((expiration_date - datetime.date.today()).days / 365.0, 1 / 365.0)
    greeks = black_scholes_greeks(current_price, float(atm_contract.get('strike')), years_to_expiry, 0.05, atm_iv, 'call')
    projection = monte_carlo_projection(current_price, atm_iv, 0.05, years_to_expiry, 252, 1000)

    display_calls = _filter_display_contracts(calls)
    display_puts = _filter_display_contracts(puts)
    top_calls = sorted(display_calls, key=lambda item: (_safe_float(item.get('openInterest'), 0.0), _safe_float(item.get('volume'), 0.0)), reverse=True)[:5]
    top_puts = sorted(display_puts, key=lambda item: (_safe_float(item.get('openInterest'), 0.0), _safe_float(item.get('volume'), 0.0)), reverse=True)[:5]

    return {
        'current_price': current_price,
        'options_source': 'mboum',
        'options_chain_summary': {
            'expiration': expiration_date.isoformat(),
            'num_calls': len(calls),
            'num_puts': len(puts),
            'selected_expiration_epoch': int(chosen_expiration),
        },
        'atm_iv': atm_iv,
        'greeks': greeks,
        'mc_projection': projection,
        'options': f'Target POP {target_pop}',
        'spot_source': spot_source,
        'crypto_quote_status': crypto_meta,
        'market_snapshot': {
            'bid': _safe_float(quote.get('bid')),
            'ask': _safe_float(quote.get('ask')),
            'regularMarketVolume': _safe_float(quote.get('regularMarketVolume')),
            'averageDailyVolume10Day': _safe_float(quote.get('averageDailyVolume10Day')),
        },
        'top_calls': top_calls,
        'top_puts': top_puts,
        'vsa': 'High volume, relevant' if _safe_float(quote.get('regularMarketVolume'), 0.0) > _safe_float(quote.get('averageDailyVolume10Day'), 0.0) else 'Not relevant',
        'pbd': 'Not implemented',
    }


def analyze_with_yfinance(symbol, hist_period, target_date, target_pop):
    if yf is None:
        raise RuntimeError('yfinance not installed')
    stock = yf.Ticker(symbol)
    current_price = stock.info.get('currentPrice')
    if current_price is None:
        raise RuntimeError('No current price from yfinance')
    expirations = stock.options
    if not expirations:
        raise RuntimeError('No options data available')
    closest_expiration = min(expirations, key=lambda item: abs(datetime.date.fromisoformat(item) - target_date))
    chain = stock.option_chain(closest_expiration)
    calls = chain.calls.to_dict(orient='records')
    puts = chain.puts.to_dict(orient='records')
    atm_contract = _select_atm_contract(calls, current_price)
    if atm_contract is None:
        raise RuntimeError('No call contracts in yfinance chain')
    atm_iv = _safe_float(atm_contract.get('impliedVolatility'), 0.8)
    years_to_expiry = max((datetime.date.fromisoformat(closest_expiration) - datetime.date.today()).days / 365.0, 1 / 365.0)
    hist = stock.history(period=hist_period)
    return {
        'current_price': float(current_price),
        'options_source': 'yfinance_fallback',
        'warning': 'Using yfinance fallback because MBOUM options fetch failed',
        'options_chain_summary': {
            'expiration': closest_expiration,
            'num_calls': len(calls),
            'num_puts': len(puts),
        },
        'atm_iv': atm_iv,
        'greeks': black_scholes_greeks(float(current_price), float(atm_contract.get('strike')), years_to_expiry, 0.05, atm_iv, 'call'),
        'mc_projection': monte_carlo_projection(float(current_price), atm_iv, 0.05, years_to_expiry, 252, 1000),
        'options': f'Target POP {target_pop}',
        'top_calls': calls[:5],
        'top_puts': puts[:5],
        'vsa': 'High volume, relevant' if not hist.empty and hist['Volume'].iloc[-1] > hist['Volume'].mean() else 'Not relevant',
        'pbd': 'Not implemented',
    }


def main():
    load_env()

    parser = argparse.ArgumentParser(description='Options Projection Engine')
    parser.add_argument('tickers', nargs='*', help='Stock tickers')
    parser.add_argument('--symbols', help='Comma separated tickers')
    parser.add_argument('--date', help='Target date YYYY-MM-DD', default=str(datetime.date.today()))
    parser.add_argument('--hist-period', default='1y')
    parser.add_argument('--target-pop', default=0.80, type=float)
    args = parser.parse_args()

    tickers = args.symbols.split(',') if args.symbols else args.tickers
    tickers = [ticker.strip().upper() for ticker in tickers if ticker.strip()]
    if not tickers:
        tickers = [ticker.strip().upper() for ticker in input('Enter ticker(s) separated by comma: ').split(',') if ticker.strip()]

    target_date = datetime.date.fromisoformat(args.date)
    output = {'date': args.date, 'symbols': tickers, 'analysis': {}}
    for ticker in tickers:
        try:
            analysis = analyze_with_mboum(ticker, target_date, args.target_pop)
        except Exception as mboum_error:
            try:
                analysis = analyze_with_yfinance(ticker, args.hist_period, target_date, args.target_pop)
                analysis['mboum_error'] = str(mboum_error)
            except Exception as fallback_error:
                analysis = {
                    'error': str(fallback_error),
                    'mboum_error': str(mboum_error),
                }
        output['analysis'][ticker] = analysis

    print(json.dumps(output, indent=2))


if __name__ == '__main__':
    main()

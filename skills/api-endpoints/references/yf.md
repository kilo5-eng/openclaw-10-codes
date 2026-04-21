# yFinance (Fallback - Ignore if Possible)
- `yf.Ticker('BMNR').info` → shortPercentOfFloat (crumb issues)
- `yf.download('BMNR', period='5d')` → price/vol fallback

Avoid primary use (401 auth).
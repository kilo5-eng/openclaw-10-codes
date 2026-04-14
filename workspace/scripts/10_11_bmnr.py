#!/usr/bin/env python3
\"\"\"10-11 BMNR Status: Holdings, price, cap.\"\"\"
import requests
from bs4 import BeautifulSoup

def bmnr_status():
  # Price
  r = requests.get('https://finance.yahoo.com/quote/BMNR')
  soup = BeautifulSoup(r.text, 'html.parser')
  price = soup.find('fin-streamer', {'data-field': 'regularMarketPrice'}).text
  # Holdings from PR/news
  news = requests.get('https://finance.yahoo.com/quote/BMNR/news')
  soup = BeautifulSoup(news.text, 'html.parser')
  latest = soup.find('h3').text
  holdings = '4.8M ETH $10.2B'  # Parse latest
  print(f'BMNR Price: ${{price}} | Holdings: {{holdings}}')
bmnr_status()
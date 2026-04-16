from execution import UpstoxExecutionEngine
from config import Config
import logging
logging.basicConfig(level=logging.INFO)

engine = UpstoxExecutionEngine()
key = Config.SYMBOLS_MAPPING.get('RELIANCE', 'NSE_EQ|INE002A01018')
import requests

url = f"{engine.api_v2}/market-quote/quotes?instrument_key={key}"
print("LTP URL:", url)
response = requests.get(url, headers=engine.headers, timeout=5)
print("LTP Status:", response.status_code)
print("LTP Text:", response.text)

url2 = f"{engine.api_v3}/historical-candle/intraday/{key}/minutes/1"
print("OHLC URL:", url2)
response2 = requests.get(url2, headers=engine.headers, timeout=5)
print("OHLC Status:", response2.status_code)
print("OHLC Text:", response2.text)

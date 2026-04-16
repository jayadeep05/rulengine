from execution import UpstoxExecutionEngine
from config import Config
import requests
engine = UpstoxExecutionEngine()
key = Config.SYMBOLS_MAPPING.get('RELIANCE', 'NSE_EQ|INE002A01018')
import urllib.parse

url = f"{engine.api_v2}/market-quote/quotes?instrument_key={urllib.parse.quote(key)}"
response = requests.get(url, headers=engine.headers, timeout=5)
print("Quoted URL:", url)
print("Quoted LTP Text:", response.text)

url2 = f"{engine.api_v2}/market-quote/quotes?instrument_key={key}"
response2 = requests.get(url2, headers=engine.headers, timeout=5)
print("Unquoted URL:", url2)
print("Unquoted LTP Text:", response2.text)

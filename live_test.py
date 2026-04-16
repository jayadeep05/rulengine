from execution import UpstoxExecutionEngine
from config import Config
import logging
import requests

logging.basicConfig(level=logging.INFO)
engine = UpstoxExecutionEngine()
key = Config.SYMBOLS_MAPPING.get('RELIANCE', 'NSE_EQ|INE002A01018')

try:
    print(f"Testing LTP for: {key}")
    ltp = engine.get_ltp_bulk([key])
    print(f"LTP Response: {ltp}")
except Exception as e:
    print(f"LTP Exception: {e}")

try:
    print(f"Testing OHLC for: {key}")
    # print raw response
    url = f"{engine.api_v3}/historical-candle/intraday/{key}/minutes/1"
    response = requests.get(url, headers=engine.headers, timeout=10)
    print(f"Raw OHLC Status: {response.status_code}")
    print(f"Raw OHLC Text: {response.text[:200]}")
    
    df = engine.get_ohlc(key)
    if df is not None:
        print(f"OHLC DF Empty: {df.empty}, Shape: {df.shape}")
except Exception as e:
    print(f"OHLC Exception: {e}")

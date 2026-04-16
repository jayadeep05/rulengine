from execution import UpstoxExecutionEngine
from config import Config
import logging
import requests
import json

logging.basicConfig(level=logging.INFO)

engine = UpstoxExecutionEngine()

def verify_all():
    print("Verifying Upstox API keys for Top 5...")
    keys_to_test = list(Config.SYMBOLS_MAPPING.items())[:5]
    
    for symbol, key in keys_to_test:
        # Need to use urllib to encode key explicitly for v3 historical
        import urllib.parse
        encoded_key = urllib.parse.quote(key)
        # Using daily candles from past 5 days
        url = f"{engine.api_v3}/historical-candle/{encoded_key}/days/1/2026-04-15/2026-04-10"
        
        response = requests.get(url, headers=engine.headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            candles = data.get('data', {}).get('candles', [])
            status = "SUCCESS" if len(candles) > 0 else "NO_CANDLES"
            print(f"[{symbol}] Key: {key} -> {status} ({len(candles)} candles fetched)")
        else:
            print(f"[{symbol}] error: {response.text}")

if __name__ == "__main__":
    verify_all()

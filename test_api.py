import requests
from config import Config

headers = {
    'Accept': 'application/json',
    'Authorization': f'Bearer {Config.UPSTOX_ACCESS_TOKEN}',
}

# Based on LTP response, instrument_token = NSE_EQ|INE040A01034 is correct
# Try OHLC v3 with URL-encoded pipe
import urllib.parse

ikey = "NSE_EQ|INE040A01034"
ikey_encoded = urllib.parse.quote(ikey, safe='')

print(f"Testing with encoded key: {ikey_encoded}")

print("\n=== TEST OHLC v3 with URL-encoded key ===")
r = requests.get(f"https://api.upstox.com/v3/historical-candle/intraday/{ikey_encoded}/minutes/1", headers=headers, timeout=10)
print("Status:", r.status_code)
print("Response:", r.text[:800])

print("\n=== TEST OHLC v2 intraday ===")
r2 = requests.get(f"https://api.upstox.com/v2/historical-candle/intraday/{ikey}/1minute", headers=headers, timeout=10)
print("Status:", r2.status_code)
print("Response:", r2.text[:800])

print("\n=== TEST OHLC v2 intraday URL-encoded ===")
r3 = requests.get(f"https://api.upstox.com/v2/historical-candle/intraday/{ikey_encoded}/1minute", headers=headers, timeout=10)
print("Status:", r3.status_code)
print("Response:", r3.text[:800])

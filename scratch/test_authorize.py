import requests
import os
import sys

# Add current directory to path
sys.path.append(os.getcwd())
from config import Config

def test_authorize():
    token = Config.UPSTOX_ACCESS_TOKEN
    headers = {
        'Accept': 'application/json',
        'Authorization': f'Bearer {token}'
    }
    
    # Authorize endpoint for Portfolio Stream or Market Stream
    url = "https://api.upstox.com/v2/feed/market-data-feed/authorize"
    
    print(f"Testing Authorize URL: {url}")
    response = requests.get(url, headers=headers)
    print(f"Status Code: {response.status_code}")
    print(f"Response Body: {response.text}")

if __name__ == "__main__":
    test_authorize()

import requests
import os
import sys

# Add current directory to path
sys.path.append(os.getcwd())
from config import Config

def test_authorize_v3():
    token = Config.UPSTOX_ACCESS_TOKEN
    headers = {
        'Accept': 'application/json',
        'Authorization': f'Bearer {token}'
    }
    
    # New V3 endpoint
    url = "https://api.upstox.com/v3/feed/market-data-feed/authorize"
    
    print(f"Testing Authorize V3 URL: {url}")
    response = requests.get(url, headers=headers)
    print(f"Status Code: {response.status_code}")
    print(f"Response Body: {response.text}")

if __name__ == "__main__":
    test_authorize_v3()

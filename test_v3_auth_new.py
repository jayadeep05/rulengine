import requests
import json
import os

# Pulled from current config.py
ACCESS_TOKEN = "eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOiJza192MS4wIiwiYWxnIjoiSFMyNTYifQ.eyJzdWIiOiIzSkNLODMiLCJqdGkiOiI2OWUxYWVmYmRmYmE2NzFmOWY1Y2M3ZmUiLCJpc011bHRpQ2xpZW50IjpmYWxzZSwiaXNQbHVzUGxhbiI6dHJ1ZSwiaWF0IjoxNzc2Mzk4MDc1LCJpc3MiOiJ1ZGFwaS1nYXRld2F5LXNlcnZpY2UiLCJleHAiOjE3NzY0NjMyMDB9.90CE_RBVMnU_SRmsSJ8FjgBliFM54qtOfbUWJf3iTyA"

def test_authorize_v3():
    print("Testing Market Data Feed Authorize Url V3 API...")
    # New endpoint suggested by the error message
    url = "https://api.upstox.com/v3/feed/market-data-feed/authorize"
    headers = {
        'Accept': 'application/json',
        'Authorization': f'Bearer {ACCESS_TOKEN}'
    }
    
    try:
        response = requests.get(url, headers=headers)
        print(f"Status Code: {response.status_code}")
        print(f"Response Body: {response.text}")
        
    except Exception as e:
        print(f"Error during authorize call: {e}")

if __name__ == "__main__":
    test_authorize_v3()

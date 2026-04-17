import requests
import json
import os

# Pulled from current config.py
ACCESS_TOKEN = "eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOiJza192MS4wIiwiYWxnIjoiSFMyNTYifQ.eyJzdWIiOiIzSkNLODMiLCJqdGkiOiI2OWUxYWVmYmRmYmE2NzFmOWY1Y2M3ZmUiLCJpc011bHRpQ2xpZW50IjpmYWxzZSwiaXNQbHVzUGxhbiI6dHJ1ZSwiaWF0IjoxNzc2Mzk4MDc1LCJpc3MiOiJ1ZGFwaS1nYXRld2F5LXNlcnZpY2UiLCJleHAiOjE3NzY0NjMyMDB9.90CE_RBVMnU_SRmsSJ8FjgBliFM54qtOfbUWJf3iTyA"

def test_authorize_v3():
    print("Testing Market Data Feed Authorize Url V3 API...")
    url = "https://api.upstox.com/v2/feed/market-data-feed/authorize"
    headers = {
        'Accept': 'application/json',
        'Authorization': f'Bearer {ACCESS_TOKEN}'
    }
    
    try:
        response = requests.get(url, headers=headers)
        print(f"Status Code: {response.status_code}")
        print(f"Response Body: {response.text}")
        
        if response.status_code == 200:
            print("Successfully got authorized URL!")
            data = response.json()
            auth_url = data.get('data', {}).get('authorizedRedirectUri')
            print(f"Authorized URL: {auth_url}")
        else:
            print("FAILED to authorize V3 Market Data Feed.")
            
    except Exception as e:
        print(f"Error during authorize call: {e}")

if __name__ == "__main__":
    test_authorize_v3()

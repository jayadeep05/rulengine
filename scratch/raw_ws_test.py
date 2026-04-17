import requests
import websocket
import ssl
import os
import sys

# Add current directory to path
sys.path.append(os.getcwd())
from config import Config

def test_raw_ws():
    token = Config.UPSTOX_ACCESS_TOKEN
    headers = {
        'Accept': 'application/json',
        'Authorization': f'Bearer {token}'
    }
    
    # Get authorized URL
    auth_url = "https://api.upstox.com/v3/feed/market-data-feed/authorize"
    print(f"Authorizing v3...")
    resp = requests.get(auth_url, headers=headers)
    if resp.status_code != 200:
        print(f"Auth failed: {resp.status_code} - {resp.text}")
        return
        
    ws_url = resp.json()['data']['authorized_redirect_uri']
    print(f"WS URL: {ws_url}")
    
    def on_message(ws, message):
        print(f"Message received: {len(message)} bytes")

    def on_error(ws, error):
        print(f"WS Error: {error}")

    def on_close(ws, close_status_code, close_msg):
        print(f"WS Closed: {close_status_code} - {close_msg}")

    def on_open(ws):
        print("WS Opened!")
        # Subscription would happen here via protobuf, but let's just see if it connects
        
    print("Connecting to WebSocket...")
    ws_headers = {
        'Authorization': f'Bearer {token}',
        'Api-Version': '2.0' # Or 3.0?
    }
    ws = websocket.WebSocketApp(ws_url,
                              on_open=on_open,
                              on_message=on_message,
                              on_error=on_error,
                              on_close=on_close,
                              header=ws_headers)

    ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})

if __name__ == "__main__":
    test_raw_ws()

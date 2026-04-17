"""
debug_ws.py — One-shot script to print the raw message structure from the WS feed.
Run once to see exactly what keys the SDK delivers to the on_message callback.
"""
import time
import upstox_client
from upstox_client.feeder.market_data_streamer_v3 import MarketDataStreamerV3

ACCESS_TOKEN = "eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOiJza192MS4wIiwiYWxnIjoiSFMyNTYifQ.eyJzdWIiOiIzSkNLODMiLCJqdGkiOiI2OWUxYjViNzBmYmRjOTYxYTJmYTAwZWUiLCJpc011bHRpQ2xpZW50IjpmYWxzZSwiaXNQbHVzUGxhbiI6dHJ1ZSwiaXNFeHRlbmRlZCI6dHJ1ZSwiaWF0IjoxNzc2Mzk5Nzk5LCJpc3MiOiJ1ZGFwaS1nYXRld2F5LXNlcnZpY2UiLCJleHAiOjE4MDc5OTkyMDB9.OqrjBCEcoTyXNhVacPMOqVdHK6uSLJzLB7kaAVzoIi8"

got_message = False

def on_message(message):
    global got_message
    if not got_message:
        got_message = True
        print("\n===== RAW MESSAGE TYPE =====")
        print(f"Type: {type(message)}")
        print("\n===== RAW MESSAGE CONTENT =====")
        print(repr(message))
        print("\n===== MESSAGE KEYS (if dict) =====")
        if isinstance(message, dict):
            print(f"Top-level keys: {list(message.keys())}")
            for k, v in message.items():
                print(f"  {k}: {type(v)} = {repr(v)[:200]}")
        else:
            # If it's not a dict, it may be a protobuf object
            print("NOT A DICT - trying attributes:")
            print(dir(message))

def on_open(*args):
    print("CONNECTED!")

def on_error(e):
    print(f"ERROR: {e}")

config = upstox_client.Configuration()
config.access_token = ACCESS_TOKEN
api_client = upstox_client.ApiClient(configuration=config)

streamer = MarketDataStreamerV3(
    api_client,
    instrumentKeys=["NSE_EQ|INE002A01018"],  # Just RELIANCE for quick test
    mode="ltpc"
)

E = MarketDataStreamerV3.Event
streamer.on(E['OPEN'], on_open)
streamer.on(E['MESSAGE'], on_message)
streamer.on(E['ERROR'], on_error)

streamer.auto_reconnect(enable=False)
streamer.connect()

print("Waiting 15 seconds for messages...")
time.sleep(15)
print("DONE")

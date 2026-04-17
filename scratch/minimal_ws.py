import logging
import time
import upstox_client
from upstox_client.feeder.market_data_streamer_v3 import MarketDataStreamerV3
import os
import sys

# Add current directory to path
sys.path.append(os.getcwd())
from config import Config

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def on_open():
    logger.info("✅ Connection Open")

def on_message(message):
    logger.info(f"📈 Tick: {message}")

def on_error(error):
    logger.error(f"❌ Error: {error}")

def on_close(ws, code, reason):
    logger.warning(f"⚠️ Closed: {code} - {reason}")

def test_minimal():
    conf = upstox_client.Configuration()
    conf.host = "https://api.upstox.com/v3"
    conf.access_token = Config.UPSTOX_ACCESS_TOKEN
    api_client = upstox_client.ApiClient(conf)
    
    # Just 2 instruments
    keys = ["NSE_EQ|INE002A01018", "NSE_INDEX|Nifty 50"]
    
    streamer = MarketDataStreamerV3(api_client, instrumentKeys=keys, mode="ltpc")
    
    streamer.on("open", on_open)
    streamer.on("message", on_message)
    streamer.on("error", on_error)
    streamer.on("close", on_close)
    
    logger.info("Attempting minimal connect...")
    streamer.connect()

if __name__ == "__main__":
    test_minimal()

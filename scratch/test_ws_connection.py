import logging
import time
import os
import sys

# Add current directory to path
sys.path.append(os.getcwd())

from market_feed import start, is_live
from config import Config

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def test_websocket():
    prices = {}
    indices = {}
    
    logger.info("Starting WebSocket Test...")
    start(prices, indices)
    
    # Wait for up to 20 seconds to see if we get a connection and some data
    timeout = 20
    start_time = time.time()
    
    while time.time() - start_time < timeout:
        if is_live():
            logger.info("✅ WebSocket is LIVE and receiving data!")
            logger.info(f"Sample prices: {list(prices.items())[:3]}")
            logger.info(f"Sample indices: {list(indices.items())[:3]}")
            return True
        
        time.sleep(2)
        logger.info("Waiting for first tick...")
    
    logger.warning("❌ WebSocket did not receive data within 20 seconds.")
    logger.info("Note: If the market is closed (current time: {}), this might be expected unless Upstox sends heartbeats/indices.".format(time.strftime('%H:%M:%S')))
    return False

if __name__ == "__main__":
    test_websocket()

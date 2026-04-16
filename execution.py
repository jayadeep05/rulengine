import logging
import requests
import pandas as pd
from datetime import datetime, date, timedelta
from config import Config

logger = logging.getLogger(__name__)

class UpstoxExecutionEngine:
    def __init__(self):
        self.api_v2 = "https://api.upstox.com/v2"
        self.api_v3 = "https://api.upstox.com/v3"
        self.headers = {
            'Accept': 'application/json',
            'Authorization': f'Bearer {Config.UPSTOX_ACCESS_TOKEN}',
        }
        
    def connect(self):
        logger.info("Initializing Upstox API Connection...")
        try:
            res = requests.get(f"{self.api_v2}/user/profile", headers=self.headers, timeout=10)
            if res.status_code == 200:
                logger.info("Connected to Upstox API successfully.")
                return True
            else:
                logger.error(f"Failed to connect to Upstox API: {res.text}")
                return False
        except Exception as e:
            logger.error(f"Upstox connection error: {e}")
            return False

    def get_ltp(self, instrument_key: str) -> float:
        """Fetch real-time Last Traded Price (LTP) using Full Market Quotes API."""
        try:
            url = f"{self.api_v2}/market-quote/quotes?instrument_key={instrument_key}"
            response = requests.get(url, headers=self.headers, timeout=5)
            response.raise_for_status()
            data = response.json()
            
            if 'data' in data and len(data['data']) > 0:
                # Upstox returns keys like "NSE_EQ:NHPC", just grab the first available value
                quote_data = list(data['data'].values())[0]
                if 'last_price' in quote_data:
                    return float(quote_data['last_price'])
                    
            logger.error(f"LTP not found in response for {instrument_key}")
        except Exception as e:
            logger.error(f"Error fetching LTP for {instrument_key}: {e}")
        return 0.0

    def get_ltp_bulk(self, instrument_keys: list) -> dict:
        """Fetch multiple LTPs in one REST sweep to avoid rate limiting UI tracking."""
        prices = {}
        try:
            if not instrument_keys:
                return prices
            keys_str = ",".join(instrument_keys)
            url = f"{self.api_v2}/market-quote/quotes?instrument_key={keys_str}"
            response = requests.get(url, headers=self.headers, timeout=5)
            response.raise_for_status()
            data = response.json()
            
            if 'data' in data:
                # Upstox returns keys like "NSE_EQ:RELIANCE" but we send "NSE_EQ|INE002A01018"
                # Build a fast reverse lookup: convert pipe-format keys to colon-format for matching
                # e.g. "NSE_EQ|INE002A01018" -> we DON'T know ticker, so match by colon key prefix
                # Upstox also accepts instrument_key in response as-is in some cases.
                # Most reliable: match by raw_key directly replacing colon with pipe.
                key_map = {}
                for k in instrument_keys:
                    # "NSE_EQ|INE002A01018" -> try "NSE_EQ:INE002A01018" as potential return key
                    colon_version = k.replace('|', ':')
                    key_map[colon_version] = k
                    key_map[k] = k  # also direct match

                for raw_key, quote_data in data['data'].items():
                    if 'last_price' in quote_data:
                        matched_key = key_map.get(raw_key)
                        if matched_key:
                            prices[matched_key] = float(quote_data['last_price'])
                        else:
                            # Fallback: match by the part after colon/pipe, and use Config.SYMBOLS_MAPPING
                            suffix = raw_key.split(':')[-1].split('|')[-1]
                            
                            # E.g. raw_key="NSE_EQ:RELIANCE" -> suffix="RELIANCE"
                            if suffix in Config.SYMBOLS_MAPPING:
                                mapped_key = Config.SYMBOLS_MAPPING[suffix]
                                if mapped_key in instrument_keys:
                                    prices[mapped_key] = float(quote_data['last_price'])
                                    continue
                            
                            for k in instrument_keys:
                                if suffix in k:
                                    prices[k] = float(quote_data['last_price'])
                                    break
        except Exception as e:
            logger.warning(f"LTP bulk fetch error: {e}")
        return prices

    def get_ohlc(self, instrument_key: str, interval: str = "1minute") -> pd.DataFrame:
        """
        Fetch historical candles using Intraday Candle Data V3 API.
        Unit: minutes, Interval: 1
        """
        try:
            url = f"{self.api_v3}/historical-candle/intraday/{instrument_key}/minutes/1"
            
            response = requests.get(url, headers=self.headers, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if 'data' in data and 'candles' in data['data']:
                candles = data['data']['candles']
                
                # Format: [timestamp, open, high, low, close, volume, open_interest]
                df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'oi'])
                
                # Upstox V3 returns chronological newest first, reverse it to oldest first for pandas rolling features
                df = df.iloc[::-1].reset_index(drop=True)
                
                df['timestamp'] = pd.to_datetime(df['timestamp'])
                for col in ['open', 'high', 'low', 'close', 'volume']:
                    df[col] = df[col].astype(float)
                    
                return df
            else:
                logger.error(f"No candlestick data returned for {instrument_key}: {data}")
                return pd.DataFrame()
        except requests.exceptions.HTTPError as he:
            logger.error(f"HTTP Error fetching OHLC for {instrument_key}: {he.response.text}")
        except Exception as e:
            logger.error(f"Error fetching OHLC for {instrument_key}: {e}")
        
        return pd.DataFrame()

    def place_order_with_retry(self, symbol: str, side: str, qty: int, order_type: str = 'MARKET', price: float = 0.0, max_retries: int = 1) -> dict:
        transaction_type = "BUY" if side.upper() == "BUY" else "SELL"
        
        payload = {
            "quantity": qty,
            "product": "I",
            "validity": "DAY",
            "price": price,
            "tag": "intBot",
            "instrument_token": symbol,
            "order_type": order_type,
            "transaction_type": transaction_type,
            "disclosed_quantity": 0,
            "trigger_price": 0,
            "is_amo": False,
            "slice": False
        }

        retries = 0
        while retries <= max_retries:
            logger.info(f"[{Config.MODE}] API Call: Place {side} {order_type} for {symbol}, Qty: {qty}. Attempt {retries + 1}")
            
            if Config.MODE == "TEST":
                logger.info(f"TEST MODE: Simulated successful {side} execution.")
                return {"status": "success", "order_id": f"test_ord_{symbol}_{side}_{retries}"}
            
            try:
                url = f"{self.api_v3}/order/place"
                headers = {**self.headers, 'Content-Type': 'application/json'}
                response = requests.post(url, headers=headers, json=payload, timeout=10)
                
                response_data = response.json()
                logger.info(f"Upstox Place Order API Response: {response_data}")
                
                if response.status_code == 200 and response_data.get('status') == 'success':
                    # Extract order_ids from V3 format which returns 'order_ids' list or 'order_id'
                    order_ids = response_data['data'].get('order_ids', [])
                    order_id = order_ids[0] if order_ids else response_data['data'].get('order_id')
                    return {"status": "success", "order_id": order_id}
                else:
                    logger.error(f"Upstox Order Rejected: {response_data}")
                    retries += 1
                    
            except Exception as e:
                logger.error(f"API Crash during order placement: {str(e)}")
                retries += 1
                
        logger.error(f"Failed to place {side} order for {symbol} after {max_retries} retries.")
        return {"status": "error", "reason": "Max retries exceeded"}

    def place_stop_loss(self, symbol: str, side: str, qty: int, sl_price: float) -> dict:
        transaction_type = "SELL" if side.upper() == "BUY" else "BUY"
        logger.info(f"[{Config.MODE}] Placing Stop Loss ({transaction_type}) at {sl_price} for {symbol}")
        
        rounded_sl = round(sl_price * 20) / 20.0
        
        payload = {
            "quantity": qty,
            "product": "I",
            "validity": "DAY",
            "price": 0,
            "tag": "intBotSL",
            "instrument_token": symbol,
            "order_type": "SL-M",
            "transaction_type": transaction_type,
            "disclosed_quantity": 0,
            "trigger_price": rounded_sl,
            "is_amo": False,
            "slice": False
        }

        if Config.MODE == "TEST":
            logger.info(f"TEST MODE: Simulated SL placement at {rounded_sl}.")
            return {"status": "success", "order_id": f"test_sl_{symbol}_{side}"}
            
        try:
            url = f"{self.api_v3}/order/place"
            headers = {**self.headers, 'Content-Type': 'application/json'}
            response = requests.post(url, headers=headers, json=payload, timeout=10)
            response_data = response.json()
            
            if response.status_code == 200 and response_data.get('status') == 'success':
                order_ids = response_data['data'].get('order_ids', [])
                order_id = order_ids[0] if order_ids else response_data['data'].get('order_id')
                return {"status": "success", "order_id": order_id}
            else:
                logger.error(f"Upstox SL Order Rejected: {response_data}")
                return {"status": "error"}
                
        except Exception as e:
            logger.error(f"API Crash during SL placement: {str(e)}")
            return {"status": "error", "reason": str(e)}

    def modify_order(self, order_id: str, new_price: float, new_trigger: float, qty: int, order_type: str = "SL-M") -> dict:
        if Config.MODE == "TEST":
            logger.info(f"TEST MODE: Modifying order {order_id}.")
            return {"status": "success"}

        payload = {
            "order_id": order_id,
            "quantity": qty,
            "validity": "DAY",
            "price": new_price,
            "order_type": order_type,
            "disclosed_quantity": 0,
            "trigger_price": new_trigger
        }
        try:
            url = f"{self.api_v3}/order/modify"
            headers = {**self.headers, 'Content-Type': 'application/json'}
            response = requests.put(url, headers=headers, json=payload, timeout=10)
            response_data = response.json()
            
            if response.status_code == 200 and response_data.get('status') == 'success':
                return {"status": "success"}
            else:
                logger.error(f"Failed to modify order {order_id}: {response_data}")
                return {"status": "error"}
        except Exception as e:
            logger.error(f"API Crash modifying {order_id}: {e}")
            return {"status": "error"}

    def cancel_order(self, order_id: str) -> dict:
        if Config.MODE == "TEST":
            logger.info(f"TEST MODE: Canceling order {order_id}.")
            return {"status": "success"}

        try:
            url = f"{self.api_v3}/order/cancel?order_id={order_id}"
            response = requests.delete(url, headers=self.headers, timeout=10)
            response_data = response.json()
            if response.status_code == 200 and response_data.get('status') == 'success':
                return {"status": "success"}
            else:
                logger.error(f"Failed to cancel {order_id}: {response_data}")
                return {"status": "error"}
        except Exception as e:
            logger.error(f"API Crash canceling {order_id}: {e}")
            return {"status": "error"}

    def exit_all_positions(self) -> dict:
        if Config.MODE == "TEST":
            logger.info("TEST MODE: Simulated Exit All Positions.")
            return {"status": "success"}
            
        try:
            url = f"{self.api_v2}/order/positions/exit"
            headers = {**self.headers, 'Content-Type': 'application/json'}
            # Payload is empty or can pass tag='intBot'
            response = requests.post(url, headers=headers, json={}, timeout=10)
            response_data = response.json()
            if response.status_code in [200, 207] and response_data.get('status') in ['success', 'partial_success']:
                logger.info(f"Successfully sent exit-all command: {response_data}")
                return {"status": "success", "data": response_data}
            else:
                logger.error(f"Exit all positions failed: {response_data}")
                return {"status": "error"}
        except Exception as e:
            logger.error(f"API Crash extending all positions: {e}")
            return {"status": "error"}

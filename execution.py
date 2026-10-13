import logging
import time as _time
import requests
import pandas as pd
from datetime import datetime, date, timedelta
from config import Config

logger = logging.getLogger(__name__)

class ExecutionSafetyViolation(Exception):
    """Raised when an operation violates trading safety, credentials, or paper isolation."""
    pass

class UpstoxExecutionEngine:
    def __init__(self, credentials: dict = None, mode: str = None):
        """
        Args:
            credentials: dict with keys upstox_access_token, analytics_token,
                         upstox_client_id, upstox_client_secret.
                         If None, falls back to Config (legacy single-user mode).
            mode: "TEST" or "LIVE". If None, defaults to Config.MODE.
        """
        self.api_v2  = "https://api.upstox.com/v2"
        self.api_v3  = "https://api.upstox.com/v3"
        # Official Upstox docs: https://upstox.com/developer/api-documentation/v3/place-order
        # Correct URL: https://api-hft.upstox.com/v2/order/place  (V2 is the correct HFT endpoint)
        self.api_hft = "https://api-hft.upstox.com/v2"
        self._creds = credentials  # per-user credentials dict or None
        self.mode = mode or Config.MODE

    @property
    def is_paper(self) -> bool:
        """Isolated paper mode is active if mode is TEST or PAPER_TRADING_MODE is True."""
        return (str(self.mode).upper() == "TEST") or getattr(Config, "PAPER_TRADING_MODE", False)

    def _get_headers(self, use_analytics=False):
        if self._creds:
            token = self._creds.get("analytics_token", "") if use_analytics else self._creds.get("upstox_access_token", "")
            if not token:  # fall through to the other token if one is empty
                token = self._creds.get("upstox_access_token", "") or self._creds.get("analytics_token", "")
        else:
            token = Config.UPSTOX_ACCESS_TOKEN
            if use_analytics and Config.ANALYTICS_TOKEN:
                token = Config.ANALYTICS_TOKEN
        return {
            'Accept': 'application/json',
            'Authorization': f'Bearer {token}',
        }


    def connect(self):
        logger.info("Initializing Upstox API Connection...")
        try:
            res = requests.get(f"{self.api_v2}/user/profile", headers=self._get_headers(), timeout=10)
            if res.status_code == 200:
                logger.info("Connected to Upstox API successfully.")
                return True
            else:
                logger.error(f"Failed to connect to Upstox API: {res.text}")
                return False
        except Exception as e:
            logger.error(f"Upstox connection error: {e}")
            return False

    def get_funds(self, force_live: bool = False) -> float:
        """Fetch real user balance (available margin) if LIVE mode is enabled or force_live is True."""
        if force_live or getattr(Config, 'USE_LIVE_CAPITAL', False):
            try:
                url = f"{self.api_v2}/user/get-funds-and-margin"
                res = requests.get(url, headers=self._get_headers(), timeout=5)
                if res.status_code == 200:
                    data = res.json()
                    if 'data' in data and 'equity' in data['data']:
                        return float(data['data']['equity'].get('available_margin', 0))
                else:
                    logger.warning(f"Upstox Funds API returned {res.status_code}: {res.text}")
            except Exception as e:
                logger.error(f"Error fetching live funds: {e}")
            return 0.0  # If live capital fails, do NOT fallback to manual capital for safety
        return getattr(Config, 'MANUAL_CAPITAL', 10000.0)

    def get_ltp(self, instrument_key: str) -> float:
        """Fetch real-time Last Traded Price (LTP) using Full Market Quotes API."""
        try:
            url = f"{self.api_v2}/market-quote/quotes?instrument_key={instrument_key}"
            # Indices can use analytics token
            use_analytics = "INDEX" in instrument_key
            response = requests.get(url, headers=self._get_headers(use_analytics), timeout=5)
            
            if response.status_code == 401 and not use_analytics:
                # Try fallback to analytics for any quote if main fails
                response = requests.get(url, headers=self._get_headers(True), timeout=5)
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
            # Check if any index keys are present
            use_analytics = any("INDEX" in k for k in instrument_keys) if 'instrument_keys' in locals() else False
            response = requests.get(url, headers=self._get_headers(use_analytics), timeout=5)
            
            if response.status_code == 401 and not use_analytics:
                response = requests.get(url, headers=self._get_headers(True), timeout=5)
                
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

    def get_market_quotes(self, instrument_keys: list) -> dict:
        """Fetch full quote data (OHLC, LTP, etc.) for multiple instruments."""
        quotes = {}
        try:
            if not instrument_keys:
                return quotes
            keys_str = ",".join(instrument_keys)
            url = f"{self.api_v2}/market-quote/quotes?instrument_key={keys_str}"
            use_analytics = any("INDEX" in k for k in instrument_keys)
            response = requests.get(url, headers=self._get_headers(use_analytics), timeout=5)
            
            if response.status_code == 401 and not use_analytics:
                response = requests.get(url, headers=self._get_headers(True), timeout=5)

            response.raise_for_status()
            data = response.json()
            
            if 'data' in data:
                # Map by colon and pipe versions
                key_map = {}
                for k in instrument_keys:
                    key_map[k.replace('|', ':')] = k
                    key_map[k] = k

                for raw_key, quote_data in data['data'].items():
                    matched_key = key_map.get(raw_key)
                    if matched_key:
                        quotes[matched_key] = quote_data
                    else:
                        # Fallback for ISIN-based keys mapped to ticker name in API response
                        suffix = raw_key.split(':')[-1].split('|')[-1]
                        if suffix in Config.SYMBOLS_MAPPING:
                            mapped_key = Config.SYMBOLS_MAPPING[suffix]
                            if mapped_key in instrument_keys:
                                quotes[mapped_key] = quote_data
                                continue
                        for k in instrument_keys:
                            if suffix in k:
                                quotes[k] = quote_data
                                break
        except Exception as e:
            logger.warning(f"Market quotes fetch error: {e}")
        return quotes

    def get_ohlc(self, instrument_key: str, interval: str = "1minute") -> pd.DataFrame:
        """
        Fetch historical candles using Intraday Candle Data V3 API.
        Unit: minutes, Interval: 1
        """
        try:
            url = f"{self.api_v3}/historical-candle/intraday/{instrument_key}/minutes/1"
            
            response = requests.get(url, headers=self._get_headers(True), timeout=10) # OHLC often works with analytics token
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
                    
                if interval != "1minute":
                    resample_map = {"5minute": "5min", "15minute": "15min", "30minute": "30min", "60minute": "60min"}
                    if interval in resample_map:
                        df = df.set_index('timestamp').resample(resample_map[interval]).agg({
                            'open': 'first',
                            'high': 'max',
                            'low': 'min',
                            'close': 'last',
                            'volume': 'sum',
                            'oi': 'last'
                        }).dropna().reset_index()
                        
                return df
            else:
                logger.error(f"No candlestick data returned for {instrument_key}: {data}")
                return pd.DataFrame()
        except requests.exceptions.HTTPError as he:
            logger.error(f"HTTP Error fetching OHLC for {instrument_key}: {he.response.text}")
        except Exception as e:
            logger.error(f"Error fetching OHLC for {instrument_key}: {e}")
        
        return pd.DataFrame()

    def get_order_book(self) -> list:
        if self.mode == "TEST":
            return []
        try:
            url = f"{self.api_v2}/order/retrieve-all"
            response = requests.get(url, headers=self._get_headers(), timeout=10)
            if response.status_code == 200:
                data = response.json()
                return data.get('data', [])
            else:
                logger.error(f"Failed to fetch order book: {response.text}")
        except Exception as e:
            logger.error(f"Error fetching order book: {e}")
        return []

    def get_order_status(self, order_id: str) -> dict:
        """
        Retrieves status of a specific order.
        """
        if self.mode == "TEST":
            return {"status": "success", "order_status": "complete", "filled_quantity": 0}
        try:
            orders = self.get_order_book()
            for o in orders:
                if o.get('order_id') == order_id:
                    return {
                        "status": "success",
                        "order_status": o.get('status'),
                        "filled_quantity": int(o.get('filled_quantity', 0)),
                        "quantity": int(o.get('quantity', 0)),
                        "average_price": float(o.get('average_price', 0.0))
                    }
        except Exception as e:
            logger.error(f"Error fetching order status for {order_id}: {e}")
        return {"status": "error", "reason": "Order not found or API error"}

    def verify_order_by_details(self, instrument_token: str, transaction_type: str, qty: int, max_age_seconds: int = 60) -> dict:
        """
        Checks order book to see if a matching order was placed within the last max_age_seconds.
        """
        try:
            orders = self.get_order_book()
            import pytz
            from datetime import datetime
            now = datetime.now(pytz.timezone('Asia/Kolkata'))
            for o in orders:
                if o.get('instrument_token') == instrument_token and o.get('transaction_type') == transaction_type and int(o.get('quantity', 0)) == qty:
                    ts_str = o.get('order_timestamp')
                    if ts_str:
                        try:
                            # Upstox timestamp: '2026-06-27T15:02:40+05:30'
                            if ts_str.endswith('+05:30'):
                                ts_str = ts_str[:-6] + '+0530'
                            ts = datetime.strptime(ts_str[:19], "%Y-%m-%dT%H:%M:%S")
                            ts = pytz.timezone('Asia/Kolkata').localize(ts)
                            age = (now - ts).total_seconds()
                            if age <= max_age_seconds:
                                order_status = str(o.get('status', '')).lower()
                                if order_status in ('complete', 'traded', 'filled'):
                                    return {
                                        "status": "success", 
                                        "order_id": o.get('order_id'), 
                                        "order_status": order_status, 
                                        "filled_quantity": int(o.get('filled_quantity', 0))
                                    }
                                elif order_status in ('rejected', 'cancelled'):
                                    return {
                                        "status": "rejected",
                                        "order_id": o.get('order_id'),
                                        "order_status": order_status,
                                        "reason": o.get('status_message', 'Rejected by broker')
                                    }
                                else:
                                    # It might be open/pending
                                    return {
                                        "status": "pending",
                                        "order_id": o.get('order_id'),
                                        "order_status": order_status
                                    }
                        except Exception as e:
                            logger.error(f"Error parsing order timestamp {ts_str}: {e}")
        except Exception as e:
            logger.error(f"Error fetching order book for verification: {e}")
        return {"status": "error", "reason": "Matching order not found in book"}

    def place_order_with_retry(self, symbol: str, side: str, qty: int, order_type: str = 'MARKET', price: float = 0.0, max_retries: int = 1) -> dict:
        """Place order with retry logic and ghost position protection (Bug Fix 1 & 2)."""
        transaction_type = "BUY" if side.upper() == "BUY" else "SELL"
        
        payload = {
            "quantity":          qty,
            "product":           "I",          # I = Intraday (MIS)
            "validity":          "DAY",
            "price":             price,
            "tag":               "intBot",
            "instrument_token":  symbol,
            "order_type":        order_type,   # MARKET / LIMIT / SL / SL-M
            "transaction_type":  transaction_type,
            "disclosed_quantity": 0,
            "trigger_price":     0,
            "is_amo":            False,
            "slice":             False,
            # market_protection: -1 = auto (per Upstox V3 docs)
            # 0 = no protection → exchange REJECTS market orders via API
            "market_protection": -1
        }

        retries = 0
        last_order_id = None  # Track any order_id we get back for verification
        
        while retries <= max_retries:
            logger.info(f"[{'PAPER' if self.is_paper else self.mode}] API Call: Place {side} {order_type} for {symbol}, Qty: {qty}. Attempt {retries + 1}")
            
            if self.is_paper:
                logger.info(f"PAPER MODE ACTIVE: Simulated successful {side} execution for {symbol}. Zero broker network calls made.")
                return {
                    "status": "success",
                    "order_id": f"paper_ord_{symbol}_{side}_{retries}_{int(_time.time()*1000)}",
                    "filled_quantity": qty,
                    "average_price": price or 0.0,
                    "paper_trade": True
                }
            
            # Strict safety check before ANY live broker call
            auth_token = self._get_headers().get("Authorization", "").replace("Bearer ", "").strip()
            if not auth_token:
                logger.error("LIVE EXECUTION BLOCKED: Missing live Upstox access token. Refusing order placement.")
                return {"status": "rejected", "reason": "Missing live Upstox access token"}
            
            try:
                # Use HFT endpoint for intraday order placement (product='I')
                url = f"{self.api_hft}/order/place"
                headers = {**self._get_headers(), 'Content-Type': 'application/json'}
                logger.info(f"Placing order via HFT endpoint: {url}")
                response = requests.post(url, headers=headers, json=payload, timeout=10)
                
                response_data = response.json()
                logger.info(f"Upstox Place Order API Response: {response_data}")
                
                if response.status_code == 200 and response_data.get('status') == 'success':
                    # Extract order_ids from V3 format which returns 'order_ids' list or 'order_id'
                    order_ids = response_data['data'].get('order_ids', [])
                    order_id = order_ids[0] if order_ids else response_data['data'].get('order_id')
                    last_order_id = order_id
                    
                    # Bug Fix 2: Fetch actual filled_quantity from order status
                    _time.sleep(0.5)  # Brief wait for exchange fill
                    status_res = self.get_order_status(order_id)
                    filled_qty = qty  # Default to requested qty
                    avg_price = 0.0
                    if status_res.get('status') == 'success':
                        filled_qty = status_res.get('filled_quantity', qty)
                        avg_price = status_res.get('average_price', 0.0)
                    
                    return {
                        "status": "success", "order_id": order_id,
                        "filled_quantity": filled_qty, "average_price": avg_price
                    }
                else:
                    logger.error(f"Upstox Order Rejected: {response_data}")
                    return {
                        "status": "rejected",
                        "reason": response_data.get('errors', [{}])[0].get('message', 'Rejected') if response_data.get('errors') else 'Rejected'
                    }
                    
            except Exception as e:
                logger.error(f"API Crash/Timeout during order placement: {str(e)}")
                
                # Bug Fix 1: Ghost Position Protection
                # Wait 2 seconds, then verify actual server-side status
                logger.info("[BUG FIX 1] Waiting 2s before verifying order status on Upstox...")
                _time.sleep(2)
                
                # First try: Check by order_id if we have one
                if last_order_id:
                    status_res = self.get_order_status(last_order_id)
                    if status_res.get('status') == 'success':
                        order_status = status_res.get('order_status', '').lower()
                        if order_status in ('complete', 'traded', 'filled'):
                            logger.info(f"[BUG FIX 1] Order {last_order_id} confirmed FILLED on Upstox despite timeout.")
                            return {
                                "status": "success", "order_id": last_order_id,
                                "filled_quantity": status_res.get('filled_quantity', qty),
                                "average_price": status_res.get('average_price', 0.0)
                            }
                
                # Second try: Check order book by details
                verify_res = self.verify_order_by_details(symbol, transaction_type, qty)
                if verify_res.get('status') == 'success':
                    logger.info(f"[BUG FIX 1] Verified order exists in book: {verify_res}")
                    return {
                        "status": "success", "order_id": verify_res['order_id'],
                        "filled_quantity": verify_res.get('filled_quantity', qty),
                        "average_price": 0.0
                    }
                
                # If status check also fails → UNKNOWN status
                logger.critical(
                    f"[BUG FIX 1] ORDER STATUS UNKNOWN for {symbol} ({side}). "
                    f"Order ID: {last_order_id or 'N/A'}. Manual check required!"
                )
                return {
                    "status": "unknown",
                    "reason": "Order status could not be verified after timeout",
                    "order_id": last_order_id,
                    "symbol": symbol
                }
                
        # Final safety verification check
        logger.info("Final verification check in order book before declaring failure...")
        verify_res = self.verify_order_by_details(symbol, transaction_type, qty)
        if verify_res.get('status') == 'success':
            logger.info(f"Final verify confirmed order: {verify_res}")
            return {
                "status": "success", "order_id": verify_res['order_id'],
                "filled_quantity": verify_res.get('filled_quantity', qty),
                "average_price": 0.0
            }

        logger.error(f"Failed to place {side} order for {symbol} after {max_retries} retries.")
        return {"status": "error", "reason": "Max retries exceeded"}

    def place_stop_loss(self, symbol: str, side: str, qty: int, sl_price: float) -> dict:
        transaction_type = "SELL" if side.upper() == "BUY" else "BUY"
        rounded_sl = round(sl_price * 20) / 20.0
        
        # Calculate limit price with 0.1% / 3-tick (0.15) buffer (Fix A)
        buffer = max(0.15, rounded_sl * 0.001)
        if transaction_type == "SELL":
            limit_price = round((rounded_sl - buffer) * 20) / 20.0
        else:
            limit_price = round((rounded_sl + buffer) * 20) / 20.0

        logger.info(f"[{self.mode}] Placing Stop Loss Limit ({transaction_type}) trigger: {rounded_sl}, limit: {limit_price} for {symbol}")

        payload = {
            "quantity": qty,
            "product": "I",
            "validity": "DAY",
            "price": limit_price,
            "tag": "intBotSL",
            "instrument_token": symbol,
            "order_type": "SL",
            "transaction_type": transaction_type,
            "disclosed_quantity": 0,
            "trigger_price": rounded_sl,
            "is_amo": False,
            "slice": False
        }

        if self.is_paper:
            logger.info(f"PAPER MODE ACTIVE: Simulated SL placement at {rounded_sl} (limit {limit_price}) for {symbol}. Zero broker network calls made.")
            return {"status": "success", "order_id": f"paper_sl_{symbol}_{side}_{int(_time.time()*1000)}", "paper_trade": True}

        # Strict safety check before ANY live broker call
        auth_token = self._get_headers().get("Authorization", "").replace("Bearer ", "").strip()
        if not auth_token:
            logger.error("LIVE EXECUTION BLOCKED: Missing live Upstox access token for SL. Refusing order placement.")
            return {"status": "rejected", "reason": "Missing live Upstox access token"}

        try:
            # Use HFT endpoint for intraday SL order placement
            url = f"{self.api_hft}/order/place"
            headers = {**self._get_headers(), 'Content-Type': 'application/json'}
            logger.info(f"Placing SL order via HFT endpoint: {url}")
            response = requests.post(url, headers=headers, json=payload, timeout=10)
            response_data = response.json()
            
            if response.status_code == 200 and response_data.get('status') == 'success':
                order_ids = response_data['data'].get('order_ids', [])
                order_id = order_ids[0] if order_ids else response_data['data'].get('order_id')
                return {"status": "success", "order_id": order_id}
            else:
                logger.error(f"Upstox SL Order Rejected: {response_data}")
                reason = "Unknown rejection"
                errors = response_data.get('errors', [])
                if errors and isinstance(errors, list) and len(errors) > 0:
                    reason = errors[0].get('message', 'Rejected')
                
                # Check for circuit limit keywords (e.g. price range, price band, circuit, limit)
                circuit_keywords = ["price range", "price band", "circuit", "daily limit", "trigger price limit", "outside range", "daily price range"]
                is_circuit = any(kw in reason.lower() for kw in circuit_keywords)
                
                if is_circuit:
                    return {"status": "circuit_limit", "reason": reason}
                return {"status": "error", "reason": reason}
                
        except Exception as e:
            logger.error(f"API Crash during SL placement: {str(e)}")
            return {"status": "error", "reason": str(e)}

    def modify_order(self, order_id: str, new_price: float, new_trigger: float, qty: int, order_type: str = "SL", transaction_type: str = None) -> dict:
        if self.is_paper or str(order_id).startswith("paper_") or str(order_id).startswith("virtual_") or str(order_id).startswith("test_"):
            logger.info(f"PAPER MODE ACTIVE: Modifying order {order_id} to trigger {new_trigger}, price {new_price}. Zero broker network calls made.")
            return {"status": "success", "paper_trade": True}

        # Strict safety check before ANY live broker call
        auth_token = self._get_headers().get("Authorization", "").replace("Bearer ", "").strip()
        if not auth_token:
            logger.error("LIVE EXECUTION BLOCKED: Missing live Upstox access token for modify_order.")
            return {"status": "rejected", "reason": "Missing live Upstox access token"}

        # Calculate limit price with 0.1% / 3-tick (0.15) buffer if SL Limit price is 0 (Fix A)
        if order_type == "SL" and new_price == 0:
            if not transaction_type:
                try:
                    orders = self.get_order_book()
                    for o in orders:
                        if o.get('order_id') == order_id:
                            transaction_type = o.get('transaction_type')
                            break
                except Exception as e:
                    logger.error(f"Failed to lookup order {order_id} for transaction_type: {e}")
            
            if not transaction_type:
                transaction_type = "SELL"  # safety fallback
                
            buffer = max(0.15, new_trigger * 0.001)
            if transaction_type.upper() == "SELL":
                new_price = round((new_trigger - buffer) * 20) / 20.0
            else:
                new_price = round((new_trigger + buffer) * 20) / 20.0

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
            # Use HFT endpoint for order modification
            url = f"{self.api_hft}/order/modify"
            headers = {**self._get_headers(), 'Content-Type': 'application/json'}
            response = requests.put(url, headers=headers, json=payload, timeout=10)
            response_data = response.json()
            
            if response.status_code == 200 and response_data.get('status') == 'success':
                return {"status": "success"}
            else:
                logger.error(f"Failed to modify order {order_id}: {response_data}")
                reason = "Unknown modification failure"
                errors = response_data.get('errors', [])
                if errors and isinstance(errors, list) and len(errors) > 0:
                    reason = errors[0].get('message', 'Rejected')
                
                # Check for circuit limit keywords (e.g. price range, price band, circuit, limit)
                circuit_keywords = ["price range", "price band", "circuit", "daily limit", "trigger price limit", "outside range", "daily price range"]
                is_circuit = any(kw in reason.lower() for kw in circuit_keywords)
                
                if is_circuit:
                    return {"status": "circuit_limit", "reason": reason}
                return {"status": "error", "reason": reason}
        except Exception as e:
            logger.error(f"API Crash modifying {order_id}: {e}")
            return {"status": "error", "reason": str(e)}

    def cancel_order(self, order_id: str) -> dict:
        if self.is_paper or str(order_id).startswith("paper_") or str(order_id).startswith("virtual_") or str(order_id).startswith("test_"):
            logger.info(f"PAPER MODE ACTIVE: Canceling order {order_id}. Zero broker network calls made.")
            return {"status": "success", "paper_trade": True}

        # Strict safety check before ANY live broker call
        auth_token = self._get_headers().get("Authorization", "").replace("Bearer ", "").strip()
        if not auth_token:
            logger.error("LIVE EXECUTION BLOCKED: Missing live Upstox access token for cancel_order.")
            return {"status": "rejected", "reason": "Missing live Upstox access token"}

        try:
            # Use HFT endpoint for order cancellation
            url = f"{self.api_hft}/order/cancel?order_id={order_id}"
            response = requests.delete(url, headers=self._get_headers(), timeout=10)
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
        if self.is_paper:
            logger.info("PAPER MODE ACTIVE: Simulated Exit All Positions. Zero broker network calls made.")
            return {"status": "success", "paper_trade": True}

        # Strict safety check before ANY live broker call
        auth_token = self._get_headers().get("Authorization", "").replace("Bearer ", "").strip()
        if not auth_token:
            logger.error("LIVE EXECUTION BLOCKED: Missing live Upstox access token for exit_all_positions.")
            return {"status": "rejected", "reason": "Missing live Upstox access token"}
            
        try:
            url = f"{self.api_v2}/order/positions/exit"
            headers = {**self._get_headers(), 'Content-Type': 'application/json'}
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

    # ── v2 Pipeline Methods ────────────────────────────────────────────────────

    def get_historical_candles_daily(self, instrument_key: str, days: int = 30) -> pd.DataFrame:
        """Fetch daily historical candles for Layer 1 pre-filter."""
        try:
            to_date = date.today().isoformat()
            from_date = (date.today() - timedelta(days=days)).isoformat()
            url = f"{self.api_v3}/historical-candle/{instrument_key}/days/1/{to_date}/{from_date}"
            response = requests.get(url, headers=self._get_headers(True), timeout=15)
            response.raise_for_status()
            data = response.json()

            if 'data' in data and 'candles' in data['data']:
                candles = data['data']['candles']
                df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'oi'])
                df = df.iloc[::-1].reset_index(drop=True)  # Oldest first
                df['timestamp'] = pd.to_datetime(df['timestamp'])
                for col in ['open', 'high', 'low', 'close', 'volume']:
                    df[col] = df[col].astype(float)
                return df
            else:
                logger.error(f"No daily candle data for {instrument_key}: {data}")
                return pd.DataFrame()
        except Exception as e:
            logger.error(f"Error fetching daily candles for {instrument_key}: {e}")
            return pd.DataFrame()

    def get_trade_book(self) -> list:
        """Fetch today's trade book from Upstox for reconciliation (Section 10)."""
        if self.mode == "TEST":
            return []
        try:
            url = f"{self.api_v2}/order/trades/get-trades-for-day"
            response = requests.get(url, headers=self._get_headers(), timeout=10)
            if response.status_code == 200:
                data = response.json()
                return data.get('data', [])
            else:
                logger.error(f"Failed to fetch trade book: {response.text}")
        except Exception as e:
            logger.error(f"Error fetching trade book: {e}")
        return []

    def get_positions(self) -> list:
        """Fetch active net positions from Upstox (Fix B)."""
        if self.mode == "TEST":
            return []
        try:
            url = f"{self.api_v2}/portfolio/net-positions"
            response = requests.get(url, headers=self._get_headers(), timeout=10)
            if response.status_code == 200:
                data = response.json()
                return data.get('data', [])
            else:
                logger.error(f"Failed to fetch net positions: {response.text}")
        except Exception as e:
            logger.error(f"Error fetching net positions: {e}")
        return []

    def check_intraday_leverage(self, instrument_key: str, price: float, qty: int = 100) -> dict:
        """
        Calls /v2/charges/margin to calculate required margin for MIS (product='I')
        and returns leverage information.
        """
        if self.mode == "TEST":
            # In test mode, assume 5x leverage is supported
            return {"status": "success", "leverage": 5.0, "intraday_supported": True}

        url = f"{self.api_v2}/charges/margin"
        payload = {
            "instruments": [
                {
                    "instrument_key": instrument_key,
                    "quantity": qty,
                    "transaction_type": "BUY",
                    "product": "I",
                    "price": price
                }
            ]
        }
        try:
            headers = {**self._get_headers(), 'Content-Type': 'application/json', 'Accept': 'application/json'}
            response = requests.post(url, headers=headers, json=payload, timeout=10)
            if response.status_code == 200:
                data = response.json()
                if data.get('status') == 'success':
                    # Parse final_margin
                    final_margin = data['data'].get('final_margin', 0.0)
                    total_value = qty * price
                    if final_margin > 0:
                        leverage = total_value / final_margin
                        return {
                            "status": "success",
                            "leverage": leverage,
                            "intraday_supported": True,
                            "required_margin": final_margin
                        }
            
            # If not successful or non-200 status, parse and return the error message
            reason = "Unknown rejection/API error"
            if response.text:
                try:
                    data = response.json()
                    errors = data.get('errors', [])
                    if errors:
                        reason = errors[0].get('message', 'Rejected')
                    else:
                        reason = response.text
                except Exception:
                    reason = response.text
            else:
                reason = f"HTTP Error {response.status_code}"
                    
            logger.warning(f"Margin check failed for {instrument_key}: {reason}")
            return {
                "status": "failed",
                "leverage": 0.0,
                "intraday_supported": False,
                "reason": reason
            }
        except Exception as e:
            logger.error(f"Error checking margin/leverage for {instrument_key}: {e}")
            return {
                "status": "failed",
                "leverage": 0.0,
                "intraday_supported": False,
                "reason": str(e)
            }


class PaperExecutionEngine(UpstoxExecutionEngine):
    """
    Dedicated isolated Paper Execution Engine.
    Guarantees zero network calls to broker order endpoints.
    Tracks simulated virtual fills, order statuses, and virtual positions locally.
    """
    def __init__(self, credentials: dict = None, initial_balance: float = None):
        super().__init__(credentials=credentials, mode="TEST")
        self.mode = "TEST"
        self._initial_balance = initial_balance or getattr(Config, 'MANUAL_CAPITAL', 25000.0)
        self._virtual_balance = self._initial_balance
        self._virtual_orders = []
        self._virtual_positions = {}  # symbol -> {qty, entry_price, side}

    @property
    def is_paper(self) -> bool:
        return True

    def get_funds(self, force_live: bool = False) -> float:
        return float(self._virtual_balance)

    def place_order_with_retry(self, symbol: str, side: str, qty: int, order_type: str = 'MARKET', price: float = 0.0, max_retries: int = 1) -> dict:
        import time as _t
        order_id = f"paper_ord_{symbol}_{side}_{int(_t.time()*1000)}"
        avg_price = float(price or self.get_ltp(symbol) or 100.0)
        record = {
            "order_id": order_id,
            "symbol": symbol,
            "side": side.upper(),
            "quantity": int(qty),
            "price": avg_price,
            "order_type": order_type,
            "status": "complete",
            "filled_quantity": int(qty),
            "average_price": avg_price,
            "timestamp": _t.time(),
            "paper_trade": True
        }
        self._virtual_orders.append(record)
        logger.info(f"[PaperExecutionEngine] Placed virtual order {order_id}: {side} {qty} {symbol} @ {avg_price:.2f}")
        return {
            "status": "success",
            "order_id": order_id,
            "filled_quantity": int(qty),
            "average_price": avg_price,
            "paper_trade": True
        }

    def place_stop_loss(self, symbol: str, side: str, qty: int, sl_price: float) -> dict:
        import time as _t
        order_id = f"paper_sl_{symbol}_{side}_{int(_t.time()*1000)}"
        logger.info(f"[PaperExecutionEngine] Placed virtual SL order {order_id}: {symbol} {side} SL={sl_price:.2f}")
        return {
            "status": "success",
            "order_id": order_id,
            "paper_trade": True
        }

    def modify_order(self, order_id: str, new_price: float, new_trigger: float, qty: int, order_type: str = "SL", transaction_type: str = None) -> dict:
        logger.info(f"[PaperExecutionEngine] Modified virtual order {order_id}: trigger={new_trigger:.2f}")
        return {"status": "success", "paper_trade": True}

    def cancel_order(self, order_id: str) -> dict:
        logger.info(f"[PaperExecutionEngine] Canceled virtual order {order_id}")
        return {"status": "success", "paper_trade": True}

    def exit_all_positions(self) -> dict:
        logger.info("[PaperExecutionEngine] Virtual exit all positions called.")
        self._virtual_positions.clear()
        return {"status": "success", "paper_trade": True}

    def get_order_book(self) -> list:
        return list(self._virtual_orders)

    def get_order_status(self, order_id: str) -> dict:
        for ord_info in self._virtual_orders:
            if ord_info.get("order_id") == order_id:
                return {
                    "status": "success",
                    "order_status": "complete",
                    "filled_quantity": ord_info["filled_quantity"],
                    "quantity": ord_info["quantity"],
                    "average_price": ord_info["average_price"],
                    "paper_trade": True
                }
        return {
            "status": "success",
            "order_status": "complete",
            "filled_quantity": 0,
            "paper_trade": True
        }



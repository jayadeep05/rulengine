"""
market_feed.py — Robust Upstox Streamer
Fixed 403 Forbidden by:
1. Monkey-patching handle_error and handle_close to be silent and non-recursive.
2. Ensuring only one authorize call per connection attempt.
3. Adding User-Agent to comply with modern API gateways.
"""

import logging
import threading
import time as _time
import json
import requests
from typing import Dict

import upstox_client
from upstox_client.feeder.market_data_streamer_v3 import MarketDataStreamerV3
from upstox_client.feeder.portfolio_data_streamer import PortfolioDataStreamer

from config import Config

logger = logging.getLogger(__name__)

# ─── Configuration ────────────────────────────────────────────────────────────
EQUITY_KEYS = list(Config.SYMBOLS_MAPPING.values())
# Comprehensive index keys from Config
_raw_indices = getattr(Config, 'ALL_INDEX_KEYS', [
    "NSE_INDEX|Nifty 50", "BSE_INDEX|SENSEX", "NSE_INDEX|Nifty Bank",
    "NSE_INDEX|Nifty Fin Service", "NSE_INDEX|NIFTY MID SELECT", "NSE_INDEX|India VIX"
])
INDEX_KEYS = [k.replace("|", ":") for k in _raw_indices] + [
    "NSE_INDEX:NIFTY FIN SERVICE", "NSE_INDEX:NIFTY BANK", "NSE_INDEX:NIFTY MIDCAP 50", "NSE_INDEX:NIFTY 50"
]
# Subscriptions for indices: Use BOTH formats for maximum compatibility
ALL_INSTRUMENT_KEYS = EQUITY_KEYS + list(set(INDEX_KEYS + [k.replace(":", "|") for k in INDEX_KEYS]))
# Add pipe versions for matching
INDEX_SET = set(INDEX_KEYS) | {k.replace(":", "|") for k in INDEX_KEYS}

_REVERSE_MAP: Dict[str, str] = {v: k for k, v in Config.SYMBOLS_MAPPING.items()}
_REVERSE_MAP.update({v.replace("|", ":"): k for k, v in Config.SYMBOLS_MAPPING.items()})

_prices_cache: Dict[str, float] = {}
_indices_cache: Dict[str, float] = {}
_indices_prev_close: Dict[str, float] = {}
_tick_timestamps: Dict[str, float] = {}
_last_equity_tick: float = 0
_last_index_tick: float = 0
_trade_manager = None
_shared_creds = None

_stop_event = threading.Event()

_last_auth_error_log = {}

def _get_authorized_url(api_type: str = "market"):
    """Fetches a fresh authorized URL."""
    token = None
    if _shared_creds:
        token = _shared_creds.get("analytics_token") if (api_type == "market" and _shared_creds.get("analytics_token")) else _shared_creds.get("upstox_access_token")
    if not token:
        token = Config.ANALYTICS_TOKEN if (api_type == "market" and Config.ANALYTICS_TOKEN) else Config.UPSTOX_ACCESS_TOKEN
    
    url = f"https://api.upstox.com/{'v3' if api_type == 'market' else 'v2'}/feed/{'market-data-feed' if api_type == 'market' else 'portfolio-stream-feed'}/authorize"
        
    headers = {
        'Accept': 'application/json',
        'Authorization': f'Bearer {token}'
    }
    
    try:
        response = requests.get(url, headers=headers, params={'client_id': Config.UPSTOX_CLIENT_ID}, timeout=15)
        if response.status_code == 200:
            data = response.json()
            uri = data.get('data', {}).get('authorizedRedirectUri') or data.get('data', {}).get('authorized_redirect_uri')
            if uri:
                return uri
        now_ts = _time.time()
        last_log = _last_auth_error_log.get(api_type, 0)
        if (now_ts - last_log) > 600:
            logger.error(f"[Auth] {api_type} FAILED ({response.status_code}): {response.text}")
            _last_auth_error_log[api_type] = now_ts
    except Exception as e:
        logger.error(f"[Auth] {api_type} EXCEPTION: {e}")
    return None

# ─── SDK Monkey Patches ───────────────────────────────────────────────────────

class _DummyPool:
    def close(self): pass
    def join(self): pass
    def terminate(self): pass
    def apply_async(self, fn, args=(), kwds=(), callback=None, error_callback=None): pass

def _patch_sdk():
    """Patches both Market and Portfolio feeders and streamers to use manual auth, prevent recursion and thread leaks."""
    import upstox_client.api_client
    upstox_client.api_client.ThreadPool = _DummyPool

    from upstox_client.feeder.market_data_feeder_v3 import MarketDataFeederV3
    from upstox_client.feeder.portfolio_data_feeder import PortfolioDataFeeder
    from upstox_client.feeder.market_data_streamer_v3 import MarketDataStreamerV3
    from upstox_client.feeder.portfolio_data_streamer import PortfolioDataStreamer
    import websocket
    import ssl

    # 1. Market Feeder Patch
    def market_connect_patched(self):
        if self.ws and self.ws.sock: return
        ws_url = _get_authorized_url("market")
        if not ws_url:
            self.on_error(self.ws, "Auth URL fetch failed")
            return
        headers = {"User-Agent": "python-upstox-v3-stable/1.2"} 
        sslopt = {"cert_reqs": ssl.CERT_NONE, "check_hostname": False}
        self.ws = websocket.WebSocketApp(
            ws_url, header=headers,
            on_open=self.on_open, on_message=self.on_message,
            on_error=self.on_error, on_close=self.on_close
        )
        threading.Thread(target=self.ws.run_forever, kwargs={"sslopt": sslopt}, daemon=True).start()

    MarketDataFeederV3.connect = market_connect_patched

    # 2. Portfolio Feeder Patch
    def port_connect_patched(self):
        if self.ws and self.ws.sock: return
        ws_url = _get_authorized_url("portfolio")
        if not ws_url:
            self.on_error(self.ws, "Auth URL fetch failed")
            return
        token = self.api_client.configuration.auth_settings().get("OAUTH2")["value"]
        headers = {'Authorization': token, "User-Agent": "python-upstox-v2-port/1.2"}
        sslopt = {"cert_reqs": ssl.CERT_NONE, "check_hostname": False}
        self.ws = websocket.WebSocketApp(
            ws_url, header=headers,
            on_open=self.on_open, on_message=self.on_message,
            on_error=self.on_error, on_close=self.on_close
        )
        threading.Thread(target=self.ws.run_forever, kwargs={"sslopt": sslopt}, daemon=True).start()

    PortfolioDataFeeder.connect = port_connect_patched

    # 3. Streamer Error/Close override (Prevent SDK recursion)
    def handle_error_silent(self, ws, error):
        self.emit(self.Event["ERROR"], error)
    
    def handle_close_silent(self, ws, code, msg):
        self.emit(self.Event["CLOSE"], code, msg)

    MarketDataStreamerV3.handle_error = handle_error_silent
    MarketDataStreamerV3.handle_close = handle_close_silent
    PortfolioDataStreamer.handle_error = handle_error_silent
    PortfolioDataStreamer.handle_close = handle_close_silent

# ─── Message Parsing ──────────────────────────────────────────────────────────

def _on_market_message(message):
    global _got_first_tick
    global _last_tick_time
    try:
        feeds = message.get('feeds', {})
        for instrument_key, feed in feeds.items():
            msg = None
            if 'ltpc' in feed:
                msg = feed['ltpc']
            elif 'fullFeed' in feed:
                market = feed['fullFeed'].get('marketFF')
                index = feed['fullFeed'].get('indexFF')
                msg = (market or index or {}).get('ltpc', {})
            
            if msg:
                # Extract ltp and previous close
                ltp = float(msg.get('ltp', 0))
                # Upstox V3 often uses 'cp' for previous close in index feeds, but let's check all
                prev_close = float(msg.get('cp') or msg.get('pc') or msg.get('pclose') or 0)
                
                # Check if it's an index (case-insensitive and flexible format)
                is_index = False
                upper_key = instrument_key.upper().replace(":", "|")
                
                # Check against predefined set
                for ik in INDEX_SET:
                    if ik.upper().replace(":", "|") == upper_key:
                        is_index = True
                        break
                
                # Broad wildcard matching for any index or VIX
                if not is_index:
                    is_index = "_INDEX" in upper_key or "NIFTY" in upper_key or "SENSEX" in upper_key or "VIX" in upper_key
                
                if is_index:
                    # Normalize to pipe format for UI consistency
                    normalized_key = instrument_key.replace(":", "|")
                    # Special handling for NIFTY 50 capitalization mismatch
                    if "NIFTY 50" in upper_key: normalized_key = "NSE_INDEX|Nifty 50"
                    if "SENSEX" in upper_key and "BSE" in upper_key:
                        if not any(sub in upper_key for sub in ("50", "60", "NEXT", "EQUAL", "SNSX", "SS6535", "SENEQW")):
                            normalized_key = "BSE_INDEX|SENSEX"
                    if "NIFTY BANK" in upper_key: normalized_key = "NSE_INDEX|Nifty Bank"

                    _indices_cache[normalized_key] = ltp
                    _tick_timestamps[normalized_key] = _time.time()
                    if prev_close > 0:
                        _indices_prev_close[normalized_key] = prev_close
                    
                    global _last_index_tick
                    _last_index_tick = _time.time()
                else:
                    # It's an equity
                    human_name = _REVERSE_MAP.get(instrument_key)
                    if not human_name:
                        # Try with pipe/colon swap
                        alt_key = instrument_key.replace(":", "|") if ":" in instrument_key else instrument_key.replace("|", ":")
                        human_name = _REVERSE_MAP.get(alt_key)
                        
                    if human_name:
                        _prices_cache[human_name] = ltp
                        _tick_timestamps[human_name] = _time.time()
                    
                    global _last_equity_tick
                    _last_equity_tick = _time.time()
                
                _last_tick_time = _time.time()
                
                # Update Trade Manager with new prices
                if _trade_manager:
                    _trade_manager.update_prices(_prices_cache)
    except Exception as exc:
        logger.debug(f"[WS-Market] Parse Error: {exc}")

# ─── Main Loops ───────────────────────────────────────────────────────────────

def _market_loop():
    backoff = 5
    while not _stop_event.is_set():
        try:
            api_client = upstox_client.ApiClient()
            config = upstox_client.Configuration()
            token = None
            if _shared_creds:
                token = _shared_creds.get("analytics_token") or _shared_creds.get("upstox_access_token")
            if not token:
                token = Config.ANALYTICS_TOKEN if Config.ANALYTICS_TOKEN else Config.UPSTOX_ACCESS_TOKEN
            if not token:
                _time.sleep(30)
                continue
            config.access_token = token
            api_client.configuration = config
            
            streamer = MarketDataStreamerV3(api_client, instrumentKeys=ALL_INSTRUMENT_KEYS, mode="ltpc")
            logger.info(f"[WS-Market] Subscribing to {len(ALL_INSTRUMENT_KEYS)} keys (including {len(INDEX_KEYS)} indices)")
            
            closed_event = threading.Event()
            _captured = closed_event
            
            E = MarketDataStreamerV3.Event
            streamer.on(E['OPEN'],    lambda *a: logger.info("[WS-Market] ✅ Connected Successfully"))
            streamer.on(E['MESSAGE'], _on_market_message)
            streamer.on(E['ERROR'],   lambda e: (logger.error(f"[WS-Market] ❌ Error: {e}"), _captured.set()))
            streamer.on(E['CLOSE'],   lambda *a: (logger.warning("[WS-Market] ⚠️ Connection closed."), _captured.set()))
            
            streamer.auto_reconnect(enable=False)
            streamer.connect()
            
            # Wait for death of websocket
            _captured.wait()
            
            if not _stop_event.is_set():
                _time.sleep(backoff)
                backoff = min(backoff + 15, 60)
        except Exception as exc:
            logger.error(f"[WS-Market] Loop Error: {exc}")
            _time.sleep(15)

def _portfolio_loop():
    backoff = 30
    while not _stop_event.is_set():
        try:
            token = _shared_creds.get("upstox_access_token") if _shared_creds else None
            if not token:
                token = Config.UPSTOX_ACCESS_TOKEN
            if not token:
                _time.sleep(30)
                continue

            api_client = upstox_client.ApiClient()
            config = upstox_client.Configuration()
            config.access_token = token
            api_client.configuration = config
            
            streamer = PortfolioDataStreamer(api_client, order_update=True, position_update=True)
            closed_event = threading.Event()
            _cap = closed_event
            
            streamer.on("open",    lambda *a: logger.info("[WS-Portfolio] ✅ Connected Successfully"))
            streamer.on("error",   lambda e: (logger.error(f"[WS-Portfolio] ❌ Error: {e}"), _cap.set()))
            streamer.on("close",   lambda *a: (logger.warning("[WS-Portfolio] ⚠️ Connection closed."), _cap.set()))
            
            streamer.auto_reconnect(enable=False)
            streamer.connect()
            _cap.wait()
            backoff = 30
        except Exception:
            pass
        _time.sleep(backoff)
        backoff = min(backoff * 2, 300)

# ─── Public API ───────────────────────────────────────────────────────────────

def update_credentials(credentials: dict):
    global _shared_creds
    _shared_creds = credentials

def start(prices_cache: dict, indices_cache: dict, prev_close_cache: dict = None, trade_manager=None, timestamps_cache: dict = None, credentials: dict = None):
    global _prices_cache, _indices_cache, _indices_prev_close, _trade_manager, _stop_event, _tick_timestamps, _shared_creds
    _prices_cache = prices_cache
    _indices_cache = indices_cache
    if prev_close_cache is not None:
        _indices_prev_close = prev_close_cache
    _trade_manager = trade_manager
    if timestamps_cache is not None:
        _tick_timestamps = timestamps_cache
    else:
        _tick_timestamps = {}
    _shared_creds = credentials
    _stop_event.clear()

    _patch_sdk()

    threading.Thread(target=_market_loop, daemon=True, name="MktWS").start()
    
    has_access_token = False
    if credentials and credentials.get("upstox_access_token"):
        has_access_token = True
    elif Config.UPSTOX_ACCESS_TOKEN:
        has_access_token = True

    if has_access_token:
        threading.Thread(target=_portfolio_loop, daemon=True, name="PortWS").start()
    logger.info("[WS] Dedicated Market & Portfolio threads launched.")

def stop():
    _stop_event.set()

def is_live():
    # Overall live if either is ticking
    return is_equity_live() or is_index_live()

def is_equity_live():
    return (_time.time() - _last_equity_tick) < 30

def is_index_live():
    return (_time.time() - _last_index_tick) < 5

# ─── Bug Fix 4: Stale Feed Helpers ───────────────────────────────────────────

def is_tick_fresh(symbol: str, max_age: float = 30.0) -> bool:
    """Check if the last tick for a symbol is within the allowed freshness window.
    Returns True if tick is fresh (within max_age seconds), False otherwise.
    Used by Layer 3, Layer 4, order placement, and trade monitoring."""
    last_tick = _tick_timestamps.get(symbol, 0.0)
    if last_tick == 0.0:
        return False
    return (_time.time() - last_tick) < max_age

def get_tick_age(symbol: str) -> float:
    """Return age of last tick in seconds. Returns -1 if no tick ever received."""
    last_tick = _tick_timestamps.get(symbol, 0.0)
    if last_tick == 0.0:
        return -1.0
    return _time.time() - last_tick

def get_feed_health_summary() -> dict:
    """Return feed health status for dashboard indicator.
    Green: all symbols fresh (<15s), Yellow: some 15-30s, Red: majority >30s or disconnected."""
    if not _tick_timestamps:
        return {"status": "red", "fresh": 0, "stale": 0, "dead": 0, "total": 0}
    
    now = _time.time()
    fresh = stale = dead = 0
    for sym, ts in _tick_timestamps.items():
        age = now - ts
        if age < 15:
            fresh += 1
        elif age < 30:
            stale += 1
        else:
            dead += 1
    
    total = fresh + stale + dead
    if dead > total * 0.5 or not is_live():
        status = "red"
    elif stale > 0 or dead > 0:
        status = "yellow"
    else:
        status = "green"
    
    return {"status": status, "fresh": fresh, "stale": stale, "dead": dead, "total": total}

def get_all_tick_timestamps() -> dict:
    """Return a copy of all tick timestamps for external use."""
    return dict(_tick_timestamps)


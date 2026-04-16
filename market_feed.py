"""
market_feed.py — Upstox WebSocket Market Data Streamer
Uses MarketDataStreamerV3 from the official upstox-python-sdk.
Streams real-time LTP for all configured symbols + indices into shared dicts.

Architecture:
  - Runs the WS client in a background daemon thread (non-blocking for asyncio).
  - On every tick, updates the shared live_prices_cache and live_indices passed in via init.
  - Uses the SDK's built-in auto_reconnect to handle drops automatically.
"""

import logging
import threading
import time as _time
from typing import Dict

import upstox_client
from upstox_client.feeder.market_data_streamer_v3 import MarketDataStreamerV3

from config import Config

logger = logging.getLogger(__name__)

# ─── Instruments to subscribe ─────────────────────────────────────────────────
EQUITY_KEYS = list(Config.SYMBOLS_MAPPING.values())

INDEX_KEYS = [
    "NSE_INDEX|Nifty 50",
    "BSE_INDEX|SENSEX",
    "NSE_INDEX|Nifty Bank",
    "NSE_INDEX|Nifty Fin Service",
    "NSE_INDEX|NIFTY MID SELECT",
    "NSE_INDEX|India VIX",
]

ALL_INSTRUMENT_KEYS = EQUITY_KEYS + INDEX_KEYS

# Reverse map: Upstox instrument_key -> human name  (equities only)
_REVERSE_MAP: Dict[str, str] = {v: k for k, v in Config.SYMBOLS_MAPPING.items()}

# ─── Shared state (references updated at start()) ────────────────────────────
_prices_cache: Dict[str, float] = {}
_indices_cache: Dict[str, float] = {}
_got_first_tick: bool = False


def _make_api_client() -> upstox_client.ApiClient:
    """Create an authenticated Upstox API client."""
    configuration = upstox_client.Configuration()
    configuration.access_token = Config.UPSTOX_ACCESS_TOKEN
    return upstox_client.ApiClient(configuration)


def _on_message(message):
    """
    Callback fired for every streamed tick from the protobuf feed.
    The SDK decodes protobuf and gives us a MarketFullFeed object.
    """
    global _got_first_tick
    try:
        feeds = message.feeds
        logger.debug(f"[WS] Received message with {len(feeds)} feeds.")
        for instrument_key, feed in feeds.items():
            ltp = None

            # Path 1: equity / full market feed
            try:
                ltp = feed.ff.marketFF.ltpc.ltp
            except Exception:
                pass

            # Path 2: index feed
            if not ltp:
                try:
                    ltp = feed.ff.indexFF.ltpc.ltp
                except Exception:
                    pass

            if not ltp:
                continue

            ltp = float(ltp)

            # Normalise key separator (SDK may use ":" instead of "|")
            normalised_key = instrument_key.replace(":", "|")

            # Route to indices or equities
            if normalised_key in INDEX_KEYS or instrument_key in INDEX_KEYS:
                _indices_cache[normalised_key] = ltp
                _indices_cache[instrument_key] = ltp  # keep both variants
            else:
                human_name = (
                    _REVERSE_MAP.get(normalised_key)
                    or _REVERSE_MAP.get(instrument_key)
                )
                if human_name:
                    _prices_cache[human_name] = ltp

            _got_first_tick = True

    except Exception as exc:
        logger.debug(f"[WS] tick parse error: {exc}")


def _on_open(*args):
    logger.info(f"[WS] ✅ Upstox Market Data WebSocket CONNECTED. (args: {args})")


def _on_close(*args):
    logger.warning(f"[WS] ⚠️  Upstox Market Data WebSocket DISCONNECTED. (args: {args})")


def _on_error(*args):
    error = args[0] if args else "Unknown error"
    logger.error(f"[WS] ❌ Upstox Market Data WebSocket ERROR: {error}")
    if "403" in str(error):
        logger.error("[WS] 💡 403 Forbidden detected. Possible causes: Expired token, IP whitelisting required, or account restricted for WebSocket.")


def _on_reconnecting(*args):
    logger.info(f"[WS] 🔄 Upstox Market Data WebSocket AUTO-RECONNECTING… (args: {args})")


def _streamer_loop():
    """
    Runs the WebSocket streamer in a background thread.
    Uses the SDK's built-in auto_reconnect; only restarts the whole object
    if auto_reconnect gives up.
    """
    backoff = 10
    while True:
        streamer = None
        try:
            api_client = _make_api_client()
            streamer = MarketDataStreamerV3(
                api_client,
                instrumentKeys=ALL_INSTRUMENT_KEYS,
                mode="ltpc",
            )

            # Wire event callbacks using SDK Event dict string values
            E = MarketDataStreamerV3.Event  # {'OPEN': 'open', 'MESSAGE': 'message', ...}
            streamer.on(E['OPEN'],         _on_open)
            streamer.on(E['CLOSE'],        _on_close)
            streamer.on(E['ERROR'],        _on_error)
            streamer.on(E['MESSAGE'],      _on_message)
            streamer.on(E['RECONNECTING'], _on_reconnecting)

            # Enable SDK-level auto-reconnect (every 5 s, max 20 attempts)
            streamer.auto_reconnect(enable=True, interval=5, retry_count=20)

            logger.info(
                f"[WS] Connecting to Upstox Market Feed — "
                f"{len(EQUITY_KEYS)} equities + {len(INDEX_KEYS)} indices "
                f"({len(ALL_INSTRUMENT_KEYS)} total)"
            )

            streamer.connect()  # Blocking — returns when WS closes/auto-reconnect exhausted
            backoff = 10        # Reset after a full clean connect cycle

        except Exception as exc:
            logger.error(f"[WS] Streamer crashed: {exc}. Hard restart in {backoff}s…")
        finally:
            try:
                if streamer:
                    streamer.disconnect()
            except Exception:
                pass

        _time.sleep(backoff)
        backoff = min(backoff * 2, 120)  # exponential back-off, cap at 2 min


# ─── Public API ───────────────────────────────────────────────────────────────

def start(prices_cache: dict, indices_cache: dict):
    """
    Start the WebSocket streamer as a background daemon thread.

    Args:
        prices_cache:  The live_prices_cache dict from main.py (updated in-place).
        indices_cache: The state.live_indices dict from main.py (updated in-place).
    """
    global _prices_cache, _indices_cache
    _prices_cache = prices_cache
    _indices_cache = indices_cache

    t = threading.Thread(target=_streamer_loop, daemon=True, name="upstox-ws-feed")
    t.start()
    logger.info("[WS] Market Data Streamer background thread started.")


def is_live() -> bool:
    """Return True if the WebSocket has delivered at least one tick."""
    return _got_first_tick

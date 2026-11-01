"""
global_indices_service.py
-------------------------
Service for fetching real-time/latest quotes for major international and global indices,
commodities, and macro indicators. Runs in a dedicated background worker thread with caching.
"""

import logging
import threading
import time
import urllib.request
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Tuple, List, Optional

logger = logging.getLogger(__name__)

GLOBAL_INDICES_CATALOG: List[Dict[str, str]] = [
    # Indian Benchmark & Major Indices (Live Sync & Universal Fallback)
    {"name": "NIFTY 50", "key": "NSE_INDEX|Nifty 50", "symbol": "^NSEI", "category": "Benchmark"},
    {"name": "SENSEX", "key": "BSE_INDEX|SENSEX", "symbol": "^BSESN", "category": "Benchmark"},
    {"name": "NIFTY BANK", "key": "NSE_INDEX|Nifty Bank", "symbol": "^NSEBANK", "category": "Sectoral"},
    {"name": "INDIA VIX", "key": "NSE_INDEX|India VIX", "symbol": "^INDIAVIX", "category": "Volatility"},
    {"name": "FINNIFTY", "key": "NSE_INDEX|Nifty Fin Service", "symbol": "NIFTY_FIN_SERVICE.NS", "category": "Sectoral"},
    {"name": "MIDCPNIFTY", "key": "NSE_INDEX|NIFTY MID SELECT", "symbol": "NIFTY_MID_SELECT.NS", "category": "Broad Market"},
    {"name": "NIFTY NEXT 50", "key": "NSE_INDEX|Nifty Next 50", "symbol": "^NSMIDCP", "category": "Broad Market"},
    {"name": "NIFTY 100", "key": "NSE_INDEX|Nifty 100", "symbol": "^CNX100", "category": "Broad Market"},
    {"name": "NIFTY 500", "key": "NSE_INDEX|Nifty 500", "symbol": "^CRSLDX", "category": "Broad Market"},
    {"name": "NIFTY IT", "key": "NSE_INDEX|Nifty IT", "symbol": "^CNXIT", "category": "Sectoral"},
    {"name": "NIFTY AUTO", "key": "NSE_INDEX|Nifty Auto", "symbol": "^CNXAUTO", "category": "Sectoral"},
    {"name": "NIFTY PHARMA", "key": "NSE_INDEX|Nifty Pharma", "symbol": "^CNXPHARMA", "category": "Sectoral"},
    {"name": "NIFTY FMCG", "key": "NSE_INDEX|Nifty FMCG", "symbol": "^CNXFMCG", "category": "Sectoral"},
    {"name": "NIFTY METAL", "key": "NSE_INDEX|Nifty Metal", "symbol": "^CNXMETAL", "category": "Sectoral"},
    {"name": "NIFTY ENERGY", "key": "NSE_INDEX|Nifty Energy", "symbol": "^CNXENERGY", "category": "Sectoral"},
    {"name": "NIFTY REALTY", "key": "NSE_INDEX|Nifty Realty", "symbol": "^CNXREALTY", "category": "Sectoral"},
    {"name": "NIFTY INFRA", "key": "NSE_INDEX|Nifty Infra", "symbol": "^CNXINFRA", "category": "Sectoral"},
    {"name": "BANKEX", "key": "BSE_INDEX|BANKEX", "symbol": "BSE-BANK.BO", "category": "Sectoral"},
    {"name": "NIFTY PSU BANK", "key": "NSE_INDEX|Nifty PSU Bank", "symbol": "^CNXPSUBANK", "category": "Sectoral"},

    # US Markets
    {"name": "S&P 500", "key": "GLOBAL_INDEX|^GSPC", "symbol": "^GSPC", "category": "US Markets"},
    {"name": "NASDAQ", "key": "GLOBAL_INDEX|^IXIC", "symbol": "^IXIC", "category": "US Markets"},
    {"name": "DOW JONES", "key": "GLOBAL_INDEX|^DJI", "symbol": "^DJI", "category": "US Markets"},
    {"name": "CBOE VIX", "key": "GLOBAL_INDEX|^VIX", "symbol": "^VIX", "category": "US Markets"},

    # European Markets
    {"name": "FTSE 100", "key": "GLOBAL_INDEX|^FTSE", "symbol": "^FTSE", "category": "European Markets"},
    {"name": "DAX", "key": "GLOBAL_INDEX|^GDAXI", "symbol": "^GDAXI", "category": "European Markets"},
    {"name": "CAC 40", "key": "GLOBAL_INDEX|^FCHI", "symbol": "^FCHI", "category": "European Markets"},

    # Asian & Pacific Markets
    {"name": "NIKKEI 225", "key": "GLOBAL_INDEX|^N225", "symbol": "^N225", "category": "Asian Markets"},
    {"name": "HANG SENG", "key": "GLOBAL_INDEX|^HSI", "symbol": "^HSI", "category": "Asian Markets"},
    {"name": "SHANGHAI", "key": "GLOBAL_INDEX|000001.SS", "symbol": "000001.SS", "category": "Asian Markets"},
    {"name": "KOSPI", "key": "GLOBAL_INDEX|^KS11", "symbol": "^KS11", "category": "Asian Markets"},
    {"name": "TAIWAN TAIEX", "key": "GLOBAL_INDEX|^TWII", "symbol": "^TWII", "category": "Asian Markets"},

    # Singapore Markets
    {"name": "STI (SINGAPORE)", "key": "GLOBAL_INDEX|^STI", "symbol": "^STI", "category": "Singapore Markets"},
    {"name": "SGX NIFTY / GIFT NIFTY", "key": "GLOBAL_INDEX|SGX_NIFTY", "symbol": "^NSEI", "category": "Singapore Markets"},
    {"name": "SGX (SINGAPORE)", "key": "GLOBAL_INDEX|S68.SI", "symbol": "S68.SI", "category": "Singapore Markets"},
    {"name": "FTSE SINGAPORE", "key": "GLOBAL_INDEX|WISGP.SI", "symbol": "WISGP.SI", "category": "Singapore Markets"},
    {"name": "SPDR STI ETF", "key": "GLOBAL_INDEX|ES3.SI", "symbol": "ES3.SI", "category": "Singapore Markets"},

    # Global Macro & Commodities
    {"name": "DOLLAR INDEX", "key": "GLOBAL_INDEX|DX-Y.NYB", "symbol": "DX-Y.NYB", "category": "Global Macro"},
    {"name": "BRENT CRUDE", "key": "GLOBAL_INDEX|BZ=F", "symbol": "BZ=F", "category": "Global Macro"},
    {"name": "WTI CRUDE", "key": "GLOBAL_INDEX|CL=F", "symbol": "CL=F", "category": "Global Macro"},
    {"name": "GOLD (USD)", "key": "GLOBAL_INDEX|GC=F", "symbol": "GC=F", "category": "Global Macro"},
    {"name": "US 10Y BOND", "key": "GLOBAL_INDEX|^TNX", "symbol": "^TNX", "category": "Global Macro"},
]

_lock = threading.Lock()
_global_prices: Dict[str, float] = {}
_global_prev_close: Dict[str, float] = {}
_is_polling = False
_thread: Optional[threading.Thread] = None


def _fetch_single_symbol(item: Dict[str, str]) -> Tuple[str, str, Optional[float], Optional[float]]:
    sym = item["symbol"]
    key = item["key"]
    name = item.get("name", "")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=1d"
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            meta = data["chart"]["result"][0]["meta"]
            price = meta.get("regularMarketPrice")
            prev = meta.get("chartPreviousClose") or meta.get("previousClose")
            if price is not None:
                return key, name, float(price), float(prev) if prev else 0.0
    except Exception as e:
        logger.debug(f"[GlobalIndices] Error fetching {sym}: {e}")
    return key, name, None, None


def refresh_global_indices() -> None:
    """Fetches all international and benchmark indices concurrently and updates cache."""
    global _global_prices, _global_prev_close
    new_prices: Dict[str, float] = {}
    new_prev: Dict[str, float] = {}

    try:
        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(_fetch_single_symbol, GLOBAL_INDICES_CATALOG))

        for key, name, price, prev in results:
            if price is not None:
                p = float(price)
                pr = float(prev or 0.0)
                # Store primary key
                new_prices[key] = p
                new_prev[key] = pr

                # Store colon / pipe alternate keys
                alt_key = key.replace("|", ":") if "|" in key else key.replace(":", "|")
                new_prices[alt_key] = p
                new_prev[alt_key] = pr

                # Store friendly name alias
                if name:
                    new_prices[name] = p
                    new_prev[name] = pr

        if new_prices:
            with _lock:
                _global_prices.update(new_prices)
                _global_prev_close.update(new_prev)
            logger.info(f"[GlobalIndices] Refreshed {len(new_prices)} index quote entries (including Indian benchmarks).")
    except Exception as e:
        logger.error(f"[GlobalIndices] refresh failed: {e}")


def _poller_worker():
    # Initial immediate refresh
    refresh_global_indices()
    while _is_polling:
        time.sleep(25)  # Poll every 25 seconds
        if not _is_polling:
            break
        refresh_global_indices()


def start_global_indices_poller() -> None:
    """Starts the background worker thread if not already running."""
    global _is_polling, _thread
    if _is_polling:
        return
    _is_polling = True
    _thread = threading.Thread(target=_poller_worker, daemon=True, name="GlobalIndicesPoller")
    _thread.start()
    logger.info("[GlobalIndices] Background poller started.")


def get_global_indices_data() -> Tuple[Dict[str, float], Dict[str, float]]:
    """Returns a snapshot copy of (prices, prev_close)."""
    with _lock:
        return dict(_global_prices), dict(_global_prev_close)

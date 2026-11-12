"""
nifty500_instruments.py — Full Nifty 500 Universe & Sector Mapping
──────────────────────────────────────────────────────────────────
Dynamically loads and manages the full Nifty 500 trading universe (500 stocks)
mapped with official Upstox instrument tokens (NSE_EQ|ISIN) and NSE industry sectors.

Auto-refreshes against official NSE India index constituents and the local
Upstox instrument dump (nse_instruments.json.gz) with JSON disk caching.
"""

import os
import io
import csv
import json
import gzip
import time
import logging
import urllib.request
from typing import Dict, List, Optional
from config import Config

logger = logging.getLogger(__name__)

CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nifty500_cache.json")
UPSTOX_DUMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nse_instruments.json.gz")
NSE_NIFTY500_URL = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"

# Global in-memory caches
_UNIVERSE_MAP: Dict[str, str] = {}
_SECTOR_MAP: Dict[str, str] = {}
_INITIALIZED: bool = False


def _build_nifty500_universe() -> bool:
    """Builds and caches the full Nifty 500 universe from NSE and Upstox dump."""
    global _UNIVERSE_MAP, _SECTOR_MAP
    try:
        # 1. Load Upstox instruments dump (trading_symbol -> instrument_key)
        nse_eq = {}
        if os.path.exists(UPSTOX_DUMP):
            with gzip.open(UPSTOX_DUMP, "rt", encoding="utf-8") as f:
                upstox_data = json.load(f)
            for item in upstox_data:
                if item.get("segment") == "NSE_EQ":
                    ts = item.get("trading_symbol")
                    ik = item.get("instrument_key")
                    if ts and ik and ts not in nse_eq:
                        nse_eq[ts] = ik

        # 2. Fetch official Nifty 500 constituents from NSE India
        req = urllib.request.Request(NSE_NIFTY500_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read().decode("utf-8")

        new_universe = {}
        new_sectors = {}
        for row in csv.DictReader(io.StringIO(content)):
            sym = row.get("Symbol", "").strip()
            ind = row.get("Industry", "").strip()
            if not sym:
                continue

            # Resolve instrument key
            ik = nse_eq.get(sym) or Config.SYMBOLS_MAPPING.get(sym)
            if not ik:
                # Check for series variation or isin
                isin = row.get("ISIN Code", "").strip()
                if isin:
                    ik = f"NSE_EQ|{isin}"

            if ik:
                new_universe[sym] = ik
                new_sectors[sym] = ind or "Diversified"

        # Merge pre-existing symbols to ensure none are lost
        for sym, ik in Config.SYMBOLS_MAPPING.items():
            if sym not in new_universe:
                new_universe[sym] = ik
                if sym not in new_sectors:
                    new_sectors[sym] = "Diversified"

        if len(new_universe) >= 200:
            _UNIVERSE_MAP = new_universe
            _SECTOR_MAP = new_sectors

            # Save to disk cache
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "universe": _UNIVERSE_MAP,
                    "sectors": _SECTOR_MAP,
                    "timestamp": time.time(),
                    "count": len(_UNIVERSE_MAP)
                }, f, indent=2)

            logger.info(f"[Nifty500] Successfully compiled & cached {len(_UNIVERSE_MAP)} Nifty 500 stocks.")
            return True
        else:
            logger.warning(f"[Nifty500] Compiled universe too small ({len(new_universe)}). Using fallback.")
            return False

    except Exception as e:
        logger.error(f"[Nifty500] Error building Nifty 500 universe: {e}")
        return False


def _load_universe() -> None:
    """Loads Nifty 500 from cache or triggers rebuild."""
    global _UNIVERSE_MAP, _SECTOR_MAP, _INITIALIZED
    if _INITIALIZED and _UNIVERSE_MAP:
        return

    # Try loading from local disk cache first
    loaded = False
    if os.path.exists(CACHE_FILE):
        try:
            file_age = time.time() - os.path.getmtime(CACHE_FILE)
            if file_age < 604800:  # Fresh within 7 days
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if data.get("universe") and len(data["universe"]) >= 200:
                        _UNIVERSE_MAP = data["universe"]
                        _SECTOR_MAP = data.get("sectors", {})
                        loaded = True
                        logger.info(f"[Nifty500] Loaded {len(_UNIVERSE_MAP)} stocks from cache.")
        except Exception as e:
            logger.warning(f"[Nifty500] Failed to read disk cache: {e}")

    # If not loaded or stale, build fresh
    if not loaded:
        if not _build_nifty500_universe():
            # Fallback to Config.SYMBOLS_MAPPING
            _UNIVERSE_MAP = dict(Config.SYMBOLS_MAPPING)

    # Sync back into Config.SYMBOLS_MAPPING in-memory so entire app benefits
    Config.SYMBOLS_MAPPING.update(_UNIVERSE_MAP)
    _INITIALIZED = True


# Initialize on import
_load_universe()

# Compatibility global SECTOR_MAP export
SECTOR_MAP: Dict[str, str] = _SECTOR_MAP


def get_universe() -> Dict[str, str]:
    """
    Returns the complete Nifty 500 trading universe:
    { "RELIANCE": "NSE_EQ|INE002A01018", "TCS": "NSE_EQ|INE467B01029", ... }
    Contains ~500 verified stocks with live Upstox instrument keys.
    """
    if not _UNIVERSE_MAP:
        _load_universe()
    return dict(_UNIVERSE_MAP)


def get_sector(symbol: str) -> str:
    """Return the official sector/industry for a given symbol."""
    if not _SECTOR_MAP:
        _load_universe()
    return _SECTOR_MAP.get(symbol.strip().upper(), "Others")


def get_sectors_mapping() -> Dict[str, str]:
    """Return the full sector mapping dictionary for all 500 stocks."""
    if not _SECTOR_MAP:
        _load_universe()
    return dict(_SECTOR_MAP)


def get_symbols_by_sector(sector: str) -> List[str]:
    """Return all symbols in a given sector."""
    if not _SECTOR_MAP:
        _load_universe()
    sec_lower = sector.strip().lower()
    return [sym for sym, sec in _SECTOR_MAP.items() if sec.lower() == sec_lower]


def get_all_sectors() -> List[str]:
    """Return all unique sector names across Nifty 500."""
    if not _SECTOR_MAP:
        _load_universe()
    return sorted(set(_SECTOR_MAP.values()))

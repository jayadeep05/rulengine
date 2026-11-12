import logging
from config import Config

logger = logging.getLogger(__name__)

def get_instrument_key(symbol: str) -> str:
    """
    Get the instrument key (token) for a given stock symbol.
    Checks Config.SYMBOLS_MAPPING first, then falls back to instrument_service master list.
    """
    if not symbol:
        return ""
    sym = symbol.strip().upper()
    if sym in Config.SYMBOLS_MAPPING:
        return Config.SYMBOLS_MAPPING[sym]
    
    try:
        from instrument_service import instrument_service
        key = instrument_service.resolve_instrument_token(sym)
        if key:
            Config.SYMBOLS_MAPPING[sym] = key
            return key
        st = instrument_service.get_stock(sym)
        if st and "instrument_key" in st:
            key = st["instrument_key"]
            Config.SYMBOLS_MAPPING[sym] = key
            return key
    except Exception:
        pass
        
    return ""

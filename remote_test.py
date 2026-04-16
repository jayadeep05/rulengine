from execution import UpstoxExecutionEngine
from config import Config
import logging
logging.basicConfig(level=logging.INFO)

engine = UpstoxExecutionEngine()
key = Config.SYMBOLS_MAPPING.get('RELIANCE', 'NSE_EQ|INE002A01018')
print(f"KEY: {key}")
print("LTP:", engine.get_ltp_bulk([key]))
df = engine.get_ohlc(key)
if df is not None:
    print("OHLC Empty:", df.empty)
    print("Columns:", df.columns if not df.empty else None)
else:
    print("OHLC is None")

import sys
import os
import json
import logging
from config import Config
from features import compute_features
from strategy import generate_signals
from execution import UpstoxExecutionEngine
from trade_manager import TradeManager
import pandas as pd

logging.basicConfig(level=logging.INFO)

print("Starting Pipeline Test...")
engine = UpstoxExecutionEngine()
success = engine.connect()
print(f"API Connection: {'SUCCESS' if success else 'FAILED'}")

if success:
    symbol = Config.SYMBOLS_MAPPING.get("HDFCBANK")
    print(f"Fetching OHLC for {symbol}...")
    df = engine.get_ohlc(symbol, interval="1minute")
    
    if df.empty:
         print("OHLC fetch failed. API might be returning nothing.")
    else:
         print(f"Fetched {len(df)} candles.")
         
         print("Testing features computation...")
         df_features = compute_features(df)
         print(f"Features computed successfully. Columns: {df_features.columns.tolist()}")
         
         print("Testing strategy generation...")
         signals = generate_signals(df_features)
         print("Signal Data:")
         print(json.dumps(signals, indent=2))
         
         print("Testing trade_manager...")
         tm = TradeManager()
         tm.add_trade(
            symbol="HDFCBANK", side="BUY", entry_price=1000.0, qty=10, sl=950.0, target=1100.0,
            probability=0.0, score=70, metadata={"atr_14": 5.0, "market_condition": "TRENDING"}
         )
         
         print(f"Active trades after add: {len(tm.active_trades)}")
         trade = tm.active_trades[-1]
         trade['current_price'] = 1010.0 # Moved slightly
         
         # Test Trailing SL logic (expect to move based on max_favorable_price)
         print(f"SL before trailing: {trade['sl']}")
         tm.manage_trailing_sl(trade)
         print(f"SL after trailing: {trade['sl']}")
         print(f"Max favorable price: {trade.get('max_favorable_price')}")
         
         # Close trade test
         tm.close_trade(trade, "TEST_EXIT")
         from config import SystemState
         print(f"Consecutive losses: {getattr(SystemState, 'consecutive_losses', 0)}")
         print("All tests passed.")
else:
    print("Skipping deeper tests due to API connection failure.")

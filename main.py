from fastapi import FastAPI, BackgroundTasks, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import asyncio
import logging
import logging
import pandas as pd
from datetime import datetime, time
import pytz

IST = pytz.timezone('Asia/Kolkata')
from typing import List, Dict

# Local imports
from config import Config, SystemState
from features import compute_features
from strategy import generate_signals
from ai_filter import analyze_trade
from execution import UpstoxExecutionEngine
from trade_manager import TradeManager

# Setup Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI(title="Intraday Trading System API")

# Mount static folder for UI
import os
os.makedirs("frontend", exist_ok=True)
app.mount("/static", StaticFiles(directory="frontend"), name="static")

state = SystemState()
trade_manager = TradeManager()
execution_engine = UpstoxExecutionEngine()

# In-memory state
prev_prices_for_momentum: dict = {}

# Track latest signals for the UI
latest_signals = []
live_prices_cache = {}

@app.on_event("startup")
async def startup_event():
    logger.info("Initializing Intelligence and Trading System...")
    # Connect to Upstox (mock/real depending on MODE)
    execution_engine.connect()
    # Start background scheduler
    asyncio.create_task(trading_cycle_loop())
    asyncio.create_task(live_pricing_pump())

async def live_pricing_pump():
    """Fetches LTP aggressively for the UI without stalling the math engine."""
    while True:
        if not state.is_running or state.kill_switch_active:
            await asyncio.sleep(5)
            continue
        try:
            keys = list(Config.SYMBOLS_MAPPING.values())
            res = execution_engine.get_ltp_bulk(keys)
            
            # Map back to human legible symbol names
            for name, key in Config.SYMBOLS_MAPPING.items():
                if key in res:
                    live_prices_cache[name] = res[key]
        except Exception:
            pass
        await asyncio.sleep(3)  # Sweep every 3 seconds

# Removed mock data generator. Relying exclusively on real execution engine for data.
async def trading_cycle_loop():
    """
    Runs every minute to fetch data, evaluate strategy, and manage trades.
    """
    while True:
        start_time = datetime.now(IST)
        
        if not state.is_running or state.kill_switch_active:
            await asyncio.sleep(10)
            continue
            
        now = datetime.now(IST)
        market_open = time(9, 15)
        market_close = time(15, 15)
        
        # MARKET TIME CONTROL (9:15 AM to 3:15 PM IST)
        if not (market_open <= now.time() <= market_close):
            if Config.MODE == 'LIVE':
                logger.info(f"Market Closed (IST: {now.strftime('%H:%M:%S')}). Waiting for active market hours (09:15 - 15:15)...")
                await asyncio.sleep(60)
                continue

        # ── OPENING RANGE LOCK: No trades before 9:30 AM ────────────────────
        trading_start = time(9, 30)
        if now.time() < trading_start and Config.MODE == 'LIVE':
            logger.info(f"Opening range forming (9:15-9:30 IST: {now.strftime('%H:%M:%S')}). No trades. Waiting...")
            await asyncio.sleep(60)
            continue
            
        logger.info(f"[{Config.MODE}] Running trading cycle...")
        
        current_prices = {}
        global latest_signals, prev_prices_for_momentum
        new_signals = []

        # ── STAGE 1: Bulk LTP fetch for ALL 100 stocks (1 API call) ─────────
        all_keys = list(Config.SYMBOLS_MAPPING.values())
        all_ltps_raw = execution_engine.get_ltp_bulk(all_keys)

        # Build name→ltp map
        name_to_ltp = {}
        for sym_name, sym_key in Config.SYMBOLS_MAPPING.items():
            if sym_key in all_ltps_raw:
                name_to_ltp[sym_name] = all_ltps_raw[sym_key]

        # ── STAGE 2: Filter by Price Range & Rank top 15 movers ──────
        valid_movers = []
        for name, ltp in name_to_ltp.items():
            if Config.MIN_STOCK_PRICE <= ltp <= Config.MAX_STOCK_PRICE:
                delta = abs(ltp - prev_prices_for_momentum.get(name, ltp))
                valid_movers.append((name, delta))
                
        movers = sorted(valid_movers, key=lambda x: x[1], reverse=True)[:15]

        # Update momentum cache for next cycle (all symbols)
        prev_prices_for_momentum = dict(name_to_ltp)

        scan_targets = [m[0] for m in movers] if movers else list(Config.SYMBOLS_MAPPING.keys())[:15]
        logger.info(f"Top 15 movers: {scan_targets[:5]}... (scanning {len(scan_targets)} stocks)")

        for symbol_name in scan_targets:
            symbol_key = Config.SYMBOLS_MAPPING.get(symbol_name)
            if not symbol_key:
                continue

            await asyncio.sleep(0.1)  # Reduced: only 15 stocks now, tighter throttle

            # 1. Fetch 1m OHLCV Data from Upstox
            df = execution_engine.get_ohlc(symbol_key, interval="1minute")
            ltp = name_to_ltp.get(symbol_name, 0.0)

            if df.empty or ltp == 0.0:
                logger.warning(f"Could not fetch real data for {symbol_name}. Skipping this cycle.")
                continue
            
            # Store latest close for PnL update
            current_prices[symbol_name] = ltp
            
            # 2. Compute Features
            df_features = compute_features(df)
            
            # 3. Strategy & Scoring Engine
            signal_data = generate_signals(df_features)
            
            prob = 0.0
            decision = signal_data['decision']
            final_decision = 'AVOID'
            
            # 4. Groq AI Filter 
            if signal_data['score'] >= Config.SCORE_THRESHOLD:
                if Config.USE_GROQ_FILTER:
                    # Execute Groq AI check
                    logger.info(f"Injecting {symbol_name} signal into Groq AI Check...")
                    ai_decision = analyze_trade(df_features.iloc[-2].to_dict())
                    
                    if ai_decision == "AVOID":
                        final_decision = "AVOID"
                    elif ai_decision == "WEAK":
                        final_decision = "AVOID"  # Only trade STRONG
                    else:
                        final_decision = decision
                else:
                    final_decision = decision # TRADE strictly on rule engine!
            
            signal_record = {
                'symbol': symbol_name,
                'instrument_token': symbol_key,
                'decision': final_decision,
                'raw_signal': decision,
                'score': signal_data['score'],
                'probability': prob,
                'entry_price': signal_data['current_price'],
                'sl': signal_data['sl'],
                'target': signal_data['target'],
                'metadata': signal_data.get('metadata', {})
            }
            new_signals.append(signal_record)
            
            if 'metadata' in signal_data and signal_data['metadata']:
                state.market_condition = signal_data['metadata'].get('market_condition', 'WAITING')
            
            # 6. Risk Management & Execution
            if final_decision in ['BUY', 'SELL']:
                execute_trade(symbol_name, symbol_key, final_decision, signal_record)

        latest_signals = new_signals
        
        # 7. Update active trades & check exit conditions continuously using full market pulse
        trade_manager.update_prices(live_prices_cache, execution_engine)
        
        # Check Kill Switch
        daily_pnl = trade_manager.get_daily_pnl()
        daily_loss_pct = daily_pnl / Config.CAPITAL
        
        state.daily_pnl = daily_pnl
        state.trades_taken = trade_manager.get_trades_taken_today()
        
        if daily_loss_pct <= Config.MAX_DAILY_LOSS_PCT:
            logger.critical("KILL SWITCH ACTIVATED! Executing Upstox exit_all_positions()")
            state.kill_switch_active = True
            state.is_running = False
            
            # Fire Upstox Exit API
            execution_engine.exit_all_positions()
            
            # Update local state manager
            for trade in list(trade_manager.active_trades):
                trade_manager.close_trade(trade, "KILL SWITCH")
                
        # Calculate how long this cycle took to avoid drifting execution loops
        end_time = datetime.now(IST)
        execution_duration = (end_time - start_time).total_seconds()
        sleep_time = max(1, 60 - execution_duration)
        
        # Wait until the next actual minute cycle
        await asyncio.sleep(sleep_time)

def execute_trade(symbol_name: str, symbol_key: str, side: str, signal: dict):
    # Instrument Token Validation
    if symbol_name not in Config.SYMBOLS_MAPPING:
        logger.error(f"Invalid symbol exception! {symbol_name} missing from mappings.")
        return

    # Fixed Rules Check
    if trade_manager.get_trades_taken_today() >= Config.MAX_TRADES_PER_DAY:
        logger.info(f"Max trades per day reached [5]. Skipping {symbol_name}.")
        return
        
    if len(trade_manager.active_trades) >= Config.MAX_OPEN_TRADES:
        logger.info(f"Max open trades reached [2]. Skipping {symbol_name}.")
        return
        
    # Duplicate Order Protection & Cooldown
    for t in trade_manager.active_trades:
        if t['symbol'] == symbol_name:
            logger.info(f"Duplicate order blocked for {symbol_name}. Already holding.")
            return

    # Check 5-minute trade cooldown
    now = datetime.now(IST)
    last_trade = state.last_trade_time.get(symbol_name)
    if last_trade and (now - last_trade).total_seconds() < 300:
        logger.info(f"Cooldown active for {symbol_name}. Skipping trade. (< 5 mins)")
        return
    
    # ── POSITION SIZING WITH HARD CAP ────────────────────────────────────────
    risk_per_trade = Config.CAPITAL * Config.RISK_PER_TRADE_PCT
    entry = signal['entry_price']
    sl = signal['sl']

    sl_distance = abs(entry - sl)
    if sl_distance <= 0:
        logger.error(f"Invalid SL Distance for {symbol_name}. Skipping.")
        return

    qty = int(risk_per_trade / sl_distance)

    # HARD CAP: Never let position value exceed 20% of capital
    MAX_POSITION_VALUE = Config.CAPITAL * 0.20
    max_qty_by_value = int(MAX_POSITION_VALUE / entry) if entry > 0 else 0
    qty = min(qty, max_qty_by_value)

    if qty <= 0:
        logger.error(f"Qty=0 after position cap for {symbol_name} (entry=₹{entry:.2f}). Skipping.")
        return

    logger.info(f"Position sizing: {symbol_name} qty={qty}, entry=₹{entry:.2f}, value=₹{qty*entry:.0f}, SL=₹{sl:.2f}, risk=₹{qty*sl_distance:.0f}")
    
    # Execute Upstox API Call
    res = execution_engine.place_order_with_retry(symbol_key, side, qty)
    
    # Order Confirmation Check & API Failure Guard
    if res.get('status') != 'success':
        logger.error(f"Trade execution rejected or failed for {symbol_name}. Aborting.")
        return
        
    # Immediate Stop Loss Processing
    sl_res = execution_engine.place_stop_loss(symbol_key, side, qty, sl)
    
    if sl_res.get('status') != 'success':
        logger.critical(f"SL FAILED for {symbol_name}! Executing naked position immediate exit.")
        # Reverse trade to squash naked position
        counter_side = "SELL" if side == "BUY" else "BUY"
        execution_engine.place_order_with_retry(symbol_key, counter_side, qty)
        return
        
    # Both Main order and SL order executed successfully
    trade_manager.add_trade(
        symbol=symbol_name,
        side=side,
        entry_price=entry,
        qty=qty,
        sl=sl,
        target=signal['target'],
        probability=0.0, # Removed raw param for UI
        score=signal['score'],
        sl_order_id=sl_res.get('order_id')
    )
    trade_manager.active_trades[-1]['instrument_token'] = symbol_key
    state.last_trade_time[symbol_name] = datetime.now(IST)

@app.get("/")
def get_dashboard():
    with open("frontend/index.html", "r", encoding="utf-8") as f:
        html_content = f.read()
    return HTMLResponse(content=html_content)

@app.get("/api/signals")
def api_get_signals():
    return latest_signals

@app.get("/api/live-prices")
def api_get_live_prices():
    return live_prices_cache

@app.get("/api/trades/active")
def api_get_active_trades():
    return trade_manager.active_trades

@app.get("/api/trades/history")
def api_get_trade_history():
    return sorted(trade_manager.trade_history, key=lambda x: x['timestamp'], reverse=True)

@app.get("/api/status")
def api_get_status():
    return {
        "is_running": state.is_running,
        "kill_switch_active": state.kill_switch_active,
        "daily_pnl": state.daily_pnl,
        "trades_taken": state.trades_taken,
        "max_trades": Config.MAX_TRADES_PER_DAY,
        "market_condition": state.market_condition
    }

@app.post("/api/toggle")
def api_toggle_bot():
    state.is_running = not state.is_running
    return {"status": "success", "is_running": state.is_running}

@app.get("/api/config")
def api_get_config():
    # Only return variables, exclude mappings and tokens for security and display
    return {k: getattr(Config, k) for k in dir(Config) if not k.startswith("__") and not callable(getattr(Config, k)) and k not in ['SYMBOLS_MAPPING', 'UPSTOX_ACCESS_TOKEN', 'UPSTOX_CLIENT_ID', 'UPSTOX_CLIENT_SECRET', 'GROQ_API_KEY', 'REDIRECT_URI']}

@app.post("/api/config")
async def api_post_config(request: Request):
    data = await request.json()
    for k, v in data.items():
        if hasattr(Config, k):
            setattr(Config, k, v)
    Config.save()
    return {"status": "success", "message": "Configuration updated successfully!"}

@app.post("/api/emergency")
def api_emergency_stop():
    logger.critical("EMERGENCY STOP INVOKED MANUALLY!")
    state.kill_switch_active = True
    state.is_running = False
    
    execution_engine.exit_all_positions()
    for trade in list(trade_manager.active_trades):
        trade_manager.close_trade(trade, "EMERGENCY EXIT")
        
    return {"status": "success", "message": "All positions squared off."}

class ModifySLRequest(BaseModel):
    new_sl: float

@app.post("/api/trades/{trade_id}/close")
def api_close_trade(trade_id: str):
    target_trade = next((t for t in trade_manager.active_trades if t['id'] == trade_id), None)
    if not target_trade:
        return {"status": "error", "message": "Trade not found"}
        
    symbol_key = target_trade.get('instrument_token', '')
    if not symbol_key:
        return {"status": "error", "message": "Missing instrument token"}
        
    # Cancel SL order if any
    sl_order_id = target_trade.get('sl_order_id')
    if sl_order_id:
        import requests
        try:
            url = f"{execution_engine.api_v3}/order/cancel?order_id={sl_order_id}"
            requests.delete(url, headers=execution_engine.headers, timeout=5)
        except:
            pass

    # Place market exit order
    counter_side = "SELL" if target_trade['side'] == "BUY" else "BUY"
    execution_engine.place_order_with_retry(symbol_key, counter_side, target_trade['qty'])
    trade_manager.close_trade(target_trade, "MANUAL OVERRIDE")
    
    return {"status": "success", "message": "Position closed manually."}

@app.post("/api/trades/{trade_id}/sl")
def api_modify_sl(trade_id: str, payload: ModifySLRequest):
    target_trade = next((t for t in trade_manager.active_trades if t['id'] == trade_id), None)
    if not target_trade:
        return {"status": "error", "message": "Trade not found"}
        
    sl_order_id = target_trade.get('sl_order_id')
    if not sl_order_id:
        return {"status": "error", "message": "No SL order attached to this trade."}
        
    new_trigger = round(payload.new_sl * 20) / 20.0
    mod_res = execution_engine.modify_order(
        order_id=sl_order_id, 
        new_price=0, 
        new_trigger=new_trigger, 
        qty=target_trade['qty'], 
        order_type="SL-M"
    )
    if mod_res.get('status') == 'success':
        target_trade['sl'] = payload.new_sl
        return {"status": "success", "message": f"SL updated to {payload.new_sl}"}
    else:
        return {"status": "error", "message": "Failed to update SL physically via Upstox API."}

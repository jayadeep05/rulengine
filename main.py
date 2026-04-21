from fastapi import FastAPI, BackgroundTasks, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import asyncio
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
from trade_manager import TradeManager
from execution import UpstoxExecutionEngine
import market_feed
from database import SessionLocal, Trade
from sqlalchemy import desc

# Setup Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Force logging to use IST
def ist_converter(*args):
    return datetime.now(IST).timetuple()
logging.Formatter.converter = ist_converter

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
    # Connect to Upstox REST API (profile check)
    execution_engine.connect()
    
    # Initialize state with real data immediately
    try:
        logger.info("Performing initial data fetch...")
        state.active_capital = execution_engine.get_funds()
        # Initial index fetch
        idx_keys = [
            "NSE_INDEX|Nifty 50", "BSE_INDEX|SENSEX", "NSE_INDEX|Nifty Bank",
            "NSE_INDEX|Nifty Fin Service", "NSE_INDEX|NIFTY MID SELECT", "NSE_INDEX|India VIX"
        ]
        res = execution_engine.get_market_quotes(idx_keys)
        for idx in idx_keys:
            if idx in res:
                state.live_indices[idx] = res[idx]['last_price']
                if 'ohlc' in res[idx]:
                    state.index_prev_close[idx] = res[idx]['ohlc'].get('close', 0)
        logger.info(f"Initial capital: ₹{state.active_capital}, Indices: {len(state.live_indices)} fetched.")
    except Exception as e:
        logger.error(f"Error during initial fetch: {e}")

    # ── Launch WebSocket real-time market feed (background daemon thread) ──
    market_feed.start(live_prices_cache, state.live_indices, trade_manager)
    logger.info("[WS] Upstox WebSocket Market Feed started.")
    # Start background task loops
    asyncio.create_task(trading_cycle_loop())
    asyncio.create_task(ws_fallback_pump())
    asyncio.create_task(periodic_stat_sync())

async def periodic_stat_sync():
    """
    Independent task to sync capital and indices every 5 minutes, 
    even when the market is closed or trading is paused.
    """
    while True:
        try:
            # Sync Capital (only every 5 mins to avoid spamming)
            new_cap = execution_engine.get_funds()
            if new_cap > 0:
                state.active_capital = new_cap
            
            # Sync Indices
            idx_keys = [
                "NSE_INDEX|Nifty 50", "BSE_INDEX|SENSEX", "NSE_INDEX|Nifty Bank",
                "NSE_INDEX|Nifty Fin Service", "NSE_INDEX|NIFTY MID SELECT", "NSE_INDEX|India VIX"
            ]
            res = execution_engine.get_market_quotes(idx_keys)
            for idx in idx_keys:
                if idx in res:
                    state.live_indices[idx] = res[idx]['last_price']
                    if 'ohlc' in res[idx]:
                        state.index_prev_close[idx] = res[idx]['ohlc'].get('close', 0)
            
            logger.info(f"[Sync] Capital: ₹{state.active_capital}")
        except Exception as e:
            logger.error(f"[Sync] Error: {e}")
            
        await asyncio.sleep(300) # Every 5 minutes

async def ws_fallback_pump():
    """
    Lightweight fallback: only polls REST if WebSocket hasn't yet delivered any data
    (e.g. very first seconds after startup, or during a long reconnect window).
    Checks every 30 seconds — much gentler than the old 3-second poll.
    """
    while True:
        await asyncio.sleep(30)
        if market_feed.is_live():
            # WebSocket is healthy — nothing to do
            continue
        logger.warning("[Fallback] WS has no data yet — firing one-shot REST bulk LTP fetch...")
        try:
            keys = list(Config.SYMBOLS_MAPPING.values())
            idx_keys = [
                "NSE_INDEX|Nifty 50", "BSE_INDEX|SENSEX", "NSE_INDEX|Nifty Bank",
                "NSE_INDEX|Nifty Fin Service", "NSE_INDEX|NIFTY MID SELECT", "NSE_INDEX|India VIX"
            ]
            keys.extend(idx_keys)
            res = execution_engine.get_ltp_bulk(keys)
            for idx in idx_keys:
                if idx in res:
                    state.live_indices[idx] = res[idx]
            for name, key in Config.SYMBOLS_MAPPING.items():
                if key in res:
                    live_prices_cache[name] = res[key]
        except Exception:
            pass

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
        
        # Refresh capital and indices for the pulse
        state.active_capital = execution_engine.get_funds()
        
        # ── GLOBAL MARKET PULSE ──────────────────────────────────────────────
        nifty_ltp = state.live_indices.get("NSE_INDEX|Nifty 50", 0)
        vix_ltp = state.live_indices.get("NSE_INDEX|India VIX", 0)
        
        if nifty_ltp > 0:
            # We use a simple sentiment based on VIX and direction
            if vix_ltp > 18:
                state.market_condition = "VOLATILE"
            elif vix_ltp < 12:
                state.market_condition = "SIDEWAYS"
            else:
                state.market_condition = "TRENDING"
        else:
            state.market_condition = "WAITING"

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
                
        movers = sorted(valid_movers, key=lambda x: x[1], reverse=True)

        # Update momentum cache for next cycle (all symbols)
        prev_prices_for_momentum = dict(name_to_ltp)

        scan_targets = [m[0] for m in movers] if movers else list(Config.SYMBOLS_MAPPING.keys())
        logger.info(f"Scanning {len(scan_targets)} stocks in full universe...")

        for symbol_name in scan_targets:
            symbol_key = Config.SYMBOLS_MAPPING.get(symbol_name)
            if not symbol_key:
                continue

            await asyncio.sleep(0.15)  # Throttled for safety with 90+ stocks

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
            
            # 4. Filter with Multi-Timeframe (MTF) Alignment
            mtf_approved = True
            macro_trend = "NONE"
            if signal_data['score'] >= Config.SCORE_THRESHOLD and decision in ['BUY', 'SELL']:
                logger.info(f"[{symbol_name}] Pre-Signal detected ({decision}). Checking 60-Min MTF Alignment...")
                df_60m = execution_engine.get_ohlc(symbol_key, interval="60minute")
                if not df_60m.empty and len(df_60m) >= 2:
                    last_1h = df_60m.iloc[-2] # Always use COMPLETED candle
                    macro_trend = "BULLISH" if last_1h['close'] > last_1h['open'] else "BEARISH"
                    
                    if decision == "BUY" and macro_trend == "BEARISH":
                        mtf_approved = False
                        logger.warning(f"[{symbol_name}] MTF REJECT: 1-hour trend is BEARISH. Skipping BUY.")
                    elif decision == "SELL" and macro_trend == "BULLISH":
                        mtf_approved = False
                        logger.warning(f"[{symbol_name}] MTF REJECT: 1-hour trend is BULLISH. Skipping SELL.")
            
            # 5. Execute Groq AI Check
            if signal_data['score'] >= Config.SCORE_THRESHOLD and mtf_approved:
                if Config.USE_GROQ_FILTER:
                    logger.info(f"Injecting {symbol_name} signal into Groq AI Check...")
                    ai_decision = analyze_trade(df_features.iloc[-2].to_dict())
                    
                    if ai_decision in ["AVOID", "WEAK"]:
                        logger.info(f"[{symbol_name}] SKIPPED: AI filter returned {ai_decision}")
                        final_decision = "AVOID"
                    else:
                        final_decision = decision
                else:
                    final_decision = decision # TRADE strictly on rule engine!
            elif signal_data['score'] < Config.SCORE_THRESHOLD and decision in ['BUY', 'SELL']:
                logger.info(f"[{symbol_name}] SKIPPED: Score ({signal_data['score']}) below threshold ({Config.SCORE_THRESHOLD})")
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
            
            # Signal record metadata handling
            if 'metadata' in signal_data and signal_data['metadata']:
                # Individual stock conditions are now stored in signal metadata only
                pass
            
            # 6. Risk Management & Execution
            if final_decision in ['BUY', 'SELL']:
                execute_trade(symbol_name, symbol_key, final_decision, signal_record)

        latest_signals = new_signals
        
        # 7. Update active trades & check exit conditions continuously using full market pulse
        trade_manager.update_prices(live_prices_cache, execution_engine)
        
        # Check Kill Switch
        daily_pnl = trade_manager.get_daily_pnl()
        daily_loss_pct = daily_pnl / state.active_capital if state.active_capital > 0 else 0
        
        state.daily_pnl = daily_pnl
        state.trades_taken = trade_manager.get_trades_taken_today()
        
        if daily_loss_pct <= -abs(Config.MAX_DAILY_LOSS_PCT):
            logger.critical(f"KILL SWITCH ACTIVATED! Daily Loss ({daily_loss_pct*100:.2f}%) hit threshold ({-abs(Config.MAX_DAILY_LOSS_PCT)*100:.2f}%). Executing Upstox exit_all_positions()")
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

    # Removed hardcoded max trades per day and max open trades restrictions.
    # Duplicate Order Protection & Cooldown
    for t in trade_manager.active_trades:
        if t['symbol'] == symbol_name:
            logger.info(f"Duplicate order blocked for {symbol_name}. Already holding.")
            return

    # ── OVERTRADING CONTROL (Phase 4) ────────────────────────────────────────
    # 1. Global Cooldown (After 2 back-to-back losses)
    if hasattr(state, 'loss_cooldown_until') and state.loss_cooldown_until:
        if datetime.now(IST) < state.loss_cooldown_until:
            logger.info("Global cooldown active due to 2 consecutive losses. Skipping trade.")
            return

    # 2. Same Stock Re-entry Block (15 mins)
    now = datetime.now(IST)
    last_trade = state.last_trade_time.get(symbol_name)
    if last_trade and (now - last_trade).total_seconds() < 900:  # 15 minutes
        logger.info(f"Cooldown active for {symbol_name}. Skipping trade. (< 15 mins)")
        return
    
    # ── POSITION SIZING WITH HARD CAP ────────────────────────────────────────
    risk_per_trade = state.active_capital * Config.RISK_PER_TRADE_PCT
    entry = signal['entry_price']
    sl = signal['sl']

    sl_distance = abs(entry - sl)
    if sl_distance <= 0:
        logger.error(f"Invalid SL Distance for {symbol_name}. Skipping.")
        return

    qty = int(risk_per_trade / sl_distance)

    # HARD CAP: Never let position value exceed 20% of capital
    MAX_POSITION_VALUE = state.active_capital * 0.20
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
        sl_order_id=sl_res.get('order_id'),
        metadata=signal.get('metadata')
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

@app.get("/api/trades/all-history")
def api_get_all_history(start_date: str = None, end_date: str = None):
    db = SessionLocal()
    try:
        query = db.query(Trade)
        
        if start_date:
            query = query.filter(Trade.trade_date >= start_date)
        if end_date:
            query = query.filter(Trade.trade_date <= end_date)
            
        trades = query.order_by(desc(Trade.entry_time)).all()
        
        result = []
        for t in trades:
            result.append({
                'id': t.id,
                'date': t.trade_date.isoformat() if t.trade_date else None,
                'entry_time': t.entry_time.isoformat() if t.entry_time else None,
                'exit_time': t.exit_time.isoformat() if t.exit_time else None,
                'symbol': t.symbol,
                'side': t.side,
                'qty': t.quantity or 1,
                'entry_price': t.entry_price,
                'exit_price': t.exit_price,
                'sl': t.sl_price,
                'pnl': t.realized_pnl or 0.0,
                'exit_reason': t.exit_reason,
                'status': t.status if t.status == 'OPEN' else ('Win' if (t.realized_pnl or 0) > 0 else 'Loss' if (t.realized_pnl or 0) < 0 else 'Breakeven')
            })
        return result
    finally:
        db.close()

@app.get("/api/status")
def api_get_status():
    return {
        "is_running": state.is_running,
        "kill_switch_active": state.kill_switch_active,
        "daily_pnl": state.daily_pnl,
        "trades_taken": state.trades_taken,
        "max_trades": Config.MAX_TRADES_PER_DAY,
        "market_condition": state.market_condition,
        "indices": state.live_indices,
        "indices_prev_close": state.index_prev_close,
        "ws_live": market_feed.is_live(),
        "active_capital": state.active_capital
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
            # For secure token handling: only update if not empty, since UI won't show it back
            if k == 'UPSTOX_ACCESS_TOKEN' and (not v or v.strip() == ""):
                continue
            setattr(Config, k, v)
    Config.save()
    
    # Reload execution engine headers if token changed
    if 'UPSTOX_ACCESS_TOKEN' in data and data['UPSTOX_ACCESS_TOKEN'].strip() != "":
        execution_engine.headers['Authorization'] = f'Bearer {Config.UPSTOX_ACCESS_TOKEN}'
        
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

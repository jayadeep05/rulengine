from fastapi import FastAPI, BackgroundTasks, Request, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncio
import time as _time
import logging
import math
import pandas as pd
from datetime import datetime, time
import pytz

IST = pytz.timezone('Asia/Kolkata')
from typing import List, Dict, Optional, Any

# ── Auth imports ──────────────────────────────────────────────────────────────
from auth import (
    authenticate_user, create_access_token, create_refresh_token,
    verify_refresh_token, revoke_access_token, revoke_refresh_token,
    is_rate_limited, record_login_attempt, clear_login_attempts,
    ACCESS_TOKEN_EXP, REFRESH_TOKEN_EXP,
    get_user_by_id, create_user, verify_access_token, hash_password
)
from auth_middleware import require_auth, require_admin, require_master_admin, require_super_admin, get_token_from_request

# Local imports
from config import Config, SystemState
from features import compute_features
from trade_manager import TradeManager
from execution import UpstoxExecutionEngine
import market_feed
from database import (
    SessionLocal, Trade, get_db,
    get_global_trading_enabled, set_global_trading_enabled,
    is_symbol_blacklisted, add_to_daily_blacklist, get_daily_blacklist,
    get_trades_count_today, get_ist_now, to_ist_iso, to_ist_datetime_str
)
from sqlalchemy import desc
from dual_strategy_engine import DualStrategyEngine
from user_bot_registry import get_or_create_bot, get_bot, reload_bot_credentials, all_bots
from credential_service import upsert_credentials, credential_field_status, has_credentials
from instrument_service import instrument_service

# ── v2 Pipeline imports ──────────────────────────────────────────────────────
from pipeline_layer1 import MorningPreFilter
from pipeline_layer2 import WatchlistRefresh
from pipeline_layer3 import SignalScanner
from pipeline_layer4 import FeatureExtractor
from pipeline_layer5 import ClaudeAnalyzer
from telegram_alert import (
    send_telegram, send_telegram_async,
    alert_bot_startup, alert_trade_entry, alert_trade_exit,
    alert_consecutive_losses, alert_daily_limit_reached,
    alert_order_status_unknown, alert_global_halt_activated,
    alert_global_halt_lifted, alert_watchlist_refreshed,
    alert_stale_feed
)
from daily_summary import generate_daily_summary
from reconciliation import run_reconciliation

# Setup Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Force logging to use IST
def ist_converter(*args):
    return datetime.now(IST).timetuple()
logging.Formatter.converter = ist_converter

app = FastAPI(title="Intraday Trading System API")

# ── CORS (tighten origins in production) ──────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Replace with specific domain in prod
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static folder for UI
import os
os.makedirs("frontend", exist_ok=True)
app.mount("/static", StaticFiles(directory="frontend"), name="static")

# ── Shared market-feed state (indices/prices are not user-specific) ──────────
# Each user's bot reads from these shared caches for market data.
# Bot execution (orders, state, SL) is fully per-user via user_bot_registry.
_shared_state     = SystemState()      # live_indices, index_prev_close only
_shared_prices    = {}                 # live LTP cache for all symbols
_shared_timestamps = {}                # tick timestamps to track feed freshness
_shared_engine    = UpstoxExecutionEngine()  # for shared market-feed + index fetching

# ── v2 Pipeline state (per-user instances stored in UserBot, shared instances here) ──
_signal_log: list = []   # Last 50 signal log entries for dashboard
_signal_log_cleared_date = None  # Track date when signal log was cleared at 9:15 AM
_pipeline_stats = {      # Pipeline stats for daily summary
    "stocks_scanned": 0,
    "layer3_signals": 0,
    "claude_enters": 0,
    "claude_calls": 0,
}


# In-memory state
prev_prices_for_momentum: dict = {}
latest_signals: list = []

def send_alert(user_id: Optional[int], message: str):
    """Logs a critical alert and sends to Telegram."""
    prefix = f"[USER {user_id}] " if user_id else "[SYSTEM] "
    alert_msg = f"🔔 ALERT: {prefix}{message}"
    logger.critical(alert_msg)
    send_telegram(alert_msg)

def _db():
    """Return a DB session for use outside of FastAPI dependency injection."""
    return SessionLocal()


@app.on_event("startup")
async def startup_event():
    logger.info("Initializing Multi-User Trading System v2...")
    # Initialize DB tables (creates users, user_credentials, v2 pipeline tables, etc.)
    from database import init_db
    init_db()

    # Load first available credentials from DB for the shared engine / feed
    shared_creds = None
    db = _db()
    try:
        from database import UserCredential
        from credential_service import get_decrypted_credentials
        cred_row = db.query(UserCredential).filter(UserCredential.upstox_access_token != None).first()
        if cred_row:
            logger.info(f"Loading shared credentials from user_id={cred_row.user_id}...")
            shared_creds = get_decrypted_credentials(db, cred_row.user_id)
            _shared_engine._creds = shared_creds
    except Exception as e:
        logger.error(f"Error loading startup credentials: {e}")
    finally:
        db.close()

    # Connect shared engine for market data / index feeds
    _shared_engine.connect()

    # Initial index fetch using shared engine
    try:
        logger.info("Performing initial data fetch...")
        idx_keys = getattr(Config, 'ALL_INDEX_KEYS', [
            "NSE_INDEX|Nifty 50", "BSE_INDEX|SENSEX", "NSE_INDEX|Nifty Bank",
            "NSE_INDEX|Nifty Fin Service", "NSE_INDEX|NIFTY MID SELECT", "NSE_INDEX|India VIX"
        ])
        res = _shared_engine.get_market_quotes(idx_keys)
        for idx in idx_keys:
            key_in_res = idx if idx in res else idx.replace("|", ":")
            if key_in_res in res:
                quote = res[key_in_res]
                _shared_state.live_indices[idx] = quote['last_price']
                if 'net_change' in quote:
                    _shared_state.index_prev_close[idx] = quote['last_price'] - quote['net_change']
                elif 'ohlc' in quote and 'close' in quote['ohlc']:
                    _shared_state.index_prev_close[idx] = quote['ohlc']['close']
        logger.info(f"Indices loaded: {len(_shared_state.live_indices)} fetched.")
    except Exception as e:
        logger.error(f"Error during initial fetch: {e}")

    # Launch WebSocket market feed (shared — serves all users)
    market_feed.start(
        _shared_prices, 
        _shared_state.live_indices, 
        _shared_state.index_prev_close, 
        None, 
        _shared_timestamps,
        credentials=shared_creds
    )
    logger.info("[WS] Upstox WebSocket Market Feed started.")

    # Start Global / International Indices Background Poller
    try:
        from global_indices_service import start_global_indices_poller
        start_global_indices_poller()
        logger.info("[GlobalIndices] Background poller successfully initialized.")
    except Exception as e:
        logger.warning(f"Error starting global indices poller: {e}")

    asyncio.create_task(ws_fallback_pump())
    asyncio.create_task(periodic_stat_sync())
    asyncio.create_task(dynamic_discovery_loop())

    # Auto-resume and register background trading tasks for all configured users
    db_resume = SessionLocal()
    registered_count = 0
    try:
        from database import UserConfig, UserCredential
        all_user_creds = db_resume.query(UserCredential).all()
        for creds in all_user_creds:
            uid = creds.user_id
            bot = get_or_create_bot(uid, db_resume)
            now_t = datetime.now(IST).time()
            is_market_hours = (time(9, 15) <= now_t <= time(15, 15)) and datetime.now(IST).weekday() < 5
            cfg = db_resume.query(UserConfig).filter(UserConfig.user_id == uid).first()
            if is_market_hours and cfg and cfg.is_running == 1 and not bot.state.kill_switch_active:
                bot.state.is_running = True
                bot.state.status = "RUNNING"
            await ensure_user_bot_task(uid, db_resume)
            registered_count += 1
            logger.info(f"[Startup] Autonomous trading task registered for user_id={uid} (running={bot.state.is_running})")
    except Exception as e:
        logger.error(f"[Startup] Error registering user bots: {e}")
    finally:
        db_resume.close()

    asyncio.create_task(bot_supervisor_watchdog())

    # ── Startup Health-Check Telegram Alert ───────────────────────────────────
    try:
        now_ist = datetime.now(IST)
        resumed_count = registered_count
        startup_msg = (
            f"✅ <b>intradayBot server started</b>\n"
            f"🕐 Time: {now_ist.strftime('%d-%b-%Y %H:%M:%S')} IST\n"
            f"🤖 Auto-resumed bots: {resumed_count}\n"
            f"📡 WebSocket feed: active\n"
            f"⚙️ All systems nominal"
        )
        send_telegram(startup_msg)
        logger.info("[Startup] Health-check Telegram alert sent.")
    except Exception as e:
        logger.warning(f"[Startup] Failed to send startup Telegram alert: {e}")

async def dynamic_discovery_loop():
    """
    Review 1 Enhancement: Intraday Discovery Sub-Layer
    Every 15 minutes, query Nifty 500 for massive volume/price breakouts 
    and dynamically inject them into all active user bots' morning_watchlists.
    """
    try:
        from nifty500_instruments import get_universe
        from config import Config
    except ImportError as e:
        logger.error(f"[Discovery] Cannot import universe/config: {e}")
        return

    while True:
        try:
            now = datetime.now(IST)
            is_weekday = now.weekday() < 5
            is_market_hours = is_weekday and (time(9, 15) <= now.time() < time(14, 30))
            if not is_market_hours:
                await asyncio.sleep(60)
                continue

            # Need a valid bot/execution engine to fetch data
            # Use the first active bot's engine
            active_bots = all_bots()
            if not active_bots:
                await asyncio.sleep(60)
                continue

            bot = list(active_bots.values())[0]
            engine = bot.engine
            if not engine:
                await asyncio.sleep(60)
                continue

            universe = get_universe()
            # Resolve to keys using Config
            keys_to_symbols = {}
            for sym in universe:
                k = Config.SYMBOLS_MAPPING.get(sym.upper())
                if k:
                    keys_to_symbols[k] = sym

            all_keys = list(keys_to_symbols.keys())
            
            # Fetch Nifty and Sector Data for Relative Strength
            nifty_chg_pct = 0.0
            if hasattr(bot, 'layer4'):
                nifty_chg_pct = bot.layer4.get_nifty_change_pct()
                
            from pipeline_layer3 import SignalScanner
            dummy_l3 = SignalScanner(user_id=bot.user_id)
            sector_perf = dummy_l3._sector_perf if hasattr(dummy_l3, '_sector_perf') else {}

            scored_candidates = []

            # Fetch quotes in chunks of 50
            chunk_size = 50
            for i in range(0, len(all_keys), chunk_size):
                chunk = all_keys[i:i + chunk_size]
                try:
                    quotes = engine.get_market_quotes(chunk)
                    for k, q in quotes.items():
                        if 'ohlc' not in q:
                            continue
                        ohlc = q['ohlc']
                        open_p = ohlc.get('open', 0)
                        close_p = ohlc.get('close', 0)
                        
                        vol = q.get('volume', 0)
                        if vol == 0 or open_p == 0:
                            continue
                            
                        # Scan criteria: Up > 1.5% from open (lowered to allow more ranking context)
                        pct_from_open = ((close_p - open_p) / open_p) * 100
                        if pct_from_open > 1.5:
                            sym = keys_to_symbols[k]
                            
                            # Calculate scoring components
                            rs_vs_nifty = pct_from_open - nifty_chg_pct
                            
                            from nifty500_instruments import get_sector
                            sector = get_sector(sym)
                            sector_strength = sector_perf.get(sector, 0.0) if sector else 0.0
                            
                            # Composite Discovery Score
                            score = pct_from_open + rs_vs_nifty + (sector_strength * 0.5)
                            scored_candidates.append((score, sym))
                            
                except Exception as ce:
                    logger.warning(f"[Discovery] Chunk fetch error: {ce}")
                
                await asyncio.sleep(1) # throttle API

            if scored_candidates:
                # Sort descending and take top 20, storing rank and score
                scored_candidates.sort(key=lambda x: x[0], reverse=True)
                top_20 = [(i+1, x[1], x[0]) for i, x in enumerate(scored_candidates[:20])] # (Rank, Symbol, Score)
                
                # Add to MorningWatchlist in DB so Layer 2 picks it up across all user bots
                from database import SessionLocal, MorningWatchlist, get_ist_date, get_ist_now
                db = SessionLocal()
                try:
                    today = get_ist_date()
                    existing_symbols = {r.symbol for r in db.query(MorningWatchlist).filter(MorningWatchlist.date == today).all()}
                    
                    new_symbols_count = 0
                    for rank, sym, score in top_20:
                        if sym not in existing_symbols:
                            new_record = MorningWatchlist(
                                date=today,
                                symbol=sym,
                                instrument_key=universe.get(sym),
                                direction_bias="LONG_CANDIDATE", # Breakouts are naturally longs
                                discovery_score=float(score),
                                discovery_rank=int(rank),
                                created_at=get_ist_now()
                            )
                            db.add(new_record)
                            existing_symbols.add(sym)
                            new_symbols_count += 1
                    
                    if new_symbols_count > 0:
                        db.commit()
                        logger.info(f"[Discovery] Injected {new_symbols_count} top-ranked breakouts into DB. Top score: {scored_candidates[0][0]:.2f}")
                except Exception as db_err:
                    logger.error(f"[Discovery] DB Insert Error: {db_err}")
                    db.rollback()
                finally:
                    db.close()

        except Exception as e:
            logger.error(f"[Discovery] Error in discovery loop: {e}")

        # Run every 15 minutes
        await asyncio.sleep(15 * 60)


async def periodic_stat_sync():
    """
    Sync shared indices every 5 minutes during market hours (not user-specific).
    Per-user capital is synced inside each user's trading cycle.
    """
    while True:
        try:
            now = datetime.now(IST)
            is_weekday = now.weekday() < 5
            is_market_hours = is_weekday and (time(9, 15) <= now.time() <= time(15, 30))
            if not is_market_hours:
                await asyncio.sleep(60)
                continue

            idx_keys = getattr(Config, 'ALL_INDEX_KEYS', [
                "NSE_INDEX|Nifty 50", "BSE_INDEX|SENSEX", "NSE_INDEX|Nifty Bank",
                "NSE_INDEX|Nifty Fin Service", "NSE_INDEX|NIFTY MID SELECT", "NSE_INDEX|India VIX"
            ])
            res = _shared_engine.get_market_quotes(idx_keys)
            for idx in idx_keys:
                key_in_res = idx if idx in res else idx.replace("|", ":")
                if key_in_res in res:
                    quote = res[key_in_res]
                    _shared_state.live_indices[idx] = quote['last_price']
                    if 'net_change' in quote:
                        _shared_state.index_prev_close[idx] = quote['last_price'] - quote['net_change']
                    elif 'ohlc' in quote and 'close' in quote['ohlc']:
                        _shared_state.index_prev_close[idx] = quote['ohlc']['close']
            logger.info(f"[Sync] Indices updated: {len(_shared_state.live_indices)}")
        except Exception as e:
            logger.error(f"[Sync] Error: {e}")
        await asyncio.sleep(300)

async def ws_fallback_pump():
    """
    Fallback REST polling for indices/equities when WebSocket is stale.
    """
    while True:
        now = datetime.now(IST)
        is_weekday = now.weekday() < 5
        is_market_hours = is_weekday and (time(9, 15) <= now.time() <= time(15, 30))
        
        if not is_market_hours:
            await asyncio.sleep(60)
            continue

        await asyncio.sleep(10)

        if not market_feed.is_index_live():
            idx_keys = getattr(Config, 'ALL_INDEX_KEYS', [
                "NSE_INDEX|Nifty 50", "BSE_INDEX|SENSEX", "NSE_INDEX|Nifty Bank",
                "NSE_INDEX|Nifty Fin Service", "NSE_INDEX|NIFTY MID SELECT", "NSE_INDEX|India VIX"
            ])
            try:
                res = _shared_engine.get_ltp_bulk(idx_keys)
                for idx in idx_keys:
                    if idx in res:
                        _shared_state.live_indices[idx] = res[idx]
            except Exception as e:
                logger.debug(f"[Fallback] Index REST error: {e}")

        if not market_feed.is_equity_live():
            try:
                keys = list(Config.SYMBOLS_MAPPING.values())
                res = _shared_engine.get_ltp_bulk(keys)
                for ticker, ltp in res.items():
                    _shared_prices[ticker] = ltp
            except Exception as e:
                logger.warning(f"[Fallback] Equity REST error: {e}")

def run_local_rule_analysis(feature_payloads: list) -> list:
    """
    Programmatic fallback when Generative AI filter is disabled.
    Applies the exact same entry, SL, target, and skip rules as Claude Analyzer in Python.
    """
    decisions = []
    for payload in feature_payloads:
        symbol = payload["symbol"]
        direction = payload["signal_direction"]
        ltp = payload["price_context"]["ltp"]
        atr = payload["price_context"]["atr_14_5min"]
        support = payload["levels"]["support_20bar_5min"]
        resistance = payload["levels"]["resistance_20bar_5min"]
        
        # 1. Entry price calculation (with buffer: 0.075%)
        if direction == "LONG":
            entry_price = round(ltp * 1.00075, 2)
            stop_loss = round(support * 0.999, 2)
        else:
            entry_price = round(ltp * 0.99925, 2)
            stop_loss = round(resistance * 1.001, 2)
            
        risk = abs(entry_price - stop_loss)
        
        # 2. Risk check & SL widening (to 1x ATR if too close)
        min_risk = entry_price * 0.003
        if risk < min_risk:
            if direction == "LONG":
                stop_loss = round(entry_price - atr, 2)
            else:
                stop_loss = round(entry_price + atr, 2)
            risk = abs(entry_price - stop_loss)
            
        # 3. Target calculation (1.5x R:R minimum)
        if direction == "LONG":
            target = round(entry_price + (1.5 * risk), 2)
        else:
            target = round(entry_price - (1.5 * risk), 2)
            
        # 4. Mandatory skip checks
        decision_val = "ENTER"
        reason = "Local rules: setup meets all quantitative criteria."
        red_flags = []
        
        price_vs_vwap = payload["price_context"]["price_vs_vwap_pct"]
        dist_to_res = payload["levels"]["distance_to_resistance_pct"]
        dist_to_sup = payload["levels"]["distance_to_support_pct"]
        volume_trend = payload["volume"]["volume_trend"]
        vol_ratio = payload["volume"]["vol_ratio_6bar_5min"]
        rsi_14_daily = payload["momentum"]["rsi_14_daily"]
        
        # Rule 2: price vs VWAP check
        if direction == "LONG" and price_vs_vwap <= 0:
            decision_val = "SKIP"
            reason = "Price is below VWAP for LONG setup."
            red_flags.append("below VWAP")
        elif direction == "SHORT" and price_vs_vwap >= 0:
            decision_val = "SKIP"
            reason = "Price is above VWAP for SHORT setup."
            red_flags.append("above VWAP")
            
        # Rule 3: Support/Resistance proximity check
        if direction == "LONG" and dist_to_res < 0.3:
            decision_val = "SKIP"
            reason = "Too close to resistance."
            red_flags.append("near resistance")
        elif direction == "SHORT" and dist_to_sup < 0.3:
            decision_val = "SKIP"
            reason = "Too close to support."
            red_flags.append("near support")
            
        # Rule 4: SL wider than 1.5x ATR check
        if risk > 1.5 * atr:
            decision_val = "SKIP"
            reason = "Stop-loss wider than 1.5x ATR."
            red_flags.append("risk too large")
            
        # Rule 5: Volume contracting check
        if volume_trend == "contracting" and vol_ratio < 1.5:
            decision_val = "SKIP"
            reason = "Volume contracting and volume ratio too low."
            red_flags.append("volume declining")
            
        # 5. Confidence scoring
        confidence = 3
        if vol_ratio >= 2.5:
            confidence += 1
        nifty_trend = payload["market_context"]["nifty_trend_today"]
        if (direction == "LONG" and nifty_trend == "bullish") or (direction == "SHORT" and nifty_trend == "bearish"):
            confidence += 1
            
        rs = payload["market_context"]["stock_vs_nifty_5day_pct"]
        if direction == "LONG" and rs < 0:
            confidence -= 1
            red_flags.append("weak relative strength")
        elif direction == "SHORT" and rs > 0:
            confidence -= 1
            red_flags.append("strong relative strength against short")
            
        if direction == "LONG" and rsi_14_daily > 72:
            confidence -= 1
            red_flags.append("extended daily RSI")
        elif direction == "SHORT" and rsi_14_daily < 28:
            confidence -= 1
            red_flags.append("oversold daily RSI")

        # Leading Index Trend (Bank Nifty - Gap 1)
        bn_slope = payload["market_context"].get("bank_nifty_slope", "flat")
        bn_change = payload["market_context"].get("bank_nifty_change_3bar_pct", 0.0)
        if direction == "LONG":
            if bn_slope == "falling" and bn_change < -0.15:
                confidence -= 1
                red_flags.append("bank nifty counter-trend")
            if bn_slope == "falling" and bn_change < -0.30:
                decision_val = "SKIP"
                reason = f"Bank Nifty is falling strongly ({bn_change:.2f}%)."
                red_flags.append("bank nifty counter-trend")
        elif direction == "SHORT":
            if bn_slope == "rising" and bn_change > 0.15:
                confidence -= 1
                red_flags.append("bank nifty counter-trend")
            if bn_slope == "rising" and bn_change > 0.30:
                decision_val = "SKIP"
                reason = f"Bank Nifty is rising strongly (+{bn_change:.2f}%)."
                red_flags.append("bank nifty counter-trend")

        # Previous Day High/Low Confirmation (Gap 2)
        cleared_pdh = payload["levels"].get("cleared_previous_day_high", False)
        cleared_pdl = payload["levels"].get("cleared_previous_day_low", False)
        if direction == "LONG":
            if cleared_pdh:
                confidence += 1
            else:
                if vol_ratio <= 4.0:
                    confidence -= 1
                    red_flags.append("inside yesterday range")
        elif direction == "SHORT":
            if cleared_pdl:
                confidence += 1
            else:
                if vol_ratio <= 4.0:
                    confidence -= 1
                    red_flags.append("inside yesterday range")
            
        confidence = max(1, min(5, confidence))
        if confidence < 3:
            decision_val = "SKIP"
            reason = f"Confidence score too low ({confidence}/5)."
            
        decisions.append({
            "symbol": symbol,
            "decision": decision_val,
            "direction": direction,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "target": target,
            "confidence": confidence,
            "reasoning": reason,
            "red_flags": ",".join(red_flags) if red_flags else "none"
        })
    return decisions

# ────────────────────────────────────────────────────────────────────────────
# PER-USER TRADING CYCLE (v2 Pipeline-Based)
# ────────────────────────────────────────────────────────────────────────────

def _evaluate_market_regime(bot, user_id, layer4, participation_ratio):
    from telegram_alert import send_telegram
    nifty_chg_pct = layer4.get_nifty_change_pct()
    vix_val = _shared_state.live_indices.get("NSE_INDEX|India VIX", 0)
    
    nifty_df = bot.engine.get_ohlc("NSE_INDEX|Nifty 50", interval="5minute")
    nifty_high_vs_open = 0.0
    nifty_low_vs_open = 0.0
    
    if not nifty_df.empty:
        nifty_df['ist_time'] = nifty_df['timestamp'].dt.tz_convert('Asia/Kolkata') if nifty_df['timestamp'].dt.tz else nifty_df['timestamp'].dt.tz_localize('Asia/Kolkata')
        mask = (nifty_df['ist_time'].dt.time >= time(9, 15)) & (nifty_df['ist_time'].dt.time < time(9, 30))
        nifty_15m = nifty_df.loc[mask]
        if not nifty_15m.empty:
            n_open = float(nifty_15m.iloc[0]['open'])
            n_high = float(nifty_15m['high'].max())
            n_low = float(nifty_15m['low'].min())
            nifty_high_vs_open = (n_high - n_open) / n_open * 100
            nifty_low_vs_open = (n_low - n_open) / n_open * 100
            
    # Regime Logic
    old_regime = getattr(bot.state, 'market_regime', "UNKNOWN")
    
    regime_flags = []
    
    if nifty_chg_pct < -1.5:
        regime_flags.append("TRENDING_DOWN") # Hourly override logic for material changes
    elif nifty_chg_pct < -0.3 and nifty_low_vs_open < 0:
        regime_flags.append("TRENDING_DOWN")
    elif nifty_chg_pct > 0.3 and nifty_high_vs_open > 0:
        regime_flags.append("TRENDING_UP")
        
    if vix_val > 18:
        regime_flags.append("HIGH_VOLATILITY")
        
    if abs(nifty_chg_pct) < 0.3 and participation_ratio < 1.2:
        regime_flags.append("RANGING")
        
    if not regime_flags:
        regime_flags.append("RANGING") # Default cautious

    new_regime = ",".join(regime_flags)

    bot.state.market_regime = new_regime
    logger.info(f"[USER {user_id}] Market Regime Classified: {bot.state.market_regime} (Nifty: {nifty_chg_pct:.2f}%, VIX: {vix_val:.1f})")
    
    if old_regime != "UNKNOWN" and old_regime != new_regime:
        send_telegram(f"⚠️ Market regime dynamically updated from {old_regime} to {new_regime} (Nifty: {nifty_chg_pct:.2f}%, VIX: {vix_val:.1f})")


async def user_trading_cycle(user_id: int):
    """
    Per-user trading cycle — v2 pipeline-based.
    Runs as its own asyncio Task. Implements the full daily schedule (Section 7).
    """
    global _signal_log, _pipeline_stats, latest_signals, _signal_log_cleared_date
    from database import SessionLocal, UserConfig, UserCredential, SystemLog, log_system_event

    from user_bot_registry import get_bot
    bot = get_bot(user_id)
    if not bot:
        logger.error(f"[USER {user_id}] Bot not initialized. Cannot start cycle.")
        return

    logger.info(f"[USER {user_id}] v2 Pipeline trading cycle started.")
    
    # ── Initialize pipeline components ────────────────────────────────────
    layer1 = MorningPreFilter(bot.engine)
    layer2 = WatchlistRefresh(bot.engine, user_id=user_id)
    layer3 = SignalScanner(user_id=user_id)
    layer4 = FeatureExtractor(bot.engine)
    layer5 = ClaudeAnalyzer()
    from pipeline_orb import OpeningRangeBreakout
    orb    = OpeningRangeBreakout(user_id=user_id)

    # Track daily state
    layer1_done = False
    last_layer2_refresh = 0.0  # Timestamp of last 15-minute watchlist refresh
    daily_summary_sent = False
    reconciliation_done = False
    ws_disconnected = False
    orb_obs_complete = False   # ORB: True once 9:30 AM observation window closes
    orb_last_scan    = 0.0     # ORB: timestamp of last 1-min breakout scan
    last_l3_scan     = 0.0     # Momentum: timestamp of last 5-min Layer 3-5 scan
    token_refreshed_today = False
    last_token_refresh_attempt = 0.0
    
    # Recovery: Load watchlist from DB if restarting mid-day
    now = datetime.now(IST)
    current_date = now.date()
    
    # Recovery: check if daily summary, reconciliation, or token refresh was already done today
    db_rec = SessionLocal()
    try:
        from database import SystemLog, UserCredential
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        
        # Check if token was already updated today
        cred_chk = db_rec.query(UserCredential).filter(UserCredential.user_id == user_id).first()
        if cred_chk and cred_chk.updated_at and cred_chk.updated_at.date() == current_date:
            token_refreshed_today = True
            logger.info(f"[USER {user_id}] Recovered state: Upstox token was already refreshed today.")
        
        # Query DAILY_SUMMARY
        summary_log = db_rec.query(SystemLog).filter(
            SystemLog.user_id == user_id,
            SystemLog.event_type == "DAILY_SUMMARY",
            SystemLog.timestamp >= start_of_day
        ).first()
        if summary_log:
            daily_summary_sent = True
            logger.info(f"[USER {user_id}] Recovered state: Daily summary was already sent today.")

        # Query RECONCILIATION
        recon_log = db_rec.query(SystemLog).filter(
            SystemLog.user_id == user_id,
            SystemLog.event_type == "RECONCILIATION",
            SystemLog.timestamp >= start_of_day
        ).first()
        if recon_log:
            reconciliation_done = True
            logger.info(f"[USER {user_id}] Recovered state: Reconciliation was already done today.")
    except Exception as rec_err:
        logger.error(f"[USER {user_id}] Failed to recover daily summary/reconciliation state: {rec_err}")
    finally:
        db_rec.close()

    last_token = bot.creds.get("upstox_access_token") if (bot and bot.creds) else None
    if now.hour >= 9:
        existing_watchlist = layer2.load_active_watchlist()
        if existing_watchlist:
            logger.info(f"[USER {user_id}] Recovered {len(existing_watchlist)} active watchlist stocks from DB.")
            layer1_done = True  # Skip Layer 1 if watchlist exists
            if now.time() >= time(9, 30):
                try:
                    orb.recover_ranges_from_candles(layer2.active_watchlist, bot.engine)
                    orb_obs_complete = True
                except Exception as ex:
                    logger.error(f"[USER {user_id}] Failed to recover ORB ranges on startup: {ex}")
        elif now.hour >= 10:
            # Past 9 AM, try loading morning watchlist
            morning = MorningPreFilter.load_from_db()
            if morning:
                layer1.watchlist = morning
                layer2.active_watchlist = [{"symbol": s["symbol"], "instrument_key": s["instrument_key"], 
                                           "direction_bias": s["direction_bias"]} for s in morning]
                layer1_done = True
                logger.info(f"[USER {user_id}] Recovered {len(morning)} morning watchlist stocks from DB.")
                if now.time() >= time(9, 30):
                    try:
                        orb.recover_ranges_from_candles(layer2.active_watchlist, bot.engine)
                        orb_obs_complete = True
                    except Exception as ex:
                        logger.error(f"[USER {user_id}] Failed to recover ORB ranges on startup: {ex}")

    while True:
        bot = get_bot(user_id)  # re-fetch in case of hot-reload
        if not bot:
            break

        # Check if Upstox token was refreshed/updated mid-day
        current_token = bot.creds.get("upstox_access_token") if (bot and bot.creds) else None
        if current_token and current_token != last_token:
            logger.info(f"[USER {user_id}] Detected token update mid-day. Resetting Layer 1 flag to allow watchlist rebuild.")
            last_token = current_token
            if not layer1.watchlist:
                layer1_done = False

        now = datetime.now(IST)

        # Clear previous day's signal log and daily pipeline stats at 9:15 AM on trading days
        if now.time() >= time(9, 15) and _signal_log_cleared_date != now.date():
            logger.info(f"[USER {user_id}] 9:15 AM reached. Clearing yesterday's live signal logs and daily pipeline stats.")
            _signal_log = []
            _pipeline_stats = {
                "stocks_scanned": 0,
                "layer3_signals": 0,
                "claude_enters": 0,
                "claude_calls": 0,
            }
            _signal_log_cleared_date = now.date()

        # Reset daily state on a new calendar day
        # Reset daily state on a new calendar day
        if now.date() != current_date:
            logger.info(f"[USER {user_id}] New day detected ({now.date()}). Resetting daily pipeline flags.")
            layer1_done = False
            last_layer2_refresh = 0.0
            daily_summary_sent = False
            reconciliation_done = False
            ws_disconnected = False
            orb_obs_complete = False
            token_refreshed_today = False
            last_token_refresh_attempt = 0.0
            try:
                orb.reset()
            except Exception as orb_err:
                logger.error(f"[USER {user_id}] Error in orb.reset(): {orb_err}")
            try:
                if hasattr(layer5, "reset"):
                    layer5.reset()
                elif hasattr(layer5, "reset_daily_stats"):
                    layer5.reset_daily_stats()
            except Exception as l5_err:
                logger.error(f"[USER {user_id}] Error in layer5.reset(): {l5_err}")
            
            setattr(bot.state, '_token_verified_0850', False)

            # Reset VIX alert flags for the new day
            try:
                from telegram_alert import reset_vix_alerts
                reset_vix_alerts()
            except Exception as reset_err:
                logger.error(f"Error resetting VIX alerts: {reset_err}")
                
            current_date = now.date()

        logger.info(f"[USER {user_id}] Heartbeat: Trading cycle active. Trades: {len(bot.tm.active_trades)}, Watchlist: {len(layer2.active_watchlist)}")

        # ── Trade monitoring runs ALWAYS (even during halt) — Bug Fix 7 ───
        # Active position monitoring continues regardless of global halt
        if bot.tm.active_trades:
            vix_ltp = _shared_state.live_indices.get("NSE_INDEX|India VIX", 0)
            nifty_ltp = _shared_state.live_indices.get("NSE_INDEX|Nifty 50", 0)
            bot.state.market_condition = "VOLATILE" if vix_ltp > 18 else ("SIDEWAYS" if vix_ltp < 12 else "TRENDING")
            
            # Update prices for active trades
            name_to_ltp = {}
            for sym_name, sym_key in Config.SYMBOLS_MAPPING.items():
                if sym_name in _shared_prices:
                    name_to_ltp[sym_name] = _shared_prices[sym_name]
                elif sym_key in _shared_prices:
                    name_to_ltp[sym_name] = _shared_prices[sym_key]
            bot.tm.update_prices(name_to_ltp, bot.engine)

        # ── Token Health & Auto-Sync Check ─────────────────────────────────
        import time as _t
        now_ts = _t.time()
        
        # Check token health periodically (every 5 min) or at 8:00 AM / 8:55 AM
        is_token_check_due = (now_ts - getattr(bot.state, '_last_token_check_ts', 0)) > 300
        should_refresh_morning = (now.hour == 8 and not token_refreshed_today)
        
        if is_token_check_due or should_refresh_morning:
            bot.state._last_token_check_ts = now_ts
            is_valid = bot.engine.connect()
            
            # If token is invalid in memory, try reloading from database first!
            if not is_valid:
                with SessionLocal() as s_db:
                    reload_bot_credentials(user_id, s_db)
                bot = get_bot(user_id)
                if bot and bot.creds:
                    _shared_engine._creds = bot.creds
                is_valid = bot.engine.connect()
                if is_valid:
                    logger.info(f"[USER {user_id}] Successfully synchronized fresh Upstox token from database.")
                    token_refreshed_today = True

            # If still invalid, or if scheduled 8:00 AM morning refresh has not run today
            if (not is_valid and (now_ts - last_token_refresh_attempt) > 300) or (should_refresh_morning and not token_refreshed_today):
                last_token_refresh_attempt = now_ts
                try:
                    from upstox_auth_manager import auto_refresh_and_apply
                    logger.info(f"[USER {user_id}] Checking / Refreshing Upstox Token via TOTP...")
                    res = auto_refresh_and_apply(silent=token_refreshed_today)
                    if res.get("status") == "success":
                        token_refreshed_today = True
                        with SessionLocal() as s_db:
                            reload_bot_credentials(user_id, s_db)
                        bot = get_bot(user_id)
                        new_tok = res.get("access_token")
                        if bot and new_tok:
                            bot.creds["upstox_access_token"] = new_tok
                            bot.engine._creds["upstox_access_token"] = new_tok
                            _shared_engine._creds["upstox_access_token"] = new_tok
                        logger.info(f"[USER {user_id}] Upstox token successfully refreshed via TOTP.")
                except Exception as e:
                    logger.error(f"[USER {user_id}] Failed auto-refreshing Upstox token: {e}")

        # ── 08:50 AM — Token verification & WebSocket Startup ─────────────
        if now.hour == 8 and now.minute >= 50 and not getattr(bot.state, '_token_verified_0850', False):
            setattr(bot.state, '_token_verified_0850', True)
            logger.info(f"[USER {user_id}] 8:50 AM — Verifying Upstox API token validity...")
            is_valid = bot.engine.connect()
            if not is_valid:
                retry_count = 0
                while not is_valid and retry_count < 2:
                    logger.warning(f"[USER {user_id}] Upstox token invalid. Retrying connection (Attempt {retry_count + 1})...")
                    reload_bot_credentials(user_id, _db())
                    bot = get_bot(user_id)
                    is_valid = bot.engine.connect()
                    retry_count += 1
                    await asyncio.sleep(2)
                
                # If still invalid, perform an automatic emergency TOTP refresh!
                if not is_valid:
                    try:
                        from upstox_auth_manager import auto_refresh_and_apply
                        logger.warning(f"[USER {user_id}] Attempting emergency TOTP token auto-refresh at 8:50 AM...")
                        ref_res = auto_refresh_and_apply(silent=False)
                        if ref_res.get("status") == "success":
                            with SessionLocal() as s_db:
                                reload_bot_credentials(user_id, s_db)
                            bot = get_bot(user_id)
                            new_tok = ref_res.get("access_token")
                            if new_tok and bot:
                                bot.creds["upstox_access_token"] = new_tok
                                bot.engine._creds["upstox_access_token"] = new_tok
                                _shared_engine._creds["upstox_access_token"] = new_tok
                            is_valid = bot.engine.connect()
                    except Exception as ref_err:
                        logger.error(f"[USER {user_id}] Emergency TOTP refresh failed at 8:50 AM: {ref_err}")

                if not is_valid:
                    from telegram_alert import alert_token_refresh_failed
                    alert_token_refresh_failed()
                    logger.critical(f"[USER {user_id}] Upstox token verification failed at 8:50 AM. Aborting startup.")
                    bot.state.is_running = False
                    continue
            logger.info(f"[USER {user_id}] Upstox token verified successfully.")

            # Restart WebSocket feed for the new day if it was stopped
            if market_feed._stop_event.is_set():
                logger.info(f"[USER {user_id}] Restarting WebSocket Feed for the new day...")
                market_feed.start(
                    _shared_prices, 
                    _shared_state.live_indices, 
                    _shared_state.index_prev_close, 
                    None, 
                    _shared_timestamps,
                    credentials=bot.creds
                )

        # ── Autonomous Daily Startup: Automatically activate trading system at 08:50 AM ──
        is_weekday = now.weekday() < 5
        is_active_window = (now.hour == 8 and now.minute >= 50) or (9 <= now.hour < 15) or (now.hour == 15 and now.minute < 15)
        if is_weekday and is_active_window and not bot.state.is_running and not bot.state.kill_switch_active:
            bot.state.is_running = True
            bot.state.status = "RUNNING"
            logger.info(f"[AUTONOMOUS] 08:50 AM IST — Automatically started trading system for user_id={user_id} (Mode: {bot.config.get('MODE', 'LIVE')})")
            try:
                from telegram_alert import send_telegram
                send_telegram(
                    f"🚀 <b>IntradayBot Auto-Started!</b>\n"
                    f"System is <b>RUNNING</b> (08:50 AM IST).\n"
                    f"Mode: <b>{bot.config.get('MODE', 'LIVE')}</b>\n"
                    f"Pre-market preparation active. Layer 1 scan scheduled for 09:00 AM."
                )
            except Exception:
                pass
            with SessionLocal() as db_start:
                u_cfg = db_start.query(UserConfig).filter(UserConfig.user_id == user_id).first()
                if u_cfg:
                    u_cfg.is_running = 1
                    db_start.commit()

        # ── 09:00 AM — Layer 1: Morning Pre-Filter ───────────────────────
        if not layer1_done and now.hour >= 9:
            # Self-heal token if expired before Layer 1 scan
            if not bot.engine.connect():
                try:
                    from upstox_auth_manager import auto_refresh_and_apply
                    logger.warning(f"[USER {user_id}] Token connection invalid at 09:00 AM. Refreshing via TOTP before Layer 1...")
                    ref_res = auto_refresh_and_apply(silent=True)
                    if ref_res.get("status") == "success":
                        with SessionLocal() as s_db:
                            reload_bot_credentials(user_id, s_db)
                        bot = get_bot(user_id)
                        new_tok = ref_res.get("access_token")
                        if new_tok and bot:
                            bot.creds["upstox_access_token"] = new_tok
                            bot.engine._creds["upstox_access_token"] = new_tok
                            _shared_engine._creds["upstox_access_token"] = new_tok
                except Exception as pre_l1_err:
                    logger.error(f"[USER {user_id}] Pre-Layer 1 token refresh error: {pre_l1_err}")

            try:
                logger.info(f"[USER {user_id}] Running Layer 1 morning pre-filter...")
                watchlist = layer1.run()
                layer2.active_watchlist = [{"symbol": s["symbol"], "instrument_key": s["instrument_key"],
                                           "direction_bias": s["direction_bias"]} for s in watchlist]
                layer1_done = True
                alert_bot_startup(now.strftime("%d-%b-%Y"), [s["symbol"] for s in watchlist])
                logger.info(f"[USER {user_id}] Layer 1 complete. {len(watchlist)} stocks in morning watchlist.")
            except Exception as e:
                logger.error(f"[USER {user_id}] Layer 1 failed: {e}")
                layer1_done = True  # Don't retry infinitely

        # ── Wait for Market Open (09:15 AM) before evaluating trade signals ──
        if now.time() < time(9, 15):
            await asyncio.sleep(15)
            continue

        # If user explicitly paused system or kill-switch tripped, pause new trading
        if not bot.state.is_running or bot.state.kill_switch_active:
            await asyncio.sleep(10)
            continue

        # ── ORB Phase 1: Observation window (9:15 AM – 9:30 AM) ─────────
        if not orb_obs_complete and time(9, 15) <= now.time() < time(9, 30):
            if layer2.active_watchlist:
                orb.observe(layer2.active_watchlist, _shared_prices)

        # ── ORB Phase 1 → 2 transition: mark complete at 9:30 AM ────────
        if not orb_obs_complete and now.time() >= time(9, 30):
            orb.mark_observation_complete()
            orb_obs_complete = True

            # --- Addition 1: Opening 15-min volume day classifier ---
            try:
                logger.info(f"[USER {user_id}] Calculating 15-min volume participation...")
                total_expected_15min_vol = 0
                total_actual_15min_vol = 0
                for s in layer2.active_watchlist:
                    avg_vol = s.get("avg_vol_20", 0)
                    if avg_vol > 0:
                        total_expected_15min_vol += avg_vol * (15 / 375)
                        
                        df = bot.engine.get_ohlc(s["instrument_key"], interval="5minute")
                        if not df.empty:
                            df['ist_time'] = df['timestamp'].dt.tz_convert('Asia/Kolkata') if df['timestamp'].dt.tz else df['timestamp'].dt.tz_localize('Asia/Kolkata')
                            mask = (df['ist_time'].dt.time >= time(9, 15)) & (df['ist_time'].dt.time < time(9, 30))
                            actual_vol = df.loc[mask, 'volume'].sum()
                            total_actual_15min_vol += actual_vol
                
                if total_expected_15min_vol > 0:
                    participation_ratio = total_actual_15min_vol / total_expected_15min_vol
                    bot.state.participation_ratio = participation_ratio
                    if participation_ratio < 0.8:
                        bot.state.day_type = "LOW_PARTICIPATION"
                    elif participation_ratio > 2.0:
                        bot.state.day_type = "HIGH_PARTICIPATION"
                    else:
                        bot.state.day_type = "NORMAL"
                    logger.info(f"[USER {user_id}] 15-min Participation Ratio: {participation_ratio:.2f} -> {bot.state.day_type}")
                    
                    _evaluate_market_regime(bot, user_id, layer4, participation_ratio)
                        
            except Exception as e:
                logger.error(f"[USER {user_id}] Error calculating regime/volume: {e}")
                bot.state.day_type = "NORMAL"
                bot.state.market_regime = "RANGING"

        # ── Hourly Regime Re-Classification ──────────────────────────────
        if now.minute == 30 and now.time() >= time(10, 30):
            if getattr(bot.state, 'last_regime_check_hour', None) != now.hour:
                bot.state.last_regime_check_hour = now.hour
                try:
                    _evaluate_market_regime(bot, user_id, layer4, getattr(bot.state, 'participation_ratio', 1.0))
                except Exception as e:
                    logger.error(f"[USER {user_id}] Error in hourly regime check: {e}")


        # ── 15:15 PM — Force exit all positions ──────────────────────────
        force_exit_h, force_exit_m = Config.FORCE_EXIT_ALL_AT
        if now.time() >= time(force_exit_h, force_exit_m):
            if bot.tm.active_trades:
                logger.info(f"[USER {user_id}] 3:15 PM — Force exiting {len(bot.tm.active_trades)} active trades.")
                for trade in list(bot.tm.active_trades):
                    bot.tm.close_trade(trade, "EOD_FORCED_EXIT", execution_engine=bot.engine)
                    alert_trade_exit(trade['symbol'], "EOD FORCED EXIT",
                                     trade['entry_price'], trade['current_price'], trade['pnl'])

        # ── 15:25 PM — WebSocket disconnect ──────────────────────────────
        ws_h, ws_m = Config.WEBSOCKET_DISCONNECT_AT
        if now.time() >= time(ws_h, ws_m) and not ws_disconnected:
            market_feed.stop()
            ws_disconnected = True
            logger.info(f"[USER {user_id}] 3:25 PM — WebSocket feed disconnected.")

        # ── 15:30 PM — Daily summary ─────────────────────────────────────
        if now.time() >= time(15, 30) and not daily_summary_sent:
            try:
                generate_daily_summary(
                    bot.engine,
                    pipeline_stats={
                        "stocks_scanned": len(layer2.active_watchlist),
                        "layer3_signals": layer3.scan_count,
                        "orb_signals": orb.signals_today,
                        "claude_enters": layer5.enter_decisions_today,
                        "claude_calls": layer5.api_calls_today,
                    },
                    user_id=user_id,
                )
                daily_summary_sent = True
                db_log = SessionLocal()
                try:
                    log_system_event(db_log, user_id, "INFO", "DAILY_SUMMARY", "Daily summary sent successfully")
                finally:
                    db_log.close()
            except Exception as e:
                logger.error(f"[USER {user_id}] Daily summary failed: {e}")

            # Transition bot state to STANDBY for the night
            if bot.state.is_running:
                bot.state.is_running = False
                bot.state.status = "STANDBY"
                logger.info(f"[AUTONOMOUS] 15:30 PM IST — Market session closed. System transitioned to STANDBY for the night.")
                with SessionLocal() as db_night:
                    u_cfg = db_night.query(UserConfig).filter(UserConfig.user_id == user_id).first()
                    if u_cfg:
                        u_cfg.is_running = 0
                        db_night.commit()

        # ── 15:35 PM — Reconciliation ────────────────────────────────────
        if now.time() >= time(15, 35) and not reconciliation_done:
            try:
                run_reconciliation(bot.engine, user_id=user_id)
                reconciliation_done = True
                from database import log_system_event
                db_log = SessionLocal()
                try:
                    log_system_event(db_log, user_id, "INFO", "RECONCILIATION", "Reconciliation completed successfully")
                finally:
                    db_log.close()
            except Exception as e:
                logger.error(f"[USER {user_id}] Reconciliation failed: {e}")

        # ── After 15:15 — no more scanning needed ────────────────────────
        if now.time() >= time(force_exit_h, force_exit_m):
            await asyncio.sleep(60)

        # ── Layer 2: Watchlist Refresh (every 10 minutes, 9:20 AM – 3:00 PM) ──
        now_ts_main = _time.time()
        market_open = time(9, 20) <= now.time() < time(15, 0)
        if market_open and (now_ts_main - last_layer2_refresh) >= 600:  # 600 seconds = 10 minutes
            try:
                layer2.refresh()
                last_layer2_refresh = now_ts_main
                alert_watchlist_refreshed(now.strftime("%H:%M"), len(layer2.active_watchlist))
            except Exception as e:
                logger.error(f"[USER {user_id}] Layer 2 refresh failed: {e}")

        # ── ORB Phase 2: Breakout scan (9:30 AM – 11:00 AM, every 60s) ───────
        orb_now_ts = _time.time()
        orb_window_active = time(9, 30) <= now.time() < time(11, 0)
        if (orb_obs_complete and orb_window_active
                and get_global_trading_enabled()
                and not getattr(bot.state, 'trading_halted', False)
                and (orb_now_ts - orb_last_scan) >= 60):
            orb_last_scan = orb_now_ts
            try:
                nifty_chg_pct = layer4.get_nifty_change_pct()
                nifty_trend = layer4.get_nifty_trend()
                active_trade_symbols = [t['symbol'] for t in bot.tm.active_trades]
                vix_ltp = _shared_state.live_indices.get("NSE_INDEX|India VIX", 0)
                orb_signals = orb.scan(
                    layer2.active_watchlist,
                    _shared_prices,
                    active_trade_symbols,
                    bot.engine,
                    nifty_change_pct=nifty_chg_pct
                )
                for orb_sig in orb_signals:
                    # Market Regime Block for ORB
                    if nifty_trend == "bearish" and orb_sig["direction"] == "LONG":
                        logger.info(f"[USER {user_id}] ORB LONG blocked for {orb_sig['symbol']} — market regime is TRENDING_DOWN (bearish)")
                        continue
                    if nifty_trend == "bullish" and orb_sig["direction"] == "SHORT":
                        logger.info(f"[USER {user_id}] ORB SHORT blocked for {orb_sig['symbol']} — market regime is TRENDING_UP (bullish)")
                        continue
                        
                    _add_signal_log(
                        f"[{now.strftime('%H:%M')}] ORB 🔔 {orb_sig['symbol']} "
                        f"{orb_sig['direction']} — Vol={orb_sig['vol_ratio_6bar']:.1f}x | "
                        f"H={orb_sig['orb_high']}/L={orb_sig['orb_low']} | R:R={orb_sig.get('rr_ratio',0):.1f}x"
                    )
                    # Build a minimal Layer 4 payload for Claude to evaluate the ORB setup
                    now_ist = datetime.now(IST)
                    signal_time = now_ist.strftime("%H:%M")
                    tod_h = now_ist.hour
                    if tod_h < 12:
                        time_bucket = "MORNING"
                    elif tod_h < 13 or (tod_h == 13 and now_ist.minute < 30):
                        time_bucket = "MIDDAY"
                    else:
                        time_bucket = "AFTERNOON"

                    target_multiplier = 2.0
                    market_regime = getattr(bot.state, "market_regime", "UNKNOWN")
                    if "TRENDING_UP" in market_regime or "TRENDING_DOWN" in market_regime:
                        target_multiplier = 2.5
                    elif "RANGING" in market_regime or "HIGH_VOLATILITY" in market_regime:
                        target_multiplier = 1.5

                    # Fetch daily candles to get Yesterday's High/Low (Gap 2)
                    prev_day_high = 0.0
                    prev_day_low = 0.0
                    try:
                        symbol_key = orb_sig["instrument_key"]
                        daily_df = bot.engine.get_historical_candles_daily(symbol_key, days=5)
                        if not daily_df.empty:
                            last_date = daily_df.iloc[-1]['timestamp'].date()
                            today_date = datetime.now(IST).date()
                            if last_date == today_date and len(daily_df) >= 2:
                                yesterday_row = daily_df.iloc[-2]
                            else:
                                yesterday_row = daily_df.iloc[-1]
                            prev_day_high = float(yesterday_row['high'])
                            prev_day_low = float(yesterday_row['low'])
                    except Exception as e:
                        logger.error(f"Failed to fetch daily candles for ORB stock {orb_sig['symbol']}: {e}")

                    orb_payload = {
                        "symbol": orb_sig["symbol"],
                        "target_multiplier": target_multiplier,
                        "signal_direction": orb_sig["direction"],
                        "signal_time": signal_time,
                        "time_bucket": time_bucket,
                        "layer3_conditions_met": 5,
                        "price_context": {
                            "ltp": orb_sig["ltp"],
                            # BUG FIX: compute real VWAP % instead of hardcoding 0.0.
                            # Hardcoded 0.0 caused Claude's mandatory VWAP rule to always
                            # fire for SHORT signals (0.0 >= 0 -> SKIP), wasting every API call.
                            "price_vs_vwap_pct": round(
                                (orb_sig["ltp"] - _shared_prices.get(orb_sig["symbol"], {}).get("vwap", 0.0))
                                / _shared_prices.get(orb_sig["symbol"], {}).get("vwap", orb_sig["ltp"])
                                * 100, 3
                            ) if _shared_prices.get(orb_sig["symbol"], {}).get("vwap", 0.0) > 0 else 0.0,
                            "price_vs_ema20_daily_pct": 0.0,
                            "price_vs_20day_high_pct": 0.0,
                            "atr_14_5min": abs(orb_sig["orb_high"] - orb_sig["orb_low"]) / 3,
                            "atr_pct": orb_sig.get("or_range_pct", 0) / 3,
                        },
                        "momentum": {"rsi_14_5min": 60.0, "rsi_14_daily": 55.0,
                                     "ema20_5min_slope": "rising" if orb_sig["direction"] == "LONG" else "falling",
                                     "candle_strength_last": 1.0, "price_slope_10bar": 0},
                        "volume": {"vol_ratio_20day_daily": 1.0,
                                   "vol_ratio_6bar_5min": orb_sig["vol_ratio_6bar"],
                                   "volume_trend": "expanding"},
                        "levels": {
                            "support_20bar_5min": orb_sig["orb_low"],
                            "resistance_20bar_5min": orb_sig["orb_high"],
                            "distance_to_resistance_pct": abs(orb_sig["orb_high"] - orb_sig["ltp"]) / orb_sig["ltp"] * 100,
                            "distance_to_support_pct": abs(orb_sig["ltp"] - orb_sig["orb_low"]) / orb_sig["ltp"] * 100,
                            "previous_day_high": round(prev_day_high, 2) if prev_day_high > 0 else 0.0,
                            "previous_day_low": round(prev_day_low, 2) if prev_day_low > 0 else 0.0,
                            "cleared_previous_day_high": bool(orb_sig["ltp"] > prev_day_high) if prev_day_high > 0 else False,
                            "cleared_previous_day_low": bool(orb_sig["ltp"] < prev_day_low) if prev_day_low > 0 else False,
                        },
                        "market_context": {
                            "nifty_trend_today": layer4.get_nifty_trend(),
                            "bank_nifty_slope": getattr(layer4, '_bank_nifty_trend', {}).get('slope', 'flat'),
                            "bank_nifty_change_3bar_pct": getattr(layer4, '_bank_nifty_trend', {}).get('change_3bar_pct', 0.0),
                            "india_vix": vix_ltp,
                            "sector": "Unknown",
                            "sector_vs_nifty_today_pct": 0.0,
                            "stock_vs_nifty_5day_pct": 0.0,
                        },
                        "recent_candles_5min": [],
                        "orb_meta": {"orb_high": orb_sig["orb_high"], "orb_low": orb_sig["orb_low"],
                                     "or_range_pct": orb_sig.get("or_range_pct", 0),
                                     "entry_price": orb_sig["entry_price"],
                                     "stop_loss": orb_sig["stop_loss"],
                                     "target": orb_sig["target"],
                                     "rr_ratio": orb_sig.get("rr_ratio", 0),
                                     "strategy": "ORB"},
                    }
                    use_ai = bot.config.get("USE_CLAUDE_FILTER", True)

                    # ── Layer 4.5: Python Mandatory Pre-Filter ─────────────────────────
                    # Run deterministic skip checks BEFORE calling Claude.
                    # Signals that fail are rejected instantly in Python, saving API cost.
                    from pipeline_layer4_5 import python_mandatory_filter
                    orb_approved, orb_rejected = python_mandatory_filter([orb_payload])
                    for rej in orb_rejected:
                        logger.info(
                            f"[USER {user_id}] [Pre-Filter] {rej['symbol']} {rej['direction']} "
                            f"SKIPPED before Claude — {rej['reason']}"
                        )
                        _add_signal_log(
                            f"[{now.strftime('%H:%M')}] {rej['symbol']} SKIPPED (Pre-Filter): {rej['reason']}"
                        )

                    if use_ai and orb_approved:
                        orb_decisions = layer5.analyze(
                            orb_approved,
                            nifty_trend=layer4.get_nifty_trend(),
                            nifty_change_pct=nifty_chg_pct,
                            vix_value=vix_ltp,
                        )
                    elif not orb_approved:
                        orb_decisions = []  # Pre-filter rejected all — no Claude call needed
                    else:
                        orb_decisions = [{
                            "symbol": orb_sig["symbol"],
                            "decision": "ENTER",
                            "direction": orb_sig["direction"],
                            "entry_price": orb_sig["entry_price"],
                            "stop_loss": orb_sig["stop_loss"],
                            "target": orb_sig["target"],
                            "confidence": 4,
                            "reasoning": f"ORB breakout H={orb_sig['orb_high']}/L={orb_sig['orb_low']} with {orb_sig['vol_ratio_6bar']:.1f}x volume.",
                            "strategy": "ORB",
                        }]
                    for od in orb_decisions:
                        od["strategy"] = "ORB"
                        if od.get("decision") == "ENTER":
                            await _execute_v2_trade(user_id, bot, od, [orb_sig])
            except Exception as orb_err:
                logger.error(f"[USER {user_id}] ORB breakout scan error: {orb_err}")

        # ── Layer 3-5: Signal scanning (every 5 min, 9:20 AM – 2:30 PM) ──
        now_ts_main = _time.time()
        if now.time() >= time(9, 20) and (now_ts_main - last_l3_scan) >= 300:
            last_l3_scan = now_ts_main
            cutoff_h, cutoff_m = Config.NO_NEW_ENTRIES_AFTER
            
            # Layer 3 runs even after cutoff for awareness, but L4/L5 don't
            vix_ltp = _shared_state.live_indices.get("NSE_INDEX|India VIX", 0)
            active_trade_symbols = [t['symbol'] for t in bot.tm.active_trades]

            # VIX threshold alerts check (Section 8)
            if not hasattr(bot.state, 'vix_alert_state'):
                bot.state.vix_alert_state = 'normal'

            if vix_ltp > 25.0:
                if bot.state.vix_alert_state != 'halted':
                    from telegram_alert import alert_vix_too_high
                    alert_vix_too_high(vix_ltp)
                    bot.state.vix_alert_state = 'halted'
            elif vix_ltp > 18.0:
                if bot.state.vix_alert_state != 'cautious':
                    from telegram_alert import alert_vix_cautious
                    alert_vix_cautious(vix_ltp)
                    bot.state.vix_alert_state = 'cautious'
            else:
                bot.state.vix_alert_state = 'normal'

            try:
                # Pass latest sector performance from Layer 4 into Layer 3
                sector_perf = layer4.get_sector_perf() if hasattr(layer4, 'get_sector_perf') else {}

                signals = layer3.scan(
                    layer2.active_watchlist,
                    active_trade_symbols,
                    _shared_prices,
                    vix_value=vix_ltp,
                    sector_perf=sector_perf,
                    day_type=getattr(bot.state, "day_type", "NORMAL")
                )
                _pipeline_stats["layer3_signals"] += len(signals)

                # Map Layer 3 signals to UI
                if not signals:
                    latest_signals = []
                else:
                    ui_signals = []
                    for s in signals:
                        ui_signals.append({
                            "symbol": s["symbol"],
                            "decision": "AVOID",  # Default watch state at Layer 3
                            "score": int(s["conditions_met"] / 5 * 100),
                            "entry_price": s["ltp"],
                            "current_price": s["ltp"],
                            "metadata": {
                                "breakout_level": s.get("ema_20"),
                                "breakdown_level": s.get("ema_20"),
                                "vwap": s.get("vwap"),
                                "volume_ratio": s.get("vol_ratio_6bar"),
                                "rr_ratio": 2.0,
                                "atr_pct": 1.0,
                                "breakout": bool(s.get("price_vs_vwap", 0) > 0.15 or s.get("price_vs_vwap", 0) < -0.15),
                                "volume_spike": bool(s.get("vol_ratio_6bar", 0) >= 2.0),
                                "strong_candle": bool(s.get("candle_strength", 0) >= 0.50),
                                "vwap_aligned": True
                            }
                        })
                    latest_signals = ui_signals

                # Log signals for dashboard
                for sig in signals:
                    _add_signal_log(f"[{now.strftime('%H:%M')}] {sig['symbol']} {sig['direction']} — {sig['conditions_met']}/5 conditions met")

                # Only proceed to Layer 4+5 before the entry cutoff
                if signals and now.time() < time(cutoff_h, cutoff_m):
                    # Check global halt for new entries
                    if get_global_trading_enabled() and not getattr(bot.state, 'trading_halted', False):
                        # Layer 4: Feature extraction
                        feature_payloads = layer4.extract(
                            signals, _shared_prices, _shared_state.live_indices, 
                            market_regime=getattr(bot.state, "market_regime", "UNKNOWN"),
                            prev_close_cache=_shared_state.index_prev_close
                        )

                        if feature_payloads:
                            # Check if Generative Filter is enabled
                            use_ai = bot.config.get("USE_CLAUDE_FILTER", True)

                            # ── Layer 4.5: Python Mandatory Pre-Filter ────────────────
                            # Reject signals with deterministic mathematical failures
                            # before sending to Claude. Saves API cost and latency.
                            from pipeline_layer4_5 import python_mandatory_filter
                            approved_payloads, rejected_payloads = python_mandatory_filter(feature_payloads)
                            for rej in rejected_payloads:
                                logger.info(
                                    f"[USER {user_id}] [Pre-Filter] {rej['symbol']} {rej['direction']} "
                                    f"SKIPPED before Claude -- {rej['reason']}"
                                )
                                _add_signal_log(
                                    f"[{now.strftime('%H:%M')}] {rej['symbol']} SKIPPED (Pre-Filter): {rej['reason']}"
                                )

                            if use_ai and approved_payloads:
                                # Layer 5: Claude analysis (only approved signals)
                                nifty_trend = layer4.get_nifty_trend()
                                nifty_change = layer4.get_nifty_change_pct()
                                vix_val = layer4.get_vix_value()

                                decisions = layer5.analyze(
                                    approved_payloads,
                                    nifty_trend=nifty_trend,
                                    nifty_change_pct=nifty_change,
                                    vix_value=vix_val,
                                )
                                _pipeline_stats["claude_calls"] = layer5.api_calls_today
                            elif not approved_payloads:
                                decisions = []  # All rejected by pre-filter — no Claude call
                                logger.info(f"[USER {user_id}] All signals rejected by pre-filter — skipping Claude call.")
                            else:
                                logger.info(f"[USER {user_id}] Generative Filter disabled — running local rule analysis (bypassing Claude).")
                                decisions = run_local_rule_analysis(approved_payloads)

                            # Log decisions
                            for d in decisions:
                                color_label = "ENTER" if d.get("decision") == "ENTER" else "SKIP"
                                source_label = "Claude" if use_ai else "Local"
                                _add_signal_log(
                                    f"[{now.strftime('%H:%M')}] {d['symbol']} → {color_label} "
                                    f"({source_label} confidence: {d.get('confidence', 0)}/5) — {d.get('reasoning', '')[:80]}"
                                )

                            # Map decisions to UI
                            ui_decisions = []
                            for d in decisions:
                                payload = next((p for p in feature_payloads if p["symbol"] == d["symbol"]), None)
                                m = {}
                                if payload:
                                    pc = payload.get("price_context", {})
                                    lv = payload.get("levels", {})
                                    ind = payload.get("indicators", {})
                                    m = {
                                        "breakout_level": lv.get("resistance_20bar_5min"),
                                        "breakdown_level": lv.get("support_20bar_5min"),
                                        "vwap": pc.get("vwap_5min"),
                                        "volume_ratio": payload.get("volume", {}).get("vol_ratio_6bar"),
                                        "rr_ratio": 2.0,
                                        "atr_pct": round(float(pc.get("atr_pct_5min", 0)), 2),
                                        "breakout": bool(ind.get("breakout_detected", False)),
                                        "volume_spike": bool(ind.get("volume_spike", False)),
                                        "strong_candle": bool(ind.get("momentum_candle", False)),
                                        "vwap_aligned": bool(ind.get("vwap_aligned", False))
                                    }
                                ui_decisions.append({
                                    "symbol": d["symbol"],
                                    "decision": "BUY" if d.get("decision") == "ENTER" and d.get("direction") == "LONG" else "SELL" if d.get("decision") == "ENTER" and d.get("direction") == "SHORT" else "AVOID",
                                    "score": int(d.get("confidence", 0) * 20),
                                    "entry_price": d.get("entry_price", 0.0),
                                    "current_price": _shared_prices.get(d["symbol"], 0.0),
                                    "metadata": m
                                })
                            latest_signals = ui_decisions

                            # Process ENTER decisions
                            enter_decisions = layer5.get_enter_decisions(decisions)
                            if use_ai:
                                _pipeline_stats["claude_enters"] += len(enter_decisions)

                            for decision in enter_decisions:
                                await _execute_v2_trade(user_id, bot, decision, signals)
                    else:
                        logger.info(f"[USER {user_id}] Global halt active — skipping Layer 4/5.")

            except Exception as e:
                logger.error(f"[USER {user_id}] Pipeline scan error: {e}")

        # Update bot state
        bot.state.daily_pnl    = bot.tm.get_daily_pnl()
        bot.state.trades_taken = bot.tm.get_trades_taken_today()

        daily_pnl = bot.tm.get_daily_pnl()
        if daily_pnl <= -abs(Config.DAILY_LOSS_LIMIT):
            logger.critical(f"[USER {user_id}] KILL SWITCH! Daily Loss ={daily_pnl:.2f}")
            bot.state.kill_switch_active = True
            bot.state.is_running = False
            if not bot.engine.is_paper:
                bot.engine.exit_all_positions()
            for trade in list(bot.tm.active_trades):
                bot.tm.close_trade(trade, "KILL SWITCH", execution_engine=bot.engine)

        # Sleep 60 seconds — supports 1-min ORB checks during morning window.
        # Main Layer 3-5 scan (every 5 min) is controlled by a separate timestamp tracker below.
        await asyncio.sleep(60)


def _add_signal_log(entry: str):
    """Add an entry to the signal log (last 50)."""
    global _signal_log
    _signal_log.append({"time": datetime.now(IST).isoformat(), "message": entry})
    if len(_signal_log) > 50:
        _signal_log = _signal_log[-50:]


async def _execute_v2_trade(user_id: int, bot, decision: dict, layer3_signals: list):
    """
    Execute a trade based on Claude's ENTER decision (Section 4).
    """
    symbol = decision.get("symbol", "")
    direction = decision.get("direction", "LONG")
    entry_price = float(decision.get("entry_price", 0))
    stop_loss = float(decision.get("stop_loss") or 0.0)
    target = float(decision.get("target") or 0.0)
    confidence = int(decision.get("confidence", 0))
    reasoning = decision.get("reasoning", "")

    if entry_price <= 0.0:
        logger.warning(f"[USER {user_id}] Invalid entry price {entry_price} for {symbol}. Skipping.")
        return

    # Map direction to side
    side = "BUY" if direction == "LONG" else "SELL"
    import instrument_resolver
    symbol_key = instrument_resolver.get_instrument_key(symbol)
    if not symbol_key:
        logger.warning(f"[USER {user_id}] Unknown symbol {symbol} in ENTER decision. Skipping.")
        return

    # Step 1 — Freshness check
    if not market_feed.is_tick_fresh(symbol, max_age=30.0):
        logger.warning(f"[USER {user_id}] Stale feed for {symbol} — skipping trade.")
        return

    # Step 2 — Price proximity check (Momentum: 0.3%, ORB: 0.5%)
    ltp = _shared_prices.get(symbol, 0)
    if ltp <= 0:
        return
    slippage_pct = abs(ltp - entry_price) / entry_price * 100
    strat_type = decision.get("strategy", "MOMENTUM")
    max_slippage = Config.MAX_ORB_SLIPPAGE_PCT if strat_type == "ORB" else Config.MAX_MOMENTUM_SLIPPAGE_PCT
    if slippage_pct > max_slippage:
        logger.warning(f"[USER {user_id}] Price moved too far for {symbol} ({strat_type}): "
                       f"LTP={ltp}, entry={entry_price}, slippage={slippage_pct:.2f}% (max {max_slippage}%). Skipping.")
        return

    # Step 3 — Daily limit check
    max_trades = bot.config.get('MAX_TRADES_PER_DAY', Config.MAX_TRADES_PER_DAY)
    trades_today = get_trades_count_today(user_id)
    if trades_today >= max_trades:
        logger.warning(f"[USER {user_id}] Daily trade limit reached ({trades_today}/{max_trades}). Skipping.")
        alert_daily_limit_reached(user_id, max_trades)
        return

    # Step 3.5 — Concurrent open trades check (Dynamic per user config)
    max_open = bot.config.get('MAX_OPEN_TRADES', Config.MAX_OPEN_TRADES)
    active_trades_count = len(bot.tm.active_trades)
    if active_trades_count >= max_open:
        logger.warning(f"[USER {user_id}] Max open trades limit reached ({active_trades_count}/{max_open}). Skipping trade for {symbol}.")
        return

    # Step 3.6 — Sector concentration check (Fix 10)
    from nifty500_instruments import get_sector
    new_sector = get_sector(symbol)
    if new_sector and new_sector != "Unknown" and new_sector != "Others":
        for active in bot.tm.active_trades:
            active_sector = get_sector(active['symbol'])
            if active_sector == new_sector:
                logger.warning(
                    f"[USER {user_id}] Sector concentration block: "
                    f"skipping {symbol} because we already have an active trade in '{new_sector}' ({active['symbol']})"
                )
                send_alert(user_id, f"⚠️ Sector concentration block: skipping {symbol} — already trading '{new_sector}' via {active['symbol']}.")
                return

    # Step 4 — Quantity calculation (Risk-based with Capital Cap)
    daily_loss_limit = bot.config.get('DAILY_LOSS_LIMIT', Config.DAILY_LOSS_LIMIT)
    risk_per_trade = daily_loss_limit / max(max_trades, 1)
    sl_distance = abs(entry_price - stop_loss)
    
    if sl_distance <= 0.0:
        logger.warning(f"[USER {user_id}] Invalid SL distance {sl_distance} for {symbol}. Skipping.")
        return
        
    quantity = math.floor(risk_per_trade / sl_distance)
    logger.info(
        f"[USER {user_id}] Risk_Per_Trade calc: daily_loss_limit=Rs{daily_loss_limit:.0f}, "
        f"max_trades={max_trades}, risk_per_trade=Rs{risk_per_trade:.0f}, "
        f"entry={entry_price:.2f}, sl={stop_loss:.2f}, sl_dist={sl_distance:.2f} -> qty={quantity} for {symbol}"
    )
    if quantity <= 0:
        logger.warning(f"[USER {user_id}] Quantity rounds to 0 for {symbol}. Skipping.")
        return

    # Step 4.1 — Capital Cap (with 5x Intraday Leverage): ensure trade value stays within configured capital per slot.
    # A very tight SL (small sl_distance) can inflate qty to absurd levels (e.g. YESBANK 5000 shares
    # at ₹22.89 = ₹1.14L when manual capital is only ₹10k). Cap it hard.
    # Since this is an intraday bot with 5x leverage, the buying power per slot is:
    # (Capital / Max Open Trades) * 5
    manual_capital = bot.config.get('MANUAL_CAPITAL', Config.MANUAL_CAPITAL)
    max_open = bot.config.get('MAX_OPEN_TRADES', Config.MAX_OPEN_TRADES)
    capital_per_trade = manual_capital / max(max_open, 1)
    
    # 5x intraday leverage multiplier
    leverage_multiplier = 5.0
    leveraged_capital_per_trade = capital_per_trade * leverage_multiplier
    
    max_qty_by_capital = math.floor(leveraged_capital_per_trade / entry_price)
    if max_qty_by_capital <= 0:
        logger.warning(
            f"[USER {user_id}] Leveraged Capital per trade (Rs{leveraged_capital_per_trade:.0f}) insufficient for "
            f"{symbol} at Rs{entry_price:.2f}. Skipping."
        )
        return
    if quantity > max_qty_by_capital:
        logger.info(
            f"[USER {user_id}] Qty capped by leveraged capital: {quantity} -> {max_qty_by_capital} "
            f"(leveraged_capital_per_trade=Rs{leveraged_capital_per_trade:.0f}, entry=Rs{entry_price:.2f}) for {symbol}"
        )
        quantity = max_qty_by_capital

    # Step 4.5 — IMPROVEMENT 4: VIX-based position sizing
    # Scale down position size as volatility rises to protect capital automatically.
    vix_now = _shared_state.live_indices.get("NSE_INDEX|India VIX", 0)
    if vix_now >= 25.0:
        logger.warning(f"[USER {user_id}] VIX={vix_now:.1f} >= 25 — blocking trade for {symbol}.")
        return
    elif vix_now >= 20.0:
        vix_multiplier = 0.4
    elif vix_now >= 16.0:
        vix_multiplier = 0.6
    elif vix_now >= 13.0:
        vix_multiplier = 0.8
    else:
        vix_multiplier = 1.0

    if vix_multiplier < 1.0:
        adjusted_qty = math.floor(quantity * vix_multiplier)
        if adjusted_qty <= 0:
            logger.warning(f"[USER {user_id}] VIX={vix_now:.1f} reduced qty to 0 for {symbol}. Skipping.")
            return
        logger.info(f"[USER {user_id}] VIX={vix_now:.1f} → position sized to {vix_multiplier:.0%} "
                    f"({quantity} → {adjusted_qty} shares) for {symbol}.")
        quantity = adjusted_qty

    # Log order value — Upstox handles margin/leverage validation, we do NOT block here.
    # Upstox will reject if margin is truly insufficient; we must always attempt placement.
    order_value = quantity * entry_price
    logger.info(
        f"[USER {user_id}] Order value for {symbol}: Rs{order_value:.2f} "
        f"(leveraged intraday). Sending to Upstox for margin validation."
    )

    # Find layer3 signal for this symbol (for conditions_met)
    l3_signal = next((s for s in layer3_signals if s["symbol"] == symbol), {})
    conditions_met = l3_signal.get("conditions_met", 0)

    # Validate intraday support and 5x leverage availability prior to trading (real, hybrid, or paper)
    # Using 4.5 threshold to allow for minor rounding/charges discrepancies
    leverage_res = bot.engine.check_intraday_leverage(symbol_key, entry_price, quantity)
    if not leverage_res.get("intraday_supported", False) or leverage_res.get("leverage", 0.0) < 4.5:
        reason = leverage_res.get("reason", "Intraday not supported or leverage < 5x")
        logger.warning(
            f"[USER {user_id}] Skipping trade for {symbol}: 5x leverage/intraday not supported. "
            f"Leverage: {leverage_res.get('leverage', 0.0):.2f}x, Intraday Supported: {leverage_res.get('intraday_supported')}. Detail: {reason}"
        )
        send_alert(
            user_id,
            f"⚠️ Skipping trade for {symbol} — 5x leverage/intraday not supported (Leverage: {leverage_res.get('leverage', 0.0):.1f}x)."
        )
        return

    # Step 5 — Place entry order (or simulate in paper mode)
    is_paper = bot.engine.is_paper
    if is_paper:
        logger.info(f"[USER {user_id}] PAPER TRADE WOULD ENTER: {direction} {symbol} @ ₹{ltp:.2f}")
        order_id = f"paper_{symbol}_{side}"
        filled_qty = quantity
        actual_entry = ltp
    else:
        res = bot.engine.place_order_with_retry(
            symbol_key, side, quantity,
            order_type='LIMIT', price=round(entry_price, 2)
        )
        
        is_virtual = False
        if res.get('status') == 'unknown':
            # Bug Fix 1: Order status unknown
            alert_order_status_unknown(symbol, res.get('order_id', 'N/A'))
            add_to_daily_blacklist(user_id, symbol)
            return
            
        # Check if the order was rejected synchronously or filled_qty is 0 (asynchronous rejection/insufficient funds)
        is_rejected_sync = res.get('status') == 'rejected'
        is_rejected_async = (res.get('status') == 'success' and res.get('filled_quantity', 0) <= 0)
        
        if is_rejected_sync or is_rejected_async:
            user_config = bot.config
            if user_config.get('HYBRID_VIRTUAL_MODE', True):
                reject_reason = res.get('reason', 'Exchange Rejected') if is_rejected_sync else 'Insufficient Margin/Exchange Rejected'
                logger.warning(f"[USER {user_id}] Broker/Exchange REJECTED order for {symbol} ({reject_reason}). Falling back to Hybrid Virtual Trading.")
                send_alert(user_id, f"⚠️ Broker rejected {side} order for {symbol} ({reject_reason}). Continuing trade VIRTUALLY.")
                is_virtual = True
                import uuid
                order_id = f"virtual_{symbol}_{side}_{str(uuid.uuid4())[:6]}"
                filled_qty = quantity
                actual_entry = entry_price
            else:
                send_alert(user_id, f"Entry order REJECTED by broker for {symbol} ({side}). Aborting trade.")
                return
        elif res.get('status') != 'success':
            send_alert(user_id, f"Entry order placement failed for {symbol} ({side}).")
            return
        else:
            order_id = res.get('order_id')
            filled_qty = res.get('filled_quantity', quantity)
            actual_entry = res.get('average_price', entry_price) or entry_price

        # Bug Fix 2: Check filled quantity
        if filled_qty <= 0:
            logger.error(f"[USER {user_id}] Filled quantity is 0 for {symbol}. Aborting SL placement.")
            send_alert(user_id, f"Entry order filled quantity is 0 for {symbol}. No SL placed.")
            return
 
    # Step 6 — Place SL order
    sl_order_id = None
    if not is_paper:
        if is_virtual:
            # BUG FIX: Virtual trades must NEVER contact Upstox for SL placement.
            # Placing an SL on Upstox when there's no real position causes Upstox
            # to return a rejected-but-real order ID, which then corrupts the
            # is_virtual_sl check in close_trade() and triggers a real square-off
            # order at 3:15 PM. Always use a clearly-named virtual SL ID instead.
            import uuid
            sl_order_id = f"virtual_sl_{symbol}_{str(uuid.uuid4())[:6]}"
            logger.info(f"[USER {user_id}] Virtual trade — skipping Upstox SL placement. Assigned virtual SL ID: {sl_order_id}")
        else:
            sl_res = bot.engine.place_stop_loss(symbol_key, side, filled_qty, stop_loss)
            if sl_res.get('status') != 'success':
                send_alert(user_id, f"SL order placement failed for {symbol}. Emergency square-off!")
                counter_side = "SELL" if side == "BUY" else "BUY"
                exit_res = bot.engine.place_order_with_retry(
                    symbol_key, counter_side, filled_qty,
                    order_type='MARKET'
                )
                if exit_res.get('status') != 'success':
                    send_alert(user_id, f"FATAL: Emergency square-off failed for {symbol}! Manual intervention required!")
                return
            else:
                sl_order_id = sl_res.get('order_id')
 
    # Step 7 — Persist to database
    bot.tm.add_trade(
        symbol=symbol, side=side,
        entry_price=actual_entry if not is_paper else ltp,
        qty=filled_qty,
        sl=stop_loss, target=target,
        probability=0.0, score=confidence,
        sl_order_id=sl_order_id,
        metadata={
            'is_virtual': is_virtual if not is_paper else False,
            'confidence': confidence,
            'reasoning': reasoning,
            'layer3_conditions_met': conditions_met,
            'paper_trade': is_paper,
            'atr_14': l3_signal.get('atr_14', 0),
            'market_condition': 'TRENDING',
            'discovery_score': decision.get('discovery_score'),
            'discovery_rank': decision.get('discovery_rank')
        }
    )
    bot.tm.active_trades[-1]['instrument_token'] = symbol_key
 
    # Update trade record with v2 + analytics columns
    db = SessionLocal()
    try:
        db_trade = db.query(Trade).filter(Trade.id == bot.tm.active_trades[-1]['id']).first()
        if db_trade:
            db_trade.confidence           = confidence
            db_trade.reasoning            = reasoning[:500] if reasoning else None
            db_trade.layer3_conditions_met = conditions_met
            db_trade.paper_trade          = is_paper

            # ── Strategy analytics columns ─────────────────────────────────
            now_ist       = get_ist_now()
            tod_h         = now_ist.hour
            if tod_h < 12:
                tod_bucket = "MORNING"
            elif tod_h < 13 or (tod_h == 13 and now_ist.minute < 30):
                tod_bucket = "MIDDAY"
            else:
                tod_bucket = "AFTERNOON"

            strategy_tag = decision.get("strategy", "MOMENTUM")

            risk_val   = abs(entry_price - stop_loss) if stop_loss else 0
            reward_val = abs(target - entry_price) if target else 0
            rr         = round(reward_val / risk_val, 2) if risk_val > 0 else None

            db_trade.strategy_type      = strategy_tag
            db_trade.rr_ratio           = rr
            db_trade.timeframe_aligned  = l3_signal.get("timeframe_aligned", None)
            db_trade.sector_aligned     = l3_signal.get("sector_aligned", None)
            db_trade.vix_at_entry       = round(vix_now, 1) if vix_now else None
            db_trade.time_of_day_bucket = tod_bucket
            db_trade.market_regime      = getattr(bot.state, 'market_regime', 'UNKNOWN')

            # ── Check if Python rules would have entered without Claude ──
            would_enter_no_claude = False
            try:
                if strategy_tag == "ORB":
                    # ORB signals are already fully validated by Python rules before reaching this step
                    would_enter_no_claude = True
                else:
                    from pipeline_layer4 import FeatureExtractor
                    fe = FeatureExtractor(bot.engine)
                    # Reconstruct the feature payload for the signal
                    payloads = fe.extract([l3_signal], _shared_prices, _shared_state.live_indices)
                    if payloads:
                        local_decisions = run_local_rule_analysis(payloads)
                        if local_decisions and local_decisions[0].get("decision") == "ENTER":
                            would_enter_no_claude = True
            except Exception as ex:
                logger.error(f"Error computing would_have_entered_without_claude: {ex}")

            db_trade.would_have_entered_without_claude = would_enter_no_claude
            db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Error updating v2 trade columns: {e}")
    finally:
        db.close()

 
    # Step 8 — Add to daily blacklist
    add_to_daily_blacklist(user_id, symbol)
 
    # Step 9 — Send Telegram alert
    alert_trade_entry(
        direction, symbol,
        actual_entry if not is_paper else ltp,
        stop_loss, target, filled_qty, confidence, reasoning
    )
 
    logger.info(f"[USER {user_id}] {'PAPER ' if is_paper else ''}Trade placed: "
                f"{direction} {symbol} x{filled_qty} @ ₹{actual_entry if not is_paper else ltp:.2f}")


# -- Per-user running tasks map: user_id -> asyncio.Task ----------------------
_user_tasks: Dict[int, asyncio.Task] = {}


async def ensure_user_bot_task(user_id: int, db):
    """Ensure a trading cycle task is running for the given user."""
    if user_id not in _user_tasks or _user_tasks[user_id].done():
        get_or_create_bot(user_id, db)
        _user_tasks[user_id] = asyncio.create_task(user_trading_cycle(user_id))
        logger.info(f"[USER {user_id}] Trading task started.")


async def bot_supervisor_watchdog():
    """Watchdog loop that verifies all configured users' bot tasks stay alive, restarting if crashed."""
    while True:
        try:
            await asyncio.sleep(30)
            from database import SessionLocal, UserCredential
            with SessionLocal() as db_watch:
                all_creds = db_watch.query(UserCredential).all()
                for c in all_creds:
                    uid = c.user_id
                    if uid not in _user_tasks or _user_tasks[uid].done():
                        if uid in _user_tasks and _user_tasks[uid].done():
                            try:
                                exc = _user_tasks[uid].exception()
                                if exc:
                                    logger.error(f"[Watchdog] Bot task for user_id={uid} stopped with: {exc}")
                            except Exception:
                                pass
                        logger.warning(f"[Watchdog] Resurrecting bot task for user_id={uid}...")
                        await ensure_user_bot_task(uid, db_watch)
        except Exception as e:
            logger.error(f"[Watchdog] Supervisor error: {e}")


# ────────────────────────────────────────────────────────────────────────────
# AUTH ROUTES
# ────────────────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str
    remember: bool = False

class RegisterRequest(BaseModel):
    email: str
    username: str
    password: str

class CredentialUpdateRequest(BaseModel):
    upstox_client_id:     Optional[str] = None
    upstox_client_secret: Optional[str] = None
    upstox_access_token:  Optional[str] = None
    analytics_token:      Optional[str] = None

@app.get("/login")
def get_login_page():
    with open("frontend/login.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/register")
def get_register_page():
    with open("frontend/register.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/credentials")
def get_credentials_page():
    with open("frontend/credentials.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.post("/auth/register")
async def register(body: RegisterRequest, db=Depends(get_db)):
    """Create a new user account."""
    import re
    email = body.email.strip().lower()[:254]
    username = re.sub(r'[^a-zA-Z0-9_.-]', '', body.username.strip())[:80]
    password = body.password

    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    if not re.match(r'^[^@]+@[^@]+\.[^@]+$', email):
        raise HTTPException(status_code=400, detail="Invalid email address.")
    if not username:
        raise HTTPException(status_code=400, detail="Invalid username.")

    try:
        user = create_user(db, email=email, username=username, password=password)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    access_token   = create_access_token(user.id, user.email, user.role)
    refresh_token  = create_refresh_token(user.id, user.email)
    exp_seconds    = ACCESS_TOKEN_EXP * 60

    response = JSONResponse({
        "access_token": access_token,
        "token_type":   "bearer",
        "expires_in":   exp_seconds,
        "username":     user.username,
        "email":        user.email,
        "role":         user.role,
    })
    _set_auth_cookies(response, access_token, refresh_token)
    return response

@app.post("/auth/login")
async def post_login(request: Request, body: LoginRequest, db=Depends(get_db)):
    """Authenticate user, return JWT tokens."""
    client_ip = request.client.host
    limited, retry_after = is_rate_limited(client_ip)
    if limited:
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many login attempts.", "retry_after": retry_after},
            headers={"Retry-After": str(retry_after)}
        )

    username = body.username.strip()[:120]
    password = body.password[:128]

    user = authenticate_user(db, username, password)
    if not user:
        record_login_attempt(client_ip)
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    if getattr(user, "is_active", True) is False:
        raise HTTPException(status_code=403, detail="Your account has been deactivated. Please contact the Super Admin.")

    try:
        user.last_login = get_ist_now()
        db.commit()
    except Exception:
        db.rollback()

    clear_login_attempts(client_ip)
    access_token  = create_access_token(user.id, user.email, user.role)
    refresh_token = create_refresh_token(user.id, user.email)
    exp_seconds   = ACCESS_TOKEN_EXP * 60

    # Ensure the user's bot task is running
    await ensure_user_bot_task(user.id, db)

    from database import log_system_event
    log_system_event(db, user.id, "INFO", "LOGIN", f"User {user.username} logged in from {client_ip}")

    response = JSONResponse({
        "access_token": access_token,
        "token_type":   "bearer",
        "expires_in":   exp_seconds,
        "username":     user.username,
        "email":        user.email,
        "role":         user.role,
    })
    _set_auth_cookies(response, access_token, refresh_token)
    return response

# Keep old /login POST for backwards compat
@app.post("/login")
async def post_login_legacy(request: Request, body: LoginRequest, db=Depends(get_db)):
    return await post_login(request, body, db)

def _set_auth_cookies(response: JSONResponse, access_token: str, refresh_token: str):
    is_https = os.getenv("HTTPS", "false").lower() == "true"
    opts = dict(httponly=True, samesite="lax", secure=is_https)
    response.set_cookie("access_token",  access_token,  max_age=ACCESS_TOKEN_EXP * 60,     **opts)
    response.set_cookie("refresh_token", refresh_token, max_age=REFRESH_TOKEN_EXP * 86400, **opts)

@app.post("/logout")
async def logout(request: Request, token: str = Depends(get_token_from_request)):
    if token:
        revoke_access_token(token)
    refresh = request.cookies.get("refresh_token")
    if refresh:
        revoke_refresh_token(refresh)
    response = JSONResponse({"status": "logged_out"})
    response.delete_cookie("access_token")
    response.delete_cookie("refresh_token")
    return response

@app.post("/refresh")
async def refresh_token_endpoint(request: Request, db=Depends(get_db)):
    refresh = request.cookies.get("refresh_token")
    if not refresh:
        raise HTTPException(status_code=401, detail="No refresh token.")
    payload = verify_refresh_token(refresh)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token.")

    user_id = payload.get("uid")
    user = get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found.")

    new_access  = create_access_token(user.id, user.email, user.role)
    new_refresh = create_refresh_token(user.id, user.email)
    revoke_refresh_token(refresh)

    response = JSONResponse({"access_token": new_access, "token_type": "bearer"})
    _set_auth_cookies(response, new_access, new_refresh)
    return response

@app.get("/me")
async def get_me(user: dict = Depends(require_auth), db=Depends(get_db)):
    db_user = get_user_by_id(db, user["uid"])
    if not db_user:
        raise HTTPException(status_code=404, detail="User not found.")
    from credential_service import credential_field_status
    cred_status = credential_field_status(db, db_user.id)
    return {
        "username":    db_user.username,
        "email":       db_user.email,
        "role":        db_user.role,
        "credentials": cred_status,
    }

# ────────────────────────────────────────────────────────────────────────────
# CREDENTIALS
# ────────────────────────────────────────────────────────────────────────────

@app.post("/credentials")
async def upsert_user_credentials(
    body: CredentialUpdateRequest,
    user: dict = Depends(require_auth),
    db=Depends(get_db)
):
    """
    Write-only credential endpoint.
    - Accepts partial updates (blank fields are ignored)
    - Values are AES-256 encrypted before DB storage
    - NEVER returns stored values in any response
    """
    user_id = user["uid"]
    try:
        upsert_credentials(
            db, user_id,
            upstox_client_id=body.upstox_client_id,
            upstox_client_secret=body.upstox_client_secret,
            upstox_access_token=body.upstox_access_token,
            analytics_token=body.analytics_token,
        )
        # Reload the engine with new credentials (hot-reload)
        reload_bot_credentials(user_id, db)
        await ensure_user_bot_task(user_id, db)

        # ALSO update the shared components (shared engine & market feed WebSocket)
        from credential_service import get_decrypted_credentials
        fresh_creds = get_decrypted_credentials(db, user_id)
        if fresh_creds.get("upstox_access_token"):
            logger.info(f"Hot-reloading shared engine and market feed with updated credentials from user {user_id}")
            _shared_engine._creds = fresh_creds
            
            # Restart market feed with new credentials
            market_feed.stop()
            # Wait a brief moment for it to stop cleanly
            await asyncio.sleep(1)
            market_feed.start(
                _shared_prices, 
                _shared_state.live_indices, 
                _shared_state.index_prev_close, 
                None, 
                _shared_timestamps,
                credentials=fresh_creds
            )

        from database import log_system_event
        log_system_event(db, user_id, "INFO", "CRED_UPDATE", "User updated trading credentials")

        return {
            "status":  "saved",
            "message": "Credentials saved securely. Fields updated: " + ", ".join(
                f for f, v in {
                    "client_id":     body.upstox_client_id,
                    "client_secret": body.upstox_client_secret,
                    "access_token":  body.upstox_access_token,
                    "analytics":     body.analytics_token,
                }.items() if v and v.strip()
            ) or "none",
            "credentials": credential_field_status(db, user_id),
        }
    except Exception as e:
        logger.error(f"Credential save failed for user {user_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to save credentials.")

@app.get("/credentials/status")
async def get_credential_status(
    user: dict = Depends(require_auth),
    db=Depends(get_db)
):
    """Returns which fields are set (True/False only). NEVER returns values."""
    return credential_field_status(db, user["uid"])


# ────────────────────────────────────────────────────────────────────────────
# DASHBOARD ROUTES (auth-protected)
# ────────────────────────────────────────────────────────────────────────────

@app.get("/")
def get_dashboard(request: Request, token: Optional[str] = Depends(get_token_from_request)):
    if not token or not verify_access_token(token):
        return RedirectResponse("/login", status_code=302)
    with open("frontend/index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/dual")
def get_dual(request: Request, token: Optional[str] = Depends(get_token_from_request)):
    if not token or not verify_access_token(token):
        return RedirectResponse("/login", status_code=302)
    with open("frontend/dual.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/dual-demo")
def get_dual_demo(request: Request, token: Optional[str] = Depends(get_token_from_request)):
    if not token or not verify_access_token(token):
        return RedirectResponse("/login", status_code=302)
    with open("frontend/dual_demo.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/super-admin")
def get_super_admin_screen(request: Request, token: Optional[str] = Depends(get_token_from_request)):
    if not token:
        return RedirectResponse("/login", status_code=302)
    payload = verify_access_token(token)
    if not payload or payload.get("role") != "super_admin":
        return RedirectResponse("/", status_code=302)
    with open("frontend/super_admin.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


# ────────────────────────────────────────────────────────────────────────────
# API ROUTES (per-user isolated)
# ────────────────────────────────────────────────────────────────────────────

def sanitize_json(obj):
    if isinstance(obj, dict):
        return {str(k): sanitize_json(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple, set)):
        return [sanitize_json(v) for v in obj]
    elif hasattr(obj, 'item'):
        return obj.item()
    elif isinstance(obj, (bool, int, float, str)) or obj is None:
        return obj
    elif hasattr(obj, 'isoformat'):
        return obj.isoformat()
    return str(obj)

@app.get("/api/signals")
def api_get_signals(user: dict = Depends(require_auth)):
    from fastapi.responses import JSONResponse
    return JSONResponse(content=sanitize_json(latest_signals))

@app.get("/api/live-prices")
def api_get_live_prices(user: dict = Depends(require_auth)):
    return _shared_prices

@app.get("/api/trades/active")
def api_get_active_trades(user: dict = Depends(require_auth)):
    uid = user["uid"]
    bot = get_bot(uid)
    if not bot:
        return []
    
    # Dynamically update prices from shared live prices cache for fast frontend updates
    trades = []
    for t in bot.tm.active_trades:
        trade_copy = dict(t)
        symbol = t["symbol"]
        ltp = _shared_prices.get(symbol, t.get("current_price", t["entry_price"]))
        if ltp == t.get("current_price", t["entry_price"]):
            sym_key = Config.SYMBOLS_MAPPING.get(symbol)
            if sym_key and sym_key in _shared_prices:
                ltp = _shared_prices[sym_key]
        
        trade_copy["current_price"] = round(ltp, 2)
        qty = t["qty"]
        entry = t["entry_price"]
        side = t["side"]
        if side == "BUY":
            pnl = (ltp - entry) * qty
        else:
            pnl = (entry - ltp) * qty
        trade_copy["pnl"] = round(pnl, 2)
        trades.append(trade_copy)
        
    return trades

@app.get("/api/trades/history")
def api_get_trade_history(user: dict = Depends(require_auth), db=Depends(get_db)):
    uid = user["uid"]
    
    # Get all unique trade dates for user, sorted descending
    all_trade_dates = db.query(Trade.trade_date).filter(Trade.user_id == uid).distinct().order_by(Trade.trade_date.desc()).all()
    trade_dates = [d[0] for d in all_trade_dates if d[0] is not None]
    
    if not trade_dates:
        return []
        
    latest_trade_date = trade_dates[0]
    
    now = get_ist_now()
    today = now.date()
    
    # Logic: Show latest_trade_date ONLY IF:
    # 1. Trade happened today
    # 2. OR Trade happened yesterday (or Friday if today is Monday), AND it is before 9:00 AM.
    # Otherwise, show an empty history for today.
    
    show_today_empty = True
    days_diff = (today - latest_trade_date).days
    
    if today == latest_trade_date:
        show_today_empty = False
    elif days_diff == 1 and now.time() < time(9, 0):
        show_today_empty = False
    elif days_diff <= 3 and now.weekday() == 0 and latest_trade_date.weekday() == 4 and now.time() < time(9, 0):
        # Today is Monday, last trade was Friday, and it's before 9:00 AM
        show_today_empty = False
            
    if show_today_empty:
        db_trades = db.query(Trade).filter(Trade.user_id == uid, Trade.trade_date == today).all()
    else:
        db_trades = db.query(Trade).filter(Trade.user_id == uid, Trade.trade_date == latest_trade_date).all()
        
    result = []
    for t in db_trades:
        result.append({
            'id': t.id,
            'timestamp': to_ist_iso(t.entry_time) if t.entry_time else now.isoformat(),
            'symbol': t.symbol,
            'side': t.side,
            'entry_price': t.entry_price,
            'exit_price': t.exit_price or 0.0,
            'current_price': t.exit_price or t.entry_price,
            'qty': t.quantity,
            'sl': t.sl_price,
            'initial_sl': t.sl_price,
            'probability': 0.0,
            'score': t.ai_score or 0.0,
            'pnl': t.realized_pnl or 0.0,
            'status': t.status,
            'exit_reason': t.exit_reason,
            'exit_time': to_ist_iso(t.exit_time) if t.exit_time else None,
            'sl_order_id': t.sl_order_id
        })
        
    return sorted(result, key=lambda x: x['timestamp'])

@app.get("/api/trades/all-history")
def api_get_all_history(
    start_date: str = None,
    end_date: str = None,
    user: dict = Depends(require_auth),
    db=Depends(get_db)
):
    uid = user["uid"]
    query = db.query(Trade).filter(Trade.user_id == uid)
    if start_date:
        query = query.filter(Trade.trade_date >= start_date)
    if end_date:
        query = query.filter(Trade.trade_date <= end_date)
    trades = query.order_by(Trade.entry_time).all()
    result = []
    for t in trades:
        result.append({
            'id': t.id, 'date': t.trade_date.isoformat() if t.trade_date else None,
            'entry_time': to_ist_iso(t.entry_time) if t.entry_time else None,
            'exit_time':  to_ist_iso(t.exit_time)  if t.exit_time  else None,
            'symbol': t.symbol, 'side': t.side, 'qty': t.quantity or 1,
            'entry_price': t.entry_price, 'exit_price': t.exit_price,
            'sl': t.sl_price, 'pnl': t.realized_pnl or 0.0,
            'exit_reason': t.exit_reason,
            'score': t.ai_score or 0,
            'status': t.status if t.status == 'OPEN' else (
                'Win' if (t.realized_pnl or 0) > 0 else 'Loss' if (t.realized_pnl or 0) < 0 else 'Breakeven'
            )
        })
    return result

@app.get("/api/trades/strategy-performance")
def api_get_strategy_performance(user: dict = Depends(require_auth), db=Depends(get_db)):
    uid = user["uid"]
    # Get all closed trades
    trades = db.query(Trade).filter(Trade.user_id == uid, Trade.status != 'OPEN').all()
    
    # helper lists
    momentum_trades = []
    orb_trades = []
    
    time_buckets = {"MORNING": [], "MIDDAY": [], "AFTERNOON": []}
    vix_buckets = {"lt_13": [], "13_16": [], "16_20": []}
    
    for t in trades:
        strat = (t.strategy_type or "MOMENTUM").upper()
        pnl = t.realized_pnl or 0.0
        is_win = pnl > 0
        
        # Strategy grouping
        if strat == "ORB":
            orb_trades.append(t)
        else:
            momentum_trades.append(t)
            
        # Time bucket grouping
        tb = (t.time_of_day_bucket or "").upper()
        if tb in time_buckets:
            time_buckets[tb].append(is_win)
        else:
            if t.entry_time:
                tod_h = t.entry_time.hour
                if tod_h < 12:
                    tb_fallback = "MORNING"
                elif tod_h < 13 or (tod_h == 13 and t.entry_time.minute < 30):
                    tb_fallback = "MIDDAY"
                else:
                    tb_fallback = "AFTERNOON"
                time_buckets[tb_fallback].append(is_win)
                
        # VIX bucket grouping
        vix = t.vix_at_entry
        if vix is not None:
            if vix < 13.0:
                vix_buckets["lt_13"].append(is_win)
            elif vix < 16.0:
                vix_buckets["13_16"].append(is_win)
            elif vix <= 20.0:
                vix_buckets["16_20"].append(is_win)

    def calc_stats(trade_list):
        total = len(trade_list)
        if total == 0:
            return {"win_rate": 0, "avg_rr": 0.0, "trades": 0}
        wins = sum(1 for t in trade_list if (t.realized_pnl or 0.0) > 0)
        win_rate = round((wins / total) * 100, 1)
        
        rr_vals = []
        for t in trade_list:
            val = t.rr_ratio
            if val is None or val <= 0:
                risk_val = abs(t.entry_price - t.sl_price) if t.sl_price else 0
                reward_val = abs(t.target_price - t.entry_price) if t.target_price else 0
                val = round(reward_val / risk_val, 2) if risk_val > 0 else 0.0
            if val > 0:
                rr_vals.append(val)
                
        avg_rr = round(sum(rr_vals) / len(rr_vals), 2) if rr_vals else 0.0
        return {"win_rate": win_rate, "avg_rr": avg_rr, "trades": total}

    def calc_bucket_winrate(bool_list):
        total = len(bool_list)
        if total == 0:
            return {"win_rate": 0, "trades": 0}
        wins = sum(1 for is_win in bool_list if is_win)
        return {"win_rate": round((wins / total) * 100, 1), "trades": total}

    return {
        "momentum": calc_stats(momentum_trades),
        "orb": calc_stats(orb_trades),
        "time_buckets": {k: calc_bucket_winrate(v) for k, v in time_buckets.items()},
        "vix_buckets": {k: calc_bucket_winrate(v) for k, v in vix_buckets.items()}
    }

@app.get("/api/performance/report")
def api_get_performance_report(user: dict = Depends(require_auth), db=Depends(get_db)):
    uid = user["uid"]
    
    # Get all closed trades
    trades = db.query(Trade).filter(Trade.user_id == uid, Trade.status != 'OPEN').all()
    
    total_trades = len(trades)
    if total_trades == 0:
        return {
            "total_trades": 0,
            "win_rate": 0,
            "avg_win": 0,
            "avg_loss": 0,
            "expectancy": 0,
            "best_time_bucket": "N/A",
            "best_strategy": "N/A",
            "claude_stats": {
                "claude_approved_count": 0,
                "claude_win_rate": 0,
                "no_claude_count": 0,
                "no_claude_win_rate": 0
            }
        }
        
    wins = [t.realized_pnl for t in trades if t.realized_pnl > 0]
    losses = [t.realized_pnl for t in trades if t.realized_pnl < 0]
    
    win_rate = round((len(wins) / total_trades) * 100, 1)
    avg_win = round(sum(wins) / len(wins), 2) if wins else 0.0
    avg_loss = round(sum(losses) / len(losses), 2) if losses else 0.0
    expectancy = round(sum(t.realized_pnl for t in trades) / total_trades, 2)
    
    # Group by strategy
    strat_perf = {}
    for t in trades:
        strat = (t.strategy_type or "MOMENTUM").upper()
        if strat not in strat_perf:
            strat_perf[strat] = []
        strat_perf[strat].append(t.realized_pnl > 0)
        
    best_strategy = "N/A"
    best_strat_wr = -1.0
    for strat, results in strat_perf.items():
        wr = sum(1 for r in results if r) / len(results)
        if wr > best_strat_wr:
            best_strat_wr = wr
            best_strategy = f"{strat} ({round(wr * 100)}% WR)"
            
    # Group by time bucket
    time_perf = {}
    for t in trades:
        tb = (t.time_of_day_bucket or "").upper()
        if not tb:
            if t.entry_time:
                tod_h = t.entry_time.hour
                if tod_h < 12:
                    tb = "MORNING"
                elif tod_h < 13 or (tod_h == 13 and t.entry_time.minute < 30):
                    tb = "MIDDAY"
                else:
                    tb = "AFTERNOON"
        if tb:
            if tb not in time_perf:
                time_perf[tb] = []
            time_perf[tb].append(t.realized_pnl > 0)
            
    best_time_bucket = "N/A"
    best_time_wr = -1.0
    for tb, results in time_perf.items():
        wr = sum(1 for r in results if r) / len(results)
        if wr > best_time_wr:
            best_time_wr = wr
            best_time_bucket = f"{tb} ({round(wr * 100)}% WR)"
            
    # Claude comparison stats
    claude_approved = trades  # in our setup all executed trades went through Claude (or local rules acting as Claude)
    claude_wins = sum(1 for t in claude_approved if t.realized_pnl > 0)
    claude_win_rate = round((claude_wins / len(claude_approved)) * 100, 1) if claude_approved else 0.0
    
    # trades that would have entered without Claude (rules only)
    no_claude = [t for t in trades if t.would_have_entered_without_claude]
    no_claude_wins = sum(1 for t in no_claude if t.realized_pnl > 0)
    no_claude_win_rate = round((no_claude_wins / len(no_claude)) * 100, 1) if no_claude else 0.0
    
    return {
        "total_trades": total_trades,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "expectancy": expectancy,
        "best_time_bucket": best_time_bucket,
        "best_strategy": best_strategy,
        "claude_stats": {
            "claude_approved_count": len(claude_approved),
            "claude_win_rate": claude_win_rate,
            "no_claude_count": len(no_claude),
            "no_claude_win_rate": no_claude_win_rate
        }
    }

@app.get("/api/status")
def api_get_status(user: dict = Depends(require_auth), db=Depends(get_db)):
    uid = user["uid"]
    bot = get_or_create_bot(uid, db)
    global_halt = not get_global_trading_enabled()
    # Merge live Indian indices and International/Global indices
    all_indices = dict(_shared_state.live_indices)
    all_prev_close = dict(_shared_state.index_prev_close)
    try:
        from global_indices_service import get_global_indices_data
        g_prices, g_prev = get_global_indices_data()
        all_indices.update(g_prices)
        all_prev_close.update(g_prev)
    except Exception as e:
        logger.debug(f"Error merging global indices: {e}")

    if not bot:
        return {
            "is_running": False, "kill_switch_active": False,
            "daily_pnl": 0.0, "trades_taken": 0, "max_trades": Config.MAX_TRADES_PER_DAY,
            "market_condition": "WAITING", "active_capital": 0.0,
            "account_balance": 0.0, "usable_balance": 0.0,
            "indices": all_indices,
            "indices_prev_close": all_prev_close,
            "active_trades": [], "mode": "TEST",
            "credentials_set": False,
            "ws_live": False,
            "global_halt": global_halt,
            "paper_trading": Config.PAPER_TRADING_MODE,
        }

    # Fetch live account balance and cache it for 60 seconds to avoid rate limiting
    import time
    now = time.time()
    last_update = getattr(bot.state, 'last_balance_update', 0.0)
    if now - last_update > 60.0 or not getattr(bot.state, 'account_balance', None):
        try:
            bot.state.account_balance = bot.engine.get_funds(force_live=True)
            bot.state.last_balance_update = now
        except Exception as e:
            logger.error(f"Error fetching real account balance: {e}")
            if not hasattr(bot.state, 'account_balance'):
                bot.state.account_balance = 0.0

    # Determine Usable Balance based on Manual Capital config
    manual_cap = float(bot.config.get("MANUAL_CAPITAL", Config.MANUAL_CAPITAL))
    bot.state.usable_balance = manual_cap
    bot.state.active_capital = manual_cap

    # Calculate live daily pnl and dynamic active trades for fast frontend updates
    dynamic_trades = []
    active_pnl = 0.0
    for t in bot.tm.active_trades:
        trade_copy = dict(t)
        symbol = t["symbol"]
        ltp = _shared_prices.get(symbol, t.get("current_price", t["entry_price"]))
        if ltp == t.get("current_price", t["entry_price"]):
            sym_key = Config.SYMBOLS_MAPPING.get(symbol)
            if sym_key and sym_key in _shared_prices:
                ltp = _shared_prices[sym_key]
        
        trade_copy["current_price"] = round(ltp, 2)
        qty = t["qty"]
        entry = t["entry_price"]
        side = t["side"]
        if side == "BUY":
            pnl = (ltp - entry) * qty
        else:
            pnl = (entry - ltp) * qty
        trade_copy["pnl"] = round(pnl, 2)
        active_pnl += pnl
        dynamic_trades.append(trade_copy)
        
    history_pnl = sum([t['pnl'] for t in bot.tm.trade_history if t['timestamp'][:10] == get_ist_now().isoformat()[:10]])
    live_daily_pnl = round(history_pnl + active_pnl, 2)
    trades_taken_today = bot.tm.get_trades_taken_today()
    bot.state.trades_taken = trades_taken_today

    return {
        "is_running":         bot.state.is_running,
        "kill_switch_active": bot.state.kill_switch_active,
        "daily_pnl":          live_daily_pnl,
        "trades_taken":       trades_taken_today,
        "max_trades":         int(bot.config.get('MAX_TRADES_PER_DAY', Config.MAX_TRADES_PER_DAY) or 5),
        "market_condition":   bot.state.market_condition,
        "active_capital":     bot.state.active_capital,
        "account_balance":    bot.state.account_balance,
        "usable_balance":     bot.state.usable_balance,
        "indices":            all_indices,
        "indices_prev_close": all_prev_close,
        "active_trades":      dynamic_trades,
        "mode":               bot.config.get("MODE", "TEST"),
        "credentials_set":    bool(bot.creds.get("upstox_access_token")),
        "upstox_updated_at":  bot.creds.get("updated_at"),
        "ws_live":            market_feed.is_live() if 'market_feed' in globals() else False,
        "global_halt":        global_halt,
        "paper_trading":      bot.engine.is_paper,
    }

class ToggleRequest(BaseModel):
    action: str  # "start" or "stop"

@app.post("/api/toggle")
async def api_toggle(body: ToggleRequest, user: dict = Depends(require_auth), db=Depends(get_db)):
    from database import UserConfig
    uid = user["uid"]
    bot = get_or_create_bot(uid, db)
    cfg = db.query(UserConfig).filter(UserConfig.user_id == uid).first()
    if not cfg:
        cfg = UserConfig(user_id=uid)
        db.add(cfg)
    
    if body.action == "start":
        bot.state.is_running = True
        bot.state.kill_switch_active = False
        cfg.is_running = 1
        db.commit()
        await ensure_user_bot_task(uid, db)
        return {"status": "started"}
    else:
        bot.state.is_running = False
        cfg.is_running = 0
        db.commit()
        return {"status": "stopped"}

@app.post("/api/emergency")
async def api_emergency(user: dict = Depends(require_auth), db=Depends(get_db)):
    """Emergency square-off for the authenticated user's positions only."""
    from database import UserConfig
    uid = user["uid"]
    bot = get_or_create_bot(uid, db)
    bot.state.is_running = False
    bot.state.kill_switch_active = True
    
    cfg = db.query(UserConfig).filter(UserConfig.user_id == uid).first()
    if cfg:
        cfg.is_running = 0
        db.commit()
        
    result = bot.engine.exit_all_positions()
    for trade in list(bot.tm.active_trades):
        bot.tm.close_trade(trade, "EMERGENCY", execution_engine=None)
    return {"status": "emergency_executed", "engine_result": result}

@app.get("/api/symbols")
def api_get_symbols(user: dict = Depends(require_auth)):
    return Config.SYMBOLS_MAPPING

@app.get("/api/instruments/stocks/search")
def api_search_stocks(q: str = "", limit: int = 15, user: dict = Depends(require_auth)):
    """Autocomplete search for NSE listed equities."""
    from instrument_service import instrument_service
    return instrument_service.search_stocks(q, limit=limit)

@app.get("/api/instruments/options/underlyings")
def api_get_options_underlyings(user: dict = Depends(require_auth)):
    """Returns available underlying symbols for options trading."""
    from instrument_service import instrument_service
    return instrument_service.get_options_underlyings()

@app.get("/api/instruments/options/expiries")
def api_get_options_expiries(underlying: str, user: dict = Depends(require_auth)):
    """Returns sorted expiration dates for the selected underlying."""
    from instrument_service import instrument_service
    return instrument_service.get_options_expiries(underlying)

@app.get("/api/instruments/options/contracts")
def api_get_options_contracts(
    underlying: str,
    expiry: Optional[int] = None,
    type: Optional[str] = None,
    user: dict = Depends(require_auth)
):
    """Returns sorted options contracts for underlying, expiry, and CE/PE."""
    from instrument_service import instrument_service
    return instrument_service.get_options_contracts(underlying, expiry=expiry, option_type=type)

@app.get("/api/market/ltp")
def api_get_market_ltp(
    instrument_key: str,
    symbol: Optional[str] = None,
    user: dict = Depends(require_auth)
):
    """Fetches real-time market price (LTP) from shared streaming prices or broker quote API."""
    # 1. Check live feed cache
    if symbol and symbol in _shared_prices and float(_shared_prices[symbol]) > 0:
        return {"ltp": float(_shared_prices[symbol]), "source": "feed"}
    if instrument_key in _shared_prices and float(_shared_prices[instrument_key]) > 0:
        return {"ltp": float(_shared_prices[instrument_key]), "source": "feed"}
    
    # 2. Check colon vs pipe format in live feed
    alt_key = instrument_key.replace('|', ':') if '|' in instrument_key else instrument_key.replace(':', '|')
    if alt_key in _shared_prices and float(_shared_prices[alt_key]) > 0:
        return {"ltp": float(_shared_prices[alt_key]), "source": "feed"}

    # 3. Query Upstox quotes API via user's execution engine
    uid = user["uid"]
    bot = get_bot(uid)
    if bot:
        try:
            live_ltp = bot.engine.get_ltp(instrument_key)
            if live_ltp > 0:
                return {"ltp": float(live_ltp), "source": "broker_quote"}
        except Exception as e:
            logger.warning(f"Error fetching quote for {instrument_key}: {e}")

    # 4. Fallback from Config.SYMBOLS_MAPPING
    if symbol and symbol in Config.SYMBOLS_MAPPING:
        token = Config.SYMBOLS_MAPPING[symbol]
        if token in _shared_prices and float(_shared_prices[token]) > 0:
            return {"ltp": float(_shared_prices[token]), "source": "feed"}

    return {"ltp": 0.0, "source": "unavailable"}

@app.post("/api/auth/refresh-upstox")
def api_refresh_upstox_token(user: dict = Depends(require_auth), db=Depends(get_db)):
    """Generates a fresh Upstox access token using TOTP and applies it immediately."""
    from upstox_auth_manager import auto_refresh_and_apply
    from user_bot_registry import reload_bot_credentials
    res = auto_refresh_and_apply()
    if res.get("status") == "success":
        reload_bot_credentials(user["uid"], db)
    return res

@app.get("/api/config")
def api_get_config(user: dict = Depends(require_auth), db=Depends(get_db)):
    """Returns configuration for the CURRENT user."""
    return get_user_config_dict(db, user["uid"])

@app.post("/api/config")
async def api_update_config(payload: dict, user: dict = Depends(require_auth), db=Depends(get_db)):
    """Updates configuration and credentials for the CURRENT user."""
    from database import UserConfig
    from credential_service import upsert_credentials
    from user_bot_registry import reload_user_config, reload_bot_credentials
    
    uid = user["uid"]
    cfg = db.query(UserConfig).filter(UserConfig.user_id == uid).first()
    if not cfg:
        cfg = UserConfig(user_id=uid)
        db.add(cfg)
    
    # Update strategy settings
    if "MAX_TRADES_PER_DAY" in payload: cfg.max_trades_per_day = int(payload["MAX_TRADES_PER_DAY"])
    if "MAX_OPEN_TRADES" in payload: cfg.max_open_trades = int(payload["MAX_OPEN_TRADES"])
    if "DAILY_LOSS_LIMIT" in payload: cfg.daily_loss_limit = float(payload["DAILY_LOSS_LIMIT"])
    if "TRAILING_SL_ACTIVATION" in payload: cfg.trailing_sl_activation = float(payload["TRAILING_SL_ACTIVATION"])
    if "VOLUME_SPIKE_RATIO" in payload: cfg.volume_spike_ratio = float(payload["VOLUME_SPIKE_RATIO"])
    if "MIN_CANDLE_STRENGTH" in payload: cfg.min_candle_strength = float(payload["MIN_CANDLE_STRENGTH"])
    if "MIN_STOCK_PRICE" in payload: cfg.min_stock_price = float(payload["MIN_STOCK_PRICE"])
    if "MAX_STOCK_PRICE" in payload: cfg.max_stock_price = float(payload["MAX_STOCK_PRICE"])
    if "USE_CLAUDE_FILTER" in payload: cfg.use_claude_filter = 1 if payload["USE_CLAUDE_FILTER"] else 0
    if "USE_LIVE_CAPITAL" in payload: cfg.use_live_capital = 1 if payload["USE_LIVE_CAPITAL"] else 0
    if "HYBRID_VIRTUAL_MODE" in payload: cfg.hybrid_virtual_mode = 1 if payload["HYBRID_VIRTUAL_MODE"] else 0
    if "MANUAL_CAPITAL" in payload: cfg.manual_capital = float(payload["MANUAL_CAPITAL"])
    if "MODE" in payload: cfg.trading_mode = payload["MODE"].upper()
    
    db.commit()
    
    # Handle write-only credentials
    creds_updated = False
    cred_fields = ["UPSTOX_CLIENT_ID", "UPSTOX_CLIENT_SECRET", "UPSTOX_ACCESS_TOKEN", "ANALYTICS_TOKEN"]
    if any(k in payload for k in cred_fields):
        upsert_credentials(
            db, uid,
            upstox_client_id=payload.get("UPSTOX_CLIENT_ID"),
            upstox_client_secret=payload.get("UPSTOX_CLIENT_SECRET"),
            upstox_access_token=payload.get("UPSTOX_ACCESS_TOKEN"),
            analytics_token=payload.get("ANALYTICS_TOKEN")
        )
        creds_updated = True
    
    # Refresh the active bot instance in the registry
    reload_user_config(uid, db)
    if creds_updated:
        reload_bot_credentials(uid, db)
        await ensure_user_bot_task(uid, db)
        
        # ALSO update the shared components (shared engine & market feed WebSocket)
        from credential_service import get_decrypted_credentials
        fresh_creds = get_decrypted_credentials(db, uid)
        if fresh_creds.get("upstox_access_token"):
            logger.info(f"Hot-reloading shared engine and market feed with config updated credentials from user {uid}")
            _shared_engine._creds = fresh_creds
            
            # Restart market feed with new credentials
            market_feed.stop()
            # Wait a brief moment for it to stop cleanly
            await asyncio.sleep(1)
            market_feed.start(
                _shared_prices, 
                _shared_state.live_indices, 
                _shared_state.index_prev_close, 
                None, 
                _shared_timestamps,
                credentials=fresh_creds
            )
        
    return {"status": "updated"}

def get_user_config_dict(db, user_id: int) -> dict:
    from database import UserConfig
    cfg = db.query(UserConfig).filter(UserConfig.user_id == user_id).first()
    if not cfg:
        # Create default config for this user if none exists
        cfg = UserConfig(user_id=user_id)
        db.add(cfg)
        db.commit()
        db.refresh(cfg)
    
    return {
        "MAX_TRADES_PER_DAY": cfg.max_trades_per_day,
        "MAX_OPEN_TRADES": cfg.max_open_trades or 2,
        "DAILY_LOSS_LIMIT": cfg.daily_loss_limit,
        "TRAILING_SL_ACTIVATION": cfg.trailing_sl_activation,
        "VOLUME_SPIKE_RATIO": cfg.volume_spike_ratio,
        "MIN_CANDLE_STRENGTH": cfg.min_candle_strength,
        "MIN_STOCK_PRICE": cfg.min_stock_price,
        "MAX_STOCK_PRICE": cfg.max_stock_price,
        "USE_CLAUDE_FILTER": bool(cfg.use_claude_filter),
        "USE_LIVE_CAPITAL": bool(cfg.use_live_capital),
        "HYBRID_VIRTUAL_MODE": getattr(cfg, 'hybrid_virtual_mode', 1) == 1,
        "MANUAL_CAPITAL": cfg.manual_capital,
        "MODE": cfg.trading_mode or "TEST"
    }

@app.get("/api/logs")
def api_get_logs(limit: int = 100, user: dict = Depends(require_auth), db=Depends(get_db)):
    from database import SystemLog
    uid = user["uid"]
    logs = db.query(SystemLog).filter(SystemLog.user_id == uid).order_by(desc(SystemLog.timestamp)).limit(limit).all()
    return [{
        "id": l.id, "timestamp": to_ist_iso(l.timestamp) if l.timestamp else None,
        "level": l.log_level, "event": l.event_type, "message": l.message,
    } for l in logs]

class ManualTradeRequest(BaseModel):
    symbol: str
    side:   str
    qty:    int
    price:  float = 0.0

@app.post("/api/trade/manual")
async def api_manual_trade(
    body: ManualTradeRequest,
    user: dict = Depends(require_auth),
    db=Depends(get_db)
):
    uid = user["uid"]
    role = user.get("role", "user")

    symbol_key = Config.SYMBOLS_MAPPING.get(body.symbol)
    if not symbol_key:
        try:
            from nifty500_instruments import get_universe
            symbol_key = get_universe().get(body.symbol)
        except Exception:
            pass
    if not symbol_key:
        symbol_key = f"NSE_EQ|{body.symbol}"
        
    side = body.side.upper()
    if side not in ("BUY", "SELL"):
        raise HTTPException(status_code=400, detail=f"Invalid order side: '{body.side}'. Must be BUY or SELL.")

    # Check if Multi-Account Trading is enabled and user is Master Admin
    from multi_account_manager import MultiAccountManager
    if role in ("admin", "master_admin") and MultiAccountManager.is_multi_account_enabled(db):
        order_payload = {
            "symbol": body.symbol,
            "instrument_key": symbol_key,
            "side": side,
            "qty": body.qty or 1,
            "price": body.price or 0.0,
            "trigger_price": 0.0,
            "sl": 0.0,
            "target": 0.0,
            "order_type": "MARKET",
            "radar_type": "ACTION_SIGNAL",
            "product": "I"
        }
        return await MultiAccountManager.execute_multi_account_trade(
            master_user_id=uid,
            order_payload=order_payload,
            db=db
        )

    bot = get_or_create_bot(uid, db)
    
    max_open = int(bot.config.get('MAX_OPEN_TRADES', Config.MAX_OPEN_TRADES) or 2)
    if len(bot.tm.active_trades) >= max_open:
        raise HTTPException(status_code=400, detail=f"Cannot place manual trade. Max open trades limit ({max_open}) reached.")
        
    # Always fetch the verified real-time live market LTP
    live_ltp = bot.engine.get_ltp(symbol_key)
    if live_ltp <= 0:
        live_ltp = float(_shared_prices.get(body.symbol, 0.0))
    if live_ltp <= 0 and body.price and body.price > 0:
        live_ltp = float(body.price)
    if live_ltp <= 0:
        raise HTTPException(status_code=400, detail=f"Unable to fetch real-time market price for {body.symbol}.")

    # Dynamic Capital Allocation & 5x Intraday Leverage
    # Total Capital (e.g. ₹10,000) / Max Open Trades (e.g. 2) = ₹5,000 per trade
    # 5x Intraday Leverage = ₹25,000 effective buying power
    # Quantity = Math.floor(25,000 / live_ltp)
    manual_capital = float(bot.config.get('MANUAL_CAPITAL', Config.MANUAL_CAPITAL) or 10000.0)
    capital_per_trade = manual_capital / max(max_open, 1)
    leverage_multiplier = 5.0
    effective_buying_power = capital_per_trade * leverage_multiplier
    leveraged_qty = max(1, math.floor(effective_buying_power / live_ltp))

    if body.qty and int(body.qty) > 0:
        qty = int(body.qty)
    else:
        qty = leveraged_qty

    logger.info(
        f"[USER {uid}] Manual trade: {side} {qty} shares of {body.symbol} @ Rs{live_ltp:.2f} "
        f"(Slot Capital: Rs{capital_per_trade:.0f}, 5x Buying Power: Rs{effective_buying_power:.0f})"
    )
        
    order_type = "MARKET"
    res = bot.engine.place_order_with_retry(
        symbol_key, side, qty,
        order_type=order_type, price=0.0
    )
    
    order_status = res.get("status", "success")
    order_id = res.get("order_id") or f"MAN_{int(_time.time()*1000)}"
    
    if order_status in ("success", "COMPLETE"):
        entry_price = round(live_ltp, 2)
        sl_pct = 0.01
        sl = round(entry_price * (1.0 + sl_pct) if side == "SELL" else entry_price * (1.0 - sl_pct), 2)
        target = round(entry_price * (1.0 - 2.0 * sl_pct) if side == "SELL" else entry_price * (1.0 + 2.0 * sl_pct), 2)
        
        bot.tm.add_trade(
            symbol=body.symbol,
            side=side,
            entry_price=entry_price,
            qty=qty,
            sl=sl,
            target=target,
            probability=0.75,
            score=80,
            metadata={
                "manual_execution": True,
                "paper_trade": bot.engine.is_paper,
                "instrument_token": symbol_key
            }
        )
        if bot.tm.active_trades:
            bot.tm.active_trades[-1]['instrument_token'] = symbol_key
            
        send_alert(uid, f"⚡ Manual {side} Order Executed for {body.symbol} ({qty} qty @ ₹{entry_price:.2f})")
        return {"status": "success", "order_id": order_id, "symbol": body.symbol, "side": side, "qty": qty, "price": entry_price}
    else:
        reason = res.get("reason", "Order rejected by broker.")
        raise HTTPException(status_code=400, detail=f"Order rejected: {reason}")

class PrimeRadarTradeRequest(BaseModel):
    symbol: str
    instrument_key: str
    side: str  # BUY or SELL
    order_type: str  # LIMIT or SL-M
    qty: int
    price: float = 0.0  # Limit Price (for LIMIT)
    trigger_price: float = 0.0  # SL Trigger Price (for SL-M)
    sl: float = 0.0  # Stop-Loss Price
    target: float = 0.0  # Target Price
    radar_type: str = "INTRADAY"  # "INTRADAY" or "OPTIONS"
    product: str = "I"  # "I" for Intraday (MIS)
    contract_info: Optional[Dict[str, Any]] = None

@app.post("/api/prime-radar/execute")
async def api_prime_radar_execute(
    body: PrimeRadarTradeRequest,
    user: dict = Depends(require_auth),
    db=Depends(get_db)
):
    """Executes validated orders from PrimeRadar – Intraday or PrimeRadar – Options."""
    uid = user["uid"]
    role = user.get("role", "user")

    # Multi-Account Trade Replication (Master Admin Only)
    from multi_account_manager import MultiAccountManager
    if role in ("admin", "master_admin") and MultiAccountManager.is_multi_account_enabled(db):
        return await MultiAccountManager.execute_multi_account_trade(
            master_user_id=uid,
            order_payload=body.dict(),
            db=db
        )

    bot = get_or_create_bot(uid, db)

    # 1. Side & Order Type validation
    side = body.side.upper()
    if side not in ("BUY", "SELL"):
        raise HTTPException(status_code=400, detail=f"Invalid side '{body.side}'. Must be BUY or SELL.")

    order_type = body.order_type.upper()
    if order_type not in ("LIMIT", "SL-M"):
        raise HTTPException(status_code=400, detail=f"Invalid order type '{body.order_type}'. Must be LIMIT or SL-M.")

    # 2. Quantity & Lot size validation
    if body.qty <= 0:
        raise HTTPException(status_code=400, detail="Quantity must be greater than 0.")

    lot_size = 1
    if body.contract_info and "lot_size" in body.contract_info:
        try:
            lot_size = int(body.contract_info["lot_size"])
        except (ValueError, TypeError):
            lot_size = 1
    elif body.radar_type == "OPTIONS":
        inst = instrument_service.get_instrument_by_key(body.instrument_key)
        if inst and "lot_size" in inst:
            lot_size = int(inst["lot_size"])

    if lot_size > 1 and body.qty % lot_size != 0:
        raise HTTPException(status_code=400, detail=f"Options quantity ({body.qty}) must be a multiple of lot size ({lot_size}).")

    # 3. Price & Trigger validation
    if order_type == "LIMIT":
        if body.price <= 0:
            raise HTTPException(status_code=400, detail="Limit Price must be greater than 0 for LIMIT orders.")
        if side == "BUY":
            if body.sl > 0 and body.sl >= body.price:
                raise HTTPException(status_code=400, detail="For BUY orders, Stop-Loss must be lower than Limit Price.")
            if body.target > 0 and body.target <= body.price:
                raise HTTPException(status_code=400, detail="For BUY orders, Target Price must be higher than Limit Price.")
        elif side == "SELL":
            if body.sl > 0 and body.sl <= body.price:
                raise HTTPException(status_code=400, detail="For SELL orders, Stop-Loss must be higher than Limit Price.")
            if body.target > 0 and body.target >= body.price:
                raise HTTPException(status_code=400, detail="For SELL orders, Target Price must be lower than Limit Price.")

    elif order_type == "SL-M":
        if body.trigger_price <= 0:
            raise HTTPException(status_code=400, detail="SL Trigger Price must be greater than 0 for SL-M orders.")
        if side == "BUY":
            if body.sl > 0 and body.sl >= body.trigger_price:
                raise HTTPException(status_code=400, detail="For BUY orders, Stop-Loss must be lower than Trigger Price.")
            if body.target > 0 and body.target <= body.trigger_price:
                raise HTTPException(status_code=400, detail="For BUY orders, Target Price must be higher than Trigger Price.")
        elif side == "SELL":
            if body.sl > 0 and body.sl <= body.trigger_price:
                raise HTTPException(status_code=400, detail="For SELL orders, Stop-Loss must be higher than Trigger Price.")
            if body.target > 0 and body.target >= body.trigger_price:
                raise HTTPException(status_code=400, detail="For SELL orders, Target Price must be lower than Trigger Price.")

    # 4. Check max open trades limit
    max_open = int(bot.config.get('MAX_OPEN_TRADES', Config.MAX_OPEN_TRADES) or 5)
    if len(bot.tm.active_trades) >= max_open:
        raise HTTPException(status_code=400, detail=f"Order rejected: Max open trades limit ({max_open}) reached.")

    # 5. Resolve Live LTP
    live_ltp = 0.0
    if body.symbol in _shared_prices:
        live_ltp = float(_shared_prices[body.symbol])
    elif body.instrument_key in _shared_prices:
        live_ltp = float(_shared_prices[body.instrument_key])

    if live_ltp <= 0:
        try:
            live_ltp = float(bot.engine.get_ltp(body.instrument_key))
        except Exception:
            pass

    if live_ltp <= 0:
        live_ltp = body.price if (order_type == "LIMIT" and body.price > 0) else (body.trigger_price if body.trigger_price > 0 else 0.0)

    if live_ltp <= 0 and not bot.engine.is_paper:
        raise HTTPException(status_code=400, detail=f"Unable to retrieve verified market quote for {body.symbol}.")

    if live_ltp <= 0:
        live_ltp = 100.0  # Fallback for paper testing when market is offline

    # 6. Execute Order via Broker Engine
    logger.info(
        f"[USER {uid}] PrimeRadar ({body.radar_type}): Submitting {side} {order_type} for "
        f"{body.symbol} ({body.qty} qty) Price: {body.price}, Trigger: {body.trigger_price}"
    )

    res = bot.engine.place_order_with_retry(
        symbol=body.instrument_key,
        side=side,
        qty=body.qty,
        order_type=order_type,
        price=body.price,
        trigger_price=body.trigger_price,
        product=body.product
    )

    order_status = res.get("status", "success")
    order_id = res.get("order_id") or f"PRIME_{int(_time.time()*1000)}"

    if order_status in ("success", "COMPLETE", "pending"):
        entry_price = float(res.get("average_price") or (body.price if order_type == "LIMIT" else (body.trigger_price or live_ltp)))
        if entry_price <= 0:
            entry_price = round(live_ltp, 2)

        # Fallback SL & Target if omitted
        sl = body.sl
        target = body.target
        if sl <= 0:
            sl_pct = 0.01
            sl = round(entry_price * (1.0 + sl_pct) if side == "SELL" else entry_price * (1.0 - sl_pct), 2)
        if target <= 0:
            target_pct = 0.02
            target = round(entry_price * (1.0 - target_pct) if side == "SELL" else entry_price * (1.0 + target_pct), 2)

        bot.tm.add_trade(
            symbol=body.symbol,
            side=side,
            entry_price=entry_price,
            qty=body.qty,
            sl=sl,
            target=target,
            probability=0.85,
            score=88,
            metadata={
                "prime_radar": True,
                "radar_type": body.radar_type,
                "order_type": order_type,
                "limit_price": body.price,
                "trigger_price": body.trigger_price,
                "paper_trade": bot.engine.is_paper,
                "instrument_token": body.instrument_key,
                "contract_info": body.contract_info or {}
            }
        )
        if bot.tm.active_trades:
            bot.tm.active_trades[-1]['instrument_token'] = body.instrument_key

        trade_msg = (
            f"⚡ PrimeRadar [{body.radar_type}] {side} {order_type} executed: "
            f"{body.qty} of {body.symbol} @ ₹{entry_price:.2f} (SL: ₹{sl:.2f}, Target: ₹{target:.2f})"
        )
        send_alert(uid, trade_msg)

        return {
            "status": "success",
            "order_id": order_id,
            "symbol": body.symbol,
            "side": side,
            "qty": body.qty,
            "price": entry_price,
            "sl": sl,
            "target": target,
            "order_type": order_type,
            "radar_type": body.radar_type
        }
    else:
        reason = res.get("reason", "Order rejected by broker.")
        logger.error(f"[USER {uid}] PrimeRadar order rejected: {reason}")
        raise HTTPException(status_code=400, detail=f"Order rejected: {reason}")

class ManualSLUpdate(BaseModel):
    trade_id:  str
    new_sl:    float
    order_id:  Optional[str] = None
    qty:       Optional[int] = 1

@app.post("/api/trade/update-sl")
async def api_update_sl(
    body: ManualSLUpdate,
    user: dict = Depends(require_auth),
    db=Depends(get_db)
):
    uid = user["uid"]
    bot = get_or_create_bot(uid, db)
    trade = next((t for t in bot.tm.active_trades if t['id'] == body.trade_id), None)
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found.")
    
    trade['sl'] = body.new_sl
    if body.order_id:
        bot.engine.modify_order(body.order_id, 0, body.new_sl, body.qty or trade.get('qty', 1))
    
    try:
        db_trade = db.query(Trade).filter(Trade.id == body.trade_id, Trade.user_id == uid).first()
        if db_trade:
            db_trade.sl_price = body.new_sl
            db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to update SL in DB: {e}")
        
    return {"status": "success", "new_sl": body.new_sl}

@app.post("/api/trade/close")
async def api_close_trade(
    trade_id: str,
    user: dict = Depends(require_auth),
    db=Depends(get_db)
):
    uid = user["uid"]
    bot = get_or_create_bot(uid, db)
    trade = next((t for t in bot.tm.active_trades if t['id'] == trade_id), None)
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found.")
    # Delegate square-off to TradeManager which cancels pending SL and places exactly ONE market exit order
    bot.tm.close_trade(trade, "MANUAL", execution_engine=bot.engine)
    return {"status": "success", "trade_id": trade_id}

@app.post("/api/trades/{trade_id}/close")
async def api_close_trade_path(
    trade_id: str,
    user: dict = Depends(require_auth),
    db=Depends(get_db)
):
    """Path-parameter endpoint for closing trades as expected by the frontend."""
    await api_close_trade(trade_id, user, db)
    return {"status": "success", "trade_id": trade_id}

class PathSLUpdate(BaseModel):
    new_sl: float

@app.post("/api/trades/{trade_id}/sl")
async def api_update_sl_path(
    trade_id: str,
    body: PathSLUpdate,
    user: dict = Depends(require_auth),
    db=Depends(get_db)
):
    """Path-parameter endpoint for modifying Stop Loss as expected by the frontend."""
    uid = user["uid"]
    bot = get_or_create_bot(uid, db)
    trade = next((t for t in bot.tm.active_trades if t['id'] == trade_id), None)
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found.")
    
    trade['sl'] = body.new_sl
    qty = trade.get('qty', 1)
    sl_order_id = trade.get('sl_order_id')
    if sl_order_id:
        # Round new SL trigger to tick size (0.05)
        rounded_trigger = round(body.new_sl * 20) / 20.0
        bot.engine.modify_order(sl_order_id, 0, rounded_trigger, qty)
        
    try:
        db_trade = db.query(Trade).filter(Trade.id == trade_id, Trade.user_id == uid).first()
        if db_trade:
            db_trade.sl_price = body.new_sl
            db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to update SL in DB: {e}")
        
    return {"status": "success", "new_sl": body.new_sl}

# Dual strategy endpoints (shared engine for demo, per-user for live)
@app.post("/api/dual/start")
async def dual_start(body: dict, user: dict = Depends(require_auth), db=Depends(get_db)):
    uid = user["uid"]
    bot = get_or_create_bot(uid, db)
    return bot.dual.start(body)

@app.post("/api/dual/stop")
async def dual_stop(user: dict = Depends(require_auth), db=Depends(get_db)):
    uid = user["uid"]
    bot = get_or_create_bot(uid, db)
    return bot.dual.stop()

@app.get("/api/dual/status")
async def dual_status(user: dict = Depends(require_auth), db=Depends(get_db)):
    uid = user["uid"]
    bot = get_or_create_bot(uid, db)
    return bot.dual.get_status()


# ────────────────────────────────────────────────────────────────────────────
# v2 API ENDPOINTS — Global Kill Switch (Bug Fix 7)
# ────────────────────────────────────────────────────────────────────────────

@app.post("/api/admin/halt")
def admin_halt_trading(user: dict = Depends(require_admin)):
    """Bug Fix 7: Halt all new entries globally. Existing positions still monitored."""
    set_global_trading_enabled(False)
    alert_global_halt_activated()
    logger.critical(f"[ADMIN] Global halt activated by {user.get('sub', 'unknown')}")
    return {"status": "halted", "global_trading_enabled": False}

@app.post("/api/admin/resume")
def admin_resume_trading(user: dict = Depends(require_admin)):
    """Bug Fix 7: Resume global trading."""
    set_global_trading_enabled(True)
    alert_global_halt_lifted()
    logger.info(f"[ADMIN] Global halt lifted by {user.get('sub', 'unknown')}")
    return {"status": "resumed", "global_trading_enabled": True}

@app.get("/api/admin/halt")
def admin_get_halt_status(user: dict = Depends(require_auth)):
    """Get current global halt status (accessible to all authenticated users for dashboard)."""
    enabled = get_global_trading_enabled()
    return {"global_trading_enabled": enabled, "global_halt": not enabled}

# Legacy kill switch endpoint (backwards compat)
@app.post("/api/admin/kill-switch")
def toggle_global_kill_switch(active: bool, user: dict = Depends(require_admin)):
    set_global_trading_enabled(not active)
    if active:
        alert_global_halt_activated()
    else:
        alert_global_halt_lifted()
    return {"status": "success", "global_kill_switch": active}

@app.get("/api/admin/kill-switch")
def get_global_kill_switch(user: dict = Depends(require_admin)):
    enabled = get_global_trading_enabled()
    return {"global_kill_switch": not enabled}


# ────────────────────────────────────────────────────────────────────────────
# MULTI-ACCOUNT TRADE REPLICATION CONTROLLERS (MASTER ADMIN ONLY)
# ────────────────────────────────────────────────────────────────────────────

class MultiAccountToggleRequest(BaseModel):
    enabled: bool

class MultiAccountLinkRequest(BaseModel):
    target_user_id: int
    allocation_pct: Optional[float] = 1.0

class MultiAccountAccountToggleRequest(BaseModel):
    target_user_id: int
    is_active: bool

@app.get("/api/admin/multi-account/config")
def api_admin_get_multi_account_config(
    user: dict = Depends(require_master_admin),
    db=Depends(get_db)
):
    """Retrieve multi-account state, linked people, and available accounts."""
    from multi_account_manager import MultiAccountManager
    return MultiAccountManager.get_linked_accounts_status(db, master_user_id=user["uid"])

@app.post("/api/admin/multi-account/toggle")
def api_admin_toggle_multi_account(
    body: MultiAccountToggleRequest,
    user: dict = Depends(require_master_admin),
    db=Depends(get_db)
):
    """Enable or disable multi-account trade replication globally for Master Admin."""
    from multi_account_manager import MultiAccountManager
    enabled = MultiAccountManager.set_multi_account_enabled(db, body.enabled)
    return {"status": "success", "multi_account_enabled": enabled}

@app.post("/api/admin/multi-account/link")
def api_admin_link_account(
    body: MultiAccountLinkRequest,
    user: dict = Depends(require_master_admin),
    db=Depends(get_db)
):
    """Link a person / trading account to Master Admin."""
    from multi_account_manager import MultiAccountManager
    try:
        res = MultiAccountManager.link_account(
            db=db,
            master_user_id=user["uid"],
            target_user_id=body.target_user_id,
            allocation_pct=body.allocation_pct or 1.0
        )
        return res
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.delete("/api/admin/multi-account/unlink/{target_user_id}")
def api_admin_unlink_account(
    target_user_id: int,
    user: dict = Depends(require_master_admin),
    db=Depends(get_db)
):
    """Unlink a person / trading account."""
    from multi_account_manager import MultiAccountManager
    try:
        res = MultiAccountManager.unlink_account(
            db=db,
            master_user_id=user["uid"],
            target_user_id=target_user_id
        )
        return res
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/admin/multi-account/account-toggle")
def api_admin_toggle_account(
    body: MultiAccountAccountToggleRequest,
    user: dict = Depends(require_master_admin),
    db=Depends(get_db)
):
    """Enable or disable execution for a specific linked account."""
    from multi_account_manager import MultiAccountManager
    try:
        res = MultiAccountManager.toggle_account_active(
            db=db,
            master_user_id=user["uid"],
            target_user_id=body.target_user_id,
            is_active=body.is_active
        )
        return res
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))



# ────────────────────────────────────────────────────────────────────────────
# SUPER ADMIN CENTRALIZED CONTROL PANEL (SUPER ADMIN ONLY)
# ────────────────────────────────────────────────────────────────────────────

class SuperAdminCreateUserRequest(BaseModel):
    email: str
    username: str
    password: str
    role: Optional[str] = "user"
    is_active: Optional[bool] = True
    permissions: Optional[Dict[str, Any]] = None

class SuperAdminUpdateUserRequest(BaseModel):
    email: Optional[str] = None
    username: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None
    password: Optional[str] = None
    permissions: Optional[Dict[str, Any]] = None

class SuperAdminUpdateConfigRequest(BaseModel):
    trading_mode: Optional[str] = None
    capital: Optional[float] = None
    max_trades: Optional[int] = None
    daily_loss_cap: Optional[float] = None
    trailing_sl_mode: Optional[str] = None
    risk_per_trade: Optional[float] = None
    is_running: Optional[int] = None
    permissions: Optional[Dict[str, Any]] = None

class SuperAdminUpdateCredentialsRequest(BaseModel):
    upstox_client_id: Optional[str] = None
    upstox_client_secret: Optional[str] = None
    upstox_access_token: Optional[str] = None
    analytics_token: Optional[str] = None

@app.get("/api/super-admin/overview")
def api_super_admin_overview(user: dict = Depends(require_super_admin), db=Depends(get_db)):
    """Summary overview stats across all users, bots, and executions."""
    from super_admin_service import SuperAdminService
    return SuperAdminService.get_system_overview_stats(db)

@app.get("/api/super-admin/users")
def api_super_admin_users(user: dict = Depends(require_super_admin), db=Depends(get_db)):
    """Full list of registered users with Upstox status, trading activity, and roles."""
    from super_admin_service import SuperAdminService
    return SuperAdminService.get_all_users_summary(db)

@app.post("/api/super-admin/users")
def api_super_admin_create_user(req: SuperAdminCreateUserRequest, user: dict = Depends(require_super_admin), db=Depends(get_db)):
    """Provision a new user account with default isolated trading configuration."""
    from super_admin_service import SuperAdminService
    try:
        new_u = SuperAdminService.create_user_admin(
            db=db,
            email=req.email,
            username=req.username,
            password=req.password,
            role=req.role or "user",
            is_active=req.is_active if req.is_active is not None else True,
            permissions=req.permissions,
            admin_user_id=user["uid"]
        )
        return {"status": "success", "user": new_u}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/super-admin/users/{target_user_id}")
def api_super_admin_get_user(target_user_id: int, user: dict = Depends(require_super_admin), db=Depends(get_db)):
    """Get complete isolated configuration, linked accounts, and trade history for a specific user."""
    from super_admin_service import SuperAdminService
    details = SuperAdminService.get_user_full_details(db, target_user_id)
    if not details:
        raise HTTPException(status_code=404, detail="User not found")
    return details

@app.put("/api/super-admin/users/{target_user_id}")
def api_super_admin_update_user(target_user_id: int, req: SuperAdminUpdateUserRequest, user: dict = Depends(require_super_admin), db=Depends(get_db)):
    """Modify user credentials, role, status, or granular permissions."""
    from super_admin_service import SuperAdminService
    try:
        updated = SuperAdminService.update_user_admin(
            db=db,
            target_user_id=target_user_id,
            email=req.email,
            username=req.username,
            role=req.role,
            is_active=req.is_active,
            password=req.password,
            permissions=req.permissions,
            admin_user_id=user["uid"]
        )
        return {"status": "success", "user": updated}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.delete("/api/super-admin/users/{target_user_id}")
def api_super_admin_delete_user(target_user_id: int, user: dict = Depends(require_super_admin), db=Depends(get_db)):
    """Safely remove a user account and clean up isolated state."""
    from super_admin_service import SuperAdminService
    try:
        SuperAdminService.delete_user_admin(db, target_user_id, admin_user_id=user["uid"])
        return {"status": "success", "deleted": True, "user_id": target_user_id}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/super-admin/users/{target_user_id}/toggle-status")
def api_super_admin_toggle_user_status(target_user_id: int, user: dict = Depends(require_super_admin), db=Depends(get_db)):
    """Instantly activate or deactivate a user account."""
    from super_admin_service import SuperAdminService
    try:
        new_active = SuperAdminService.toggle_user_active_status(db, target_user_id, admin_user_id=user["uid"])
        return {"status": "success", "is_active": new_active, "user_id": target_user_id}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.put("/api/super-admin/users/{target_user_id}/config")
def api_super_admin_update_user_config(target_user_id: int, req: SuperAdminUpdateConfigRequest, user: dict = Depends(require_super_admin), db=Depends(get_db)):
    """Update risk management parameters, trading mode, capital, and feature-level access for a specific user."""
    from super_admin_service import SuperAdminService
    try:
        cfg_dict = {
            "trading_mode": req.trading_mode,
            "capital": req.capital,
            "max_trades": req.max_trades,
            "daily_loss_cap": req.daily_loss_cap,
            "trailing_sl_mode": req.trailing_sl_mode,
            "is_running": req.is_running
        }
        res = SuperAdminService.update_user_trading_config_and_permissions(
            db=db,
            target_user_id=target_user_id,
            config_data=cfg_dict,
            permissions_data=req.permissions,
            admin_user_id=user["uid"]
        )
        return {"status": "success", "config": res}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/super-admin/users/{target_user_id}/credentials")
def api_super_admin_get_credentials(target_user_id: int, user: dict = Depends(require_super_admin), db=Depends(get_db)):
    """Fetch status of all four individual API credentials for a user (safe: values are masked)."""
    from credential_service import credential_field_status
    status = credential_field_status(db, target_user_id)
    return {
        "status": "success",
        "user_id": target_user_id,
        "credentials": status,
        "configured_count": sum(1 for v in status.values() if v),
        "total_fields": 4
    }

@app.put("/api/super-admin/users/{target_user_id}/credentials")
async def api_super_admin_update_credentials(
    target_user_id: int,
    req: SuperAdminUpdateCredentialsRequest,
    user: dict = Depends(require_super_admin),
    db=Depends(get_db)
):
    """Configure any of the 4 API credentials individually: API Key, API Secret, Access Token, Analytics Token."""
    from super_admin_service import SuperAdminService
    try:
        res = SuperAdminService.update_user_credentials_admin(
            db=db,
            target_user_id=target_user_id,
            upstox_client_id=req.upstox_client_id,
            upstox_client_secret=req.upstox_client_secret,
            upstox_access_token=req.upstox_access_token,
            analytics_token=req.analytics_token,
            admin_user_id=user["uid"]
        )
        try:
            reload_bot_credentials(target_user_id, db)
        except Exception:
            pass
        return res
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update credentials: {str(e)}")

@app.get("/api/super-admin/roles")
def api_super_admin_roles(user: dict = Depends(require_super_admin)):
    """Catalog of supported roles and their default permission profiles."""
    from super_admin_service import DEFAULT_ROLE_PERMISSIONS
    return {
        "roles": [
            {
                "key": "super_admin",
                "name": "Super Admin",
                "badge": "👑 Super Admin",
                "description": "Full unconstrained administrative access to all users, settings, and trading systems.",
                "permissions": DEFAULT_ROLE_PERMISSIONS["super_admin"]
            },
            {
                "key": "master_admin",
                "name": "Master Admin",
                "badge": "🛡️ Master Admin",
                "description": "High-tier trading administrator with multi-account trade replication and higher limits.",
                "permissions": DEFAULT_ROLE_PERMISSIONS["master_admin"]
            },
            {
                "key": "user",
                "name": "Regular User",
                "badge": "👤 Trader",
                "description": "Standard trader with personal account execution and standard risk constraints.",
                "permissions": DEFAULT_ROLE_PERMISSIONS["user"]
            }
        ]
    }

@app.get("/api/super-admin/audit-logs")
def api_super_admin_audit_logs(limit: int = 100, user: dict = Depends(require_super_admin), db=Depends(get_db)):
    """Real-time system audit trail with user attribution."""
    from super_admin_service import SuperAdminService
    return SuperAdminService.get_system_audit_logs(db, limit=limit)


# ────────────────────────────────────────────────────────────────────────────
# v2 API ENDPOINTS — Watchlist, Signal Log, Feed Health, Pipeline Stats
# ────────────────────────────────────────────────────────────────────────────


@app.get("/api/watchlist/morning")
def api_get_morning_watchlist(user: dict = Depends(require_auth)):
    """Return today's morning watchlist."""
    from database import MorningWatchlist
    db = SessionLocal()
    try:
        from database import get_ist_date
        today = get_ist_date()
        rows = db.query(MorningWatchlist).filter(MorningWatchlist.date == today).all()
        return [{
            "symbol": r.symbol, "direction_bias": r.direction_bias,
            "vol_ratio_20day": r.vol_ratio_20day,
            "relative_strength_5day": r.relative_strength_5day,
        } for r in rows]
    finally:
        db.close()

@app.get("/api/watchlist/active")
def api_get_active_watchlist(user: dict = Depends(require_auth)):
    """Return today's active watchlist (non-removed stocks)."""
    from database import ActiveWatchlist
    db = SessionLocal()
    try:
        from database import get_ist_date
        today = get_ist_date()
        rows = db.query(ActiveWatchlist).filter(
            ActiveWatchlist.date == today,
            ActiveWatchlist.removed_at.is_(None)
        ).all()
        return [{
            "symbol": r.symbol, "direction_bias": r.direction_bias,
            "added_at": r.added_at.isoformat() if r.added_at else None,
        } for r in rows]
    finally:
        db.close()

@app.get("/api/signal-log")
def api_get_signal_log(user: dict = Depends(require_auth)):
    """Return last 50 signal log entries for the dashboard."""
    return _signal_log

@app.get("/api/feed-health")
def api_get_feed_health(user: dict = Depends(require_auth)):
    """Return feed freshness status for dashboard indicator."""
    return market_feed.get_feed_health_summary()

@app.get("/api/pipeline-stats")
def api_get_pipeline_stats(user: dict = Depends(require_auth)):
    """Return pipeline statistics for daily summary display."""
    return _pipeline_stats


# ── Backtesting & Walk-Forward Endpoints ─────────────────────────────────────
_last_backtest_report: dict = {}

class BacktestRequest(BaseModel):
    symbol: str = "RELIANCE"
    initial_capital: float = 25000.0
    daily_loss_limit: float = 1000.0
    risk_per_trade: float = 200.0
    days: int = 10
    trailing_sl_mode: str = "ADAPTIVE"

@app.post("/api/v2/backtest/run")
def api_run_backtest(req: BacktestRequest, user: dict = Depends(require_auth)):
    """Run an intraday backtest using the 5-layer pipeline and Indian cost model."""
    global _last_backtest_report
    from backtesting_engine import BacktestConfig, IndianIntradayBacktestEngine, BacktestReport
    from benchmark_comparison import generate_benchmark_dataset
    import dataclasses

    config = BacktestConfig(
        initial_capital=req.initial_capital,
        daily_loss_limit=req.daily_loss_limit,
        risk_per_trade=req.risk_per_trade,
        trailing_sl_mode=req.trailing_sl_mode
    )
    dataset = generate_benchmark_dataset(symbols=[req.symbol], days=req.days)
    df = dataset.get(req.symbol)
    if df is None or df.empty:
        raise HTTPException(status_code=400, detail=f"No data available for symbol {req.symbol}")

    engine = IndianIntradayBacktestEngine(config=config)
    report = engine.run(req.symbol, df)
    _last_backtest_report = dataclasses.asdict(report)
    return _last_backtest_report

@app.get("/api/v2/backtest/report")
def api_get_backtest_report(user: dict = Depends(require_auth)):
    """Return the most recently executed backtest report."""
    if not _last_backtest_report:
        return {"status": "no_report_available", "message": "Run a backtest first via POST /api/v2/backtest/run"}
    return _last_backtest_report

@app.post("/api/v2/backtest/walk-forward")
def api_run_walk_forward(req: BacktestRequest, user: dict = Depends(require_auth)):
    """Run chronological walk-forward validation (train/test out-of-sample)."""
    from backtesting_engine import BacktestConfig, IndianIntradayBacktestEngine, WalkForwardValidator
    from benchmark_comparison import generate_benchmark_dataset
    import dataclasses

    config = BacktestConfig(
        initial_capital=req.initial_capital,
        daily_loss_limit=req.daily_loss_limit,
        risk_per_trade=req.risk_per_trade,
        trailing_sl_mode=req.trailing_sl_mode
    )
    dataset = generate_benchmark_dataset(symbols=[req.symbol], days=req.days)
    df = dataset.get(req.symbol)
    if df is None or df.empty:
        raise HTTPException(status_code=400, detail=f"No data available for symbol {req.symbol}")

    engine = IndianIntradayBacktestEngine(config=config)
    validator = WalkForwardValidator(engine=engine)
    res = validator.run_out_of_sample(req.symbol, df, train_ratio=0.60)
    return {
        "symbol": res["symbol"],
        "train_period": res["train_period"],
        "test_period": res["test_period"],
        "in_sample": dataclasses.asdict(res["in_sample"]),
        "out_of_sample": dataclasses.asdict(res["out_of_sample"]),
        "robustness_score": res["robustness_score"],
        "is_overfitted": res["is_overfitted"]
    }

@app.get("/api/v2/backtest/benchmark")
def api_get_benchmark_comparison(user: dict = Depends(require_auth)):
    """Return reproducible Before-vs-After benchmark comparison."""
    from benchmark_comparison import run_benchmark_comparison
    return run_benchmark_comparison()




import logging
import datetime
import uuid
from config import Config
from database import SessionLocal, Trade, DailyStats, SystemLog, get_ist_now, get_ist_date, to_ist_iso
import pytz

IST = pytz.timezone('Asia/Kolkata')

# Dedicated logger for execution storage
trade_logger = logging.getLogger('trades_log')
trade_logger.setLevel(logging.INFO)
file_handler = logging.FileHandler('trades.log')
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
if not trade_logger.handlers:
    trade_logger.addHandler(file_handler)

logger = logging.getLogger(__name__)

class TradeManager:
    def __init__(self, user_id: int):
        self.user_id = user_id
        self.active_trades = []
        self.trade_history = []
        self._load_from_db()
        
    def _load_from_db(self):
        db = SessionLocal()
        try:
            today = get_ist_date()
            # Load OPEN trades for THIS user
            open_db_trades = db.query(Trade).filter(Trade.status == 'OPEN', Trade.user_id == self.user_id).all()
            for t in open_db_trades:
                try:
                    if t.trade_date and t.trade_date < today:
                        logger.warning(f"DB Recovery: Found stale OPEN trade {t.id} from {t.trade_date}. Auto-closing it.")
                        t.status = 'CLOSED'
                        t.exit_reason = 'STALE_EOD_CLEANUP'
                        t.exit_time = get_ist_now()
                        t.exit_price = t.entry_price
                        t.realized_pnl = 0.0
                        db.commit()
                        continue

                    # Recover if breakeven was triggered
                    is_buy = t.side.upper() == 'BUY'
                    breakeven_triggered = False
                    
                    sl = t.sl_price if t.sl_price is not None else 0.0
                    entry = t.entry_price if t.entry_price is not None else 0.0

                    if is_buy and sl >= entry:
                        breakeven_triggered = True
                    elif not is_buy and sl <= entry:
                        breakeven_triggered = True

                    self.active_trades.append({
                        'id': t.id,
                        'timestamp': to_ist_iso(t.entry_time) if t.entry_time else get_ist_now().isoformat(),
                        'symbol': t.symbol,
                        'side': t.side,
                        'entry_price': t.entry_price,
                        'current_price': t.entry_price, # placeholder until LTP fetch
                        'max_favorable_price': t.entry_price,
                        'qty': t.quantity,
                        'sl': t.sl_price,
                        'initial_sl': t.sl_price,
                        'target': t.target_price,
                        'target_hit': (t.target_price is None or t.target_price == 0.0),
                        'probability': 0.0,
                        'score': t.ai_score or 0.0,
                        'pnl': 0.0,
                        'status': 'OPEN',
                        'metadata': {},
                        'breakeven_triggered': breakeven_triggered,
                        'sl_order_id': None
                    })
                except Exception as loop_ex:
                    logger.error(f"Failed to recover specific trade {t.id} from DB: {loop_ex}")
                    continue
            
            # Load Today's CLOSED trades for THIS user
            closed_db_trades = db.query(Trade).filter(Trade.status == 'CLOSED', Trade.trade_date == today, Trade.user_id == self.user_id).all()
            for t in closed_db_trades:
                self.trade_history.append({
                    'id': t.id,
                    'timestamp': to_ist_iso(t.entry_time) if t.entry_time else get_ist_now().isoformat(),
                    'symbol': t.symbol,
                    'side': t.side,
                    'entry_price': t.entry_price,
                    'current_price': t.exit_price or 0.0,
                    'qty': t.quantity,
                    'sl': t.sl_price,
                    'initial_sl': t.sl_price,
                    'probability': 0.0,
                    'score': t.ai_score or 0.0,
                    'pnl': t.realized_pnl or 0.0,
                    'status': 'CLOSED',
                    'exit_reason': t.exit_reason,
                    'exit_time': to_ist_iso(t.exit_time) if t.exit_time else None,
                    'sl_order_id': None
                })

            # Calculate consecutive losses from today's closed trades (Bug Fix 3 recovery)
            self.consecutive_losses = 0
            sorted_trades = sorted(closed_db_trades, key=lambda t: t.exit_time or datetime.datetime.min)
            for t in sorted_trades:
                if (t.realized_pnl or 0) < 0:
                    self.consecutive_losses += 1
                elif (t.realized_pnl or 0) > 0:
                    self.consecutive_losses = 0

            # Update bot state if bot registered
            from user_bot_registry import get_bot
            bot = get_bot(self.user_id)
            if bot:
                bot.state.consecutive_losses = self.consecutive_losses

            logger.info(f"DB Recovery: Loaded {len(self.active_trades)} OPEN and {len(self.trade_history)} CLOSED trades for today. Consecutive losses: {self.consecutive_losses}")
        except Exception as e:
            logger.error(f"Failed to load from DB: {e}")
        finally:
            db.close()
        
    def add_trade(self, symbol: str, side: str, entry_price: float, qty: int, sl: float, target: float, probability: float, score: int, sl_order_id: str = None, metadata: dict = None):
        trade_id = str(uuid.uuid4())[:8]  # Shorter UUID for easy reading
        trade = {
            'id': trade_id,
            'timestamp': get_ist_now().isoformat(),
            'symbol': symbol,
            'side': side,
            'entry_price': entry_price,
            'current_price': entry_price, # initial
            'max_favorable_price': entry_price, # Added for Peak Trailing
            'qty': int(qty),
            'sl': sl,
            'initial_sl': sl,
            'target': target,
            'target_hit': False,
            'probability': probability,
            'score': score,
            'pnl': 0.0,
            'status': 'OPEN',
            'metadata': metadata or {},
            'sl_order_id': sl_order_id,
            'mfe': entry_price,
            'mae': entry_price
        }
        self.active_trades.append(trade)
        logger.info(f"Trade added: {trade}")
        trade_logger.info(f"EXECUTED NEW TRADE: [symbol={symbol}, side={side}, qty={qty}, entry={entry_price}, sl={sl}, target={target}, sl_order_id={sl_order_id}]")
        
        # Insert into Database
        db = SessionLocal()
        try:
            db_trade = Trade(
                id=trade_id,
                user_id=self.user_id,
                trade_date=get_ist_date(),
                symbol=symbol,
                side=side,
                quantity=qty,
                entry_price=entry_price,
                sl_price=sl,
                target_price=target,
                entry_time=get_ist_now(),
                status='OPEN',
                ai_score=float(score),
                mfe=entry_price,
                mae=entry_price,
                discovery_score=metadata.get('discovery_score') if metadata else None,
                discovery_rank=metadata.get('discovery_rank') if metadata else None
            )
            db.add(db_trade)
            db.commit()
        except Exception as e:
            logger.error(f"Failed to insert trade into DB: {e}")
        finally:
            db.close()
        
    def update_prices(self, current_data: dict, execution_engine=None, market_regime="UNKNOWN"):
        '''
        current_data is a dict of symbol: current_price
        '''
        for trade in list(self.active_trades):
            sym = trade['symbol']
            if sym in current_data:
                price = current_data[sym]
                trade['current_price'] = price
                
                # Track MFE and MAE
                if trade['side'] == 'BUY':
                    trade['mfe'] = max(trade.get('mfe', trade['entry_price']), price)
                    trade['mae'] = min(trade.get('mae', trade['entry_price']), price)
                else:
                    trade['mfe'] = min(trade.get('mfe', trade['entry_price']), price)
                    trade['mae'] = max(trade.get('mae', trade['entry_price']), price)
                
                # Calculate PnL based on side
                if trade['side'] == 'BUY':
                    trade['pnl'] = (price - trade['entry_price']) * trade['qty']
                elif trade['side'] == 'SELL':
                    trade['pnl'] = (trade['entry_price'] - price) * trade['qty']
                
                self.check_exit_conditions(trade, execution_engine)
                self.manage_trailing_sl(trade, execution_engine, market_regime)
                self.check_skipped_sl(trade, execution_engine)

    def check_skipped_sl(self, trade: dict, execution_engine=None):
        """Check if the stop loss trigger was breached but the order remains open on the exchange (Fix C)."""
        if not execution_engine:
            return
            
        # Paper trades do not have real stop loss orders
        if trade.get('metadata', {}).get('paper_trade', False):
            return
            
        if trade.get('status') != 'OPEN':
            return
            
        sl = trade['sl']
        price = trade['current_price']
        side = trade['side']
        sl_order_id = trade.get('sl_order_id')
        
        if not sl_order_id or str(sl_order_id).startswith("virtual_") or sl_order_id == "circuit_limit_blocked":
            return
            
        # Check if price has crossed past the Stop Loss trigger
        is_past_sl = False
        if side == 'BUY' and price <= sl:
            is_past_sl = True
        elif side == 'SELL' and price >= sl:
            is_past_sl = True
            
        if is_past_sl:
            # Throttle the status check to once every 2 seconds to avoid rate limits
            import time
            now = time.time()
            last_check = trade.get('last_sl_status_check_time', 0)
            if now - last_check < 2.0:
                return
            trade['last_sl_status_check_time'] = now
            
            try:
                logger.info(f"Skipped SL Monitor: Price is past SL ({price} vs {sl}) for {trade['symbol']}. Checking status of order {sl_order_id} on Upstox...")
                status_res = execution_engine.get_order_status(sl_order_id)
                if status_res and status_res.get('status') == 'success':
                    order_status = str(status_res.get('order_status', '')).upper()
                    logger.info(f"Skipped SL Monitor: Order status for {trade['symbol']} is {order_status}")
                    
                    # If order is still open or trigger pending on the exchange, it has been skipped!
                    if order_status in ('OPEN', 'TRIGGER_PENDING', 'VALIDATION_PENDING', 'PUT_ORDER_REQ_RECEIVED'):
                        logger.warning(f"⚠️ [SKIPPED SL DETECTED] Stop loss order {sl_order_id} was skipped for {trade['symbol']} (Market: {price}, SL: {sl}). Triggering Emergency Exit!")
                        self.trigger_emergency_exit(trade, execution_engine)
            except Exception as e:
                logger.error(f"Error checking order status in Skipped SL Monitor: {e}")

    def trigger_emergency_exit(self, trade: dict, execution_engine):
        """Execute a safe cancel-then-market-exit fail-safe routine to close an un-executed skipped stop loss position (Fix C)."""
        symbol = trade['symbol']
        sl_order_id = trade.get('sl_order_id')
        symbol_key = trade.get('instrument_token') or Config.SYMBOLS_MAPPING.get(symbol)
        
        if not symbol_key:
            logger.error(f"Cannot perform emergency exit: missing symbol key for {symbol}")
            return
            
        logger.warning(f"🚨 [EMERGENCY EXIT] Initiating emergency market exit for {symbol}...")
        
        # 1. Cancel the pending Stop Loss order
        if sl_order_id and not str(sl_order_id).startswith("virtual_"):
            logger.info(f"[EMERGENCY EXIT] Step 1: Canceling pending Stop Loss order {sl_order_id}...")
            cancel_res = execution_engine.cancel_order(sl_order_id)
            logger.info(f"[EMERGENCY EXIT] Cancel order response: {cancel_res}")
            
        # 2. Wait 1 second for cancellation confirmation/settlement
        import time
        time.sleep(1.0)
        
        # 3. Fetch positions from Upstox to verify quantity
        try:
            logger.info(f"[EMERGENCY EXIT] Step 2: Fetching live positions book from Upstox...")
            positions = execution_engine.get_positions()
            net_qty = 0
            for pos in positions:
                if pos.get('instrument_token') == symbol_key:
                    net_qty = int(pos.get('quantity', 0))
                    break
            
            logger.info(f"[EMERGENCY EXIT] Net position quantity for {symbol}: {net_qty}")
            
            # 4. Place standard market exit order if position still open
            if (trade['side'] == 'BUY' and net_qty > 0) or (trade['side'] == 'SELL' and net_qty < 0):
                abs_qty = abs(net_qty)
                counter_side = "SELL" if trade['side'] == "BUY" else "BUY"
                logger.warning(f"🚨 [EMERGENCY EXIT] Step 3: Placing standard MARKET order to close {abs_qty} shares of {symbol}...")
                
                place_res = execution_engine.place_order_with_retry(symbol_key, counter_side, abs_qty)
                logger.warning(f"[EMERGENCY EXIT] Market order response: {place_res}")
                
                # Mark trade as closed in database
                self.close_trade(trade, "EMERGENCY_SKIPPED_SL_EXIT", execution_engine)
                
                # Send critical Telegram alert
                try:
                    from telegram_alert import send_telegram
                    send_telegram(f"🚨 **CRITICAL: Emergency Exit Executed for {symbol}**!\n"
                                  f"Stop Loss Limit order was skipped by the market.\n"
                                  f"Bot successfully canceled SL and squared off {abs_qty} shares at market.")
                except Exception as ta_err:
                    logger.error(f"Failed to send emergency exit alert: {ta_err}")
            else:
                logger.info(f"[EMERGENCY EXIT] No open position found for {symbol} in broker positions. Marking trade as closed.")
                self.close_trade(trade, "SL Hit", execution_engine)
        except Exception as ex:
            logger.error(f"Failed to perform emergency exit sequence: {ex}")

    def check_exit_conditions(self, trade: dict, execution_engine=None):
        price = trade['current_price']
        sl = trade['sl']
        target = trade.get('target', 0.0)
        
        exit_trade = False
        exit_reason = ""
        
        if trade['side'] == 'BUY':
            if price <= sl:
                exit_trade = True
                exit_reason = "Trailing SL Hit" if trade.get('target_hit') else "SL Hit"
        elif trade['side'] == 'SELL':
            if price >= sl:
                exit_trade = True
                exit_reason = "Trailing SL Hit" if trade.get('target_hit') else "SL Hit"
                
        # Also check time >= 3:15 PM
        now = get_ist_now()
        if (now.hour == 15 and now.minute >= 15) or now.hour > 15:
            exit_trade = True
            exit_reason = "Time Exit (>= 3:15 PM)"

        if exit_trade:
            self.close_trade(trade, exit_reason, execution_engine)

    def manage_trailing_sl(self, trade: dict, execution_engine=None, market_regime="UNKNOWN"):
        """
        2-stage trailing Stop Loss logic (Section 5, updated with Fixes 2 & 3).
        1. Stage 1 (Breakeven): Triggered based on regime-adaptive R-multiple.
           Moves SL to entry +/- 0.05 * ATR (true zero-loss).
        2. Stage 2 (Active Trailing): Triggers at +2x ATR and trails at LTP - 1x ATR.
        """
        import market_feed
        # Prevent trailing if tick feed is stale
        if not market_feed.is_tick_fresh(trade['symbol'], max_age=30.0):
            return

        entry = trade['entry_price']
        qty = trade['qty']
        side = trade['side']
        price = trade['current_price']
        sl_order_id = trade.get('sl_order_id')
        metadata = trade.get('metadata', {})
        
        # Use initial ATR saved into trade on entry
        atr_14 = metadata.get('atr_14')
        if not atr_14:
            atr_pct = metadata.get('atr_pct', 0.5) / 100
            atr_14 = entry * atr_pct if entry > 0 else 10.0
            
        if atr_14 <= 0:
            return

        # Fix 3: Regime-Adaptive Breakeven Trigger
        active_regime = "UNKNOWN"
        regime_str = str(market_regime or "UNKNOWN").upper()
        if regime_str == "UNKNOWN":
            regime_str = str(metadata.get('market_regime', 'UNKNOWN')).upper()

        if "RANGE" in regime_str or "CHOP" in regime_str:
            active_regime = "RANGING"
            r_mult = 0.4
        elif "TREND" in regime_str:
            active_regime = "TRENDING"
            r_mult = 0.75
        else:
            active_regime = "UNKNOWN"
            r_mult = 0.5

        # Calculate initial risk R
        initial_sl = trade.get('initial_sl', trade['sl'])
        risk = abs(entry - initial_sl)
        
        # Calculate trigger offset (fallback to 1.0x ATR if risk is invalid)
        if risk > 0:
            trigger_offset = r_mult * risk
        else:
            trigger_offset = atr_14

        if 'breakeven_triggered' not in trade:
            trade['breakeven_triggered'] = False

        if 'target_hit' not in trade:
            trade['target_hit'] = (trade.get('target') is None or trade.get('target') == 0.0)

        new_sl = trade['sl']
        
        # Check target hit
        target = trade.get('target')
        if not trade['target_hit'] and target and target > 0.0:
            target_reached = False
            if side == 'BUY' and price >= target:
                target_reached = True
            elif side == 'SELL' and price <= target:
                target_reached = True
                
            if target_reached:
                trade['target_hit'] = True
                new_sl = target
                trade['target'] = None
                
                # Update DB to clear target and set SL to target price
                db = SessionLocal()
                try:
                    db_trade = db.query(Trade).filter(Trade.id == trade['id']).first()
                    if db_trade:
                        db_trade.target_price = None
                        db_trade.sl_price = target
                        db.commit()
                        logger.info(f"DB updated: trade {trade['symbol']} target cleared and SL set to target {target}.")
                except Exception as db_err:
                    db.rollback()
                    logger.error(f"Failed to clear target / update SL in DB: {db_err}")
                finally:
                    db.close()
                    
                logger.info(f"🎯 Target hit for {trade['symbol']} @ {price}! Setting SL to target price {target} and initiating aggressive trailing.")
                try:
                    from telegram_alert import send_telegram
                    send_telegram(f"🎯 **Target Hit for {trade['symbol']}**!\n"
                                  f"LTP: ₹{price:.2f}\n"
                                  f"Target: ₹{target:.2f} is now the new Stop Loss.\n"
                                  f"Switching to Aggressive Trailing Stop Loss.")
                except Exception as ta_err:
                    logger.error(f"Failed to send target hit alert: {ta_err}")

        # 1. 2-stage trailing calculation OR Aggressive Trailing if target was hit
        if trade['target_hit']:
            # Aggressive Trailing (LTP +/- 0.4x ATR)
            if side == 'BUY':
                candidate_sl = price - 0.4 * atr_14
                if candidate_sl > new_sl:
                    new_sl = candidate_sl
                    logger.info(f"Trailing SL (Aggressive): {trade['symbol']} @ {new_sl} (LTP={price})")
            else: # SELL
                candidate_sl = price + 0.4 * atr_14
                if candidate_sl < new_sl:
                    new_sl = candidate_sl
                    logger.info(f"Trailing SL (Aggressive): {trade['symbol']} @ {new_sl} (LTP={price})")
        else:
            if side == 'BUY':
                # Stage 1: Breakeven (Regime-adaptive R-multiple)
                if not trade['breakeven_triggered'] and price >= entry + trigger_offset:
                    new_sl = entry + (0.05 * atr_14)  # Fix 2: True zero-loss breakeven
                    trade['breakeven_triggered'] = True
                    logger.info(
                        f"Trailing SL Stage 1: Breakeven triggered for {trade['symbol']}. "
                        f"ATR={atr_14:.2f}, R-multiple={r_mult}, new_SL={new_sl:.2f}, "
                        f"regime={active_regime} ({regime_str})"
                    )
                
                # Stage 2: Active Trailing (+2x ATR)
                if price >= entry + 2 * atr_14:
                    candidate_sl = price - atr_14
                    # Only move SL up
                    if candidate_sl > new_sl:
                        new_sl = candidate_sl
                        logger.info(f"Trailing SL Stage 2: Active trailing for {trade['symbol']} @ {new_sl}")
            else: # SELL
                # Stage 1: Breakeven (Regime-adaptive R-multiple)
                if not trade['breakeven_triggered'] and price <= entry - trigger_offset:
                    new_sl = entry - (0.05 * atr_14)  # Fix 2: True zero-loss breakeven
                    trade['breakeven_triggered'] = True
                    logger.info(
                        f"Trailing SL Stage 1: Breakeven triggered for {trade['symbol']}. "
                        f"ATR={atr_14:.2f}, R-multiple={r_mult}, new_SL={new_sl:.2f}, "
                        f"regime={active_regime} ({regime_str})"
                    )
                
                # Stage 2: Active Trailing (+2x ATR)
                if price <= entry - 2 * atr_14:
                    candidate_sl = price + atr_14
                    # Only move SL down
                    if candidate_sl < new_sl:
                        new_sl = candidate_sl
                        logger.info(f"Trailing SL Stage 2: Active trailing for {trade['symbol']} @ {new_sl}")

        # Rounded to tick size (0.05)
        rounded_new_sl = round(new_sl * 20) / 20.0
        rounded_current_sl = round(trade['sl'] * 20) / 20.0

        # If a physically new SL triggers, process it on the server
        is_paper = trade.get('metadata', {}).get('paper_trade', False)

        # If a physically new SL triggers, process it on the server (unless it is a paper trade)
        if abs(rounded_new_sl - rounded_current_sl) >= 0.05:
            if execution_engine and not is_paper:
                symbol_key = trade.get('instrument_token') or Config.SYMBOLS_MAPPING.get(trade['symbol'])
                
                # Check if we have a valid, active stop loss order ID on Upstox
                has_active_sl = (sl_order_id and 
                                 not str(sl_order_id).startswith("virtual_") and 
                                 sl_order_id != "circuit_limit_blocked")
                
                if has_active_sl:
                    # 1. Atomic Order Modification
                    logger.info(f"Modifying existing Stop Loss order {sl_order_id} for {trade['symbol']} to trigger at {rounded_new_sl}...")
                    res = execution_engine.modify_order(
                        order_id=sl_order_id,
                        new_price=0,  # Auto-calculated limit price with correct 0.1% buffer
                        new_trigger=rounded_new_sl,
                        qty=qty,
                        order_type="SL",
                        transaction_type=side
                    )
                    
                    # Binary search recovery: if modification fails, search intermediate SL trigger prices
                    if res.get('status') != 'success':
                        logger.warning(f"Trailing SL modification to {rounded_new_sl} failed: {res.get('reason', 'Unknown error')}. Attempting binary search recovery...")
                        price_diff = rounded_new_sl - rounded_current_sl
                        
                        if abs(price_diff) >= 0.20:
                            best_accepted = rounded_current_sl
                            l = rounded_current_sl
                            h = rounded_new_sl
                            
                            # Perform 4 binary search iterations to narrow down maximum allowable SL
                            for iteration in range(4):
                                mid = (l + h) / 2.0
                                mid_rounded = round(mid * 20) / 20.0
                                
                                if mid_rounded == round(l * 20) / 20.0 or mid_rounded == round(h * 20) / 20.0:
                                    break
                                    
                                logger.info(f"[BINARY SEARCH SL] Step {iteration+1}: Trying intermediate trigger {mid_rounded} between {l} and {h}...")
                                step_res = execution_engine.modify_order(
                                    order_id=sl_order_id,
                                    new_price=0,
                                    new_trigger=mid_rounded,
                                    qty=qty,
                                    order_type="SL",
                                    transaction_type=side
                                )
                                
                                if step_res.get('status') == 'success':
                                    best_accepted = mid_rounded
                                    res = step_res
                                    l = mid_rounded
                                else:
                                    h = mid_rounded
                                    
                            if best_accepted != rounded_current_sl:
                                rounded_new_sl = best_accepted
                                logger.info(f"Successfully recovered: Trailing SL modified to maximum accepted trigger {rounded_new_sl} for {trade['symbol']}")
                            else:
                                logger.error(f"Binary search recovery failed: All intermediate SL modifications rejected. Keeping original SL at {rounded_current_sl}.")
                        else:
                            logger.error(f"Trailing SL modification failed. Gap is too small for binary search recovery. Keeping original SL at {rounded_current_sl}.")
                else:
                    # 2. Place new Stop Loss order
                    logger.info(f"No active SL order found for {trade['symbol']}. Placing a new Stop Loss order at {rounded_new_sl}...")
                    res = execution_engine.place_stop_loss(symbol_key, side, qty, rounded_new_sl)
                
                # Check for circuit limit hit
                if res.get('status') == 'circuit_limit':
                    from telegram_alert import alert_circuit_limit_hit
                    alert_circuit_limit_hit(trade['symbol'], side, qty, rounded_new_sl, "trailing stop loss adjustment")
                    trade['manual_exit_required'] = True
                    trade['sl'] = rounded_new_sl
                    logger.error(f"[CIRCUIT LIMIT] Trailing SL for {trade['symbol']} hit circuit limit: {res.get('reason')}")
                
                # For virtual trades or successful modifications/placements
                elif res.get('status') == 'success' or (sl_order_id and str(sl_order_id).startswith("virtual_")):
                    new_sl_order_id = res.get('order_id') or sl_order_id
                    if not new_sl_order_id and str(sl_order_id).startswith("virtual_"):
                        new_sl_order_id = f"virtual_sl_{trade['symbol']}_{str(uuid.uuid4())[:6]}"
                        
                    trade['sl_order_id'] = new_sl_order_id
                    trade['sl'] = rounded_new_sl
                    
                    # Update DB
                    db = SessionLocal()
                    try:
                        db_trade = db.query(Trade).filter(Trade.id == trade['id']).first()
                        if db_trade:
                            db_trade.sl_price = rounded_new_sl
                            db_trade.sl_order_id = new_sl_order_id
                            db.commit()
                    except Exception as e:
                        logger.error(f"Failed to update SL in DB: {e}")
                    finally:
                        db.close()
                        
                    logger.info(f"Trailing SL updated successfully on server for {trade['symbol']} to {rounded_new_sl}")
                    
                    # Dispatch alert
                    try:
                        from telegram_alert import alert_trailing_sl_updated
                        stage = "BREAKEVEN" if rounded_new_sl == entry else "TRAILING"
                        alert_trailing_sl_updated(trade['symbol'], stage, rounded_current_sl, rounded_new_sl, price)
                    except Exception as ta_err:
                        logger.error(f"Failed to send trailing SL alert: {ta_err}")
                else:
                    logger.error(f"Failed to update trailed SL for {trade['symbol']}: {res.get('reason', 'Unknown error')}")
            else:
                trade_logger.info(f"TRAILED {'PAPER' if is_paper else 'LOCAL'} SL (2-Stage): {trade['symbol']} | Old: {rounded_current_sl} | New: {rounded_new_sl}")
                trade['sl'] = rounded_new_sl
                # Dispatch local trailing alert
                try:
                    from telegram_alert import alert_trailing_sl_updated
                    stage = "BREAKEVEN" if rounded_new_sl == entry else "TRAILING"
                    alert_trailing_sl_updated(trade['symbol'], stage, rounded_current_sl, rounded_new_sl, price)
                except Exception as ta_err:
                    logger.error(f"Failed to send trailing SL alert: {ta_err}")

    def close_trade(self, trade: dict, reason: str, execution_engine=None):
        trade['status'] = 'CLOSED'
        trade['exit_reason'] = reason
        trade['exit_time'] = get_ist_now().isoformat()
        
        sl_order_id = trade.get('sl_order_id')
        is_paper = trade.get('metadata', {}).get('paper_trade', False)
        
        if execution_engine and not is_paper:
            # BUG FIX 3: Check is_virtual from trade metadata FIRST.
            # Previously, only is_virtual_sl (derived from SL order ID prefix) was checked.
            # But if a rejected Upstox SL order returned a real-looking order ID (due to Bug 2),
            # is_virtual_sl would be False for virtual trades, causing real square-off
            # orders to be placed at 3:15 PM against non-existent positions.
            is_virtual_trade = trade.get('metadata', {}).get('is_virtual', False)
            if is_virtual_trade:
                logger.info(f"Virtual trade close for {trade['symbol']} ({reason}) — skipping all Upstox order actions.")
            else:
                # 1. Cancel pending SL order on Upstox (skip if SL Hit because it's already filled/completed)
                if reason != "SL Hit" and sl_order_id and not str(sl_order_id).startswith("virtual_"):
                    try:
                        logger.info(f"Canceling pending SL order {sl_order_id} for {trade['symbol']} due to trade close ({reason})")
                        execution_engine.cancel_order(sl_order_id)
                    except Exception as e:
                        logger.error(f"Failed to cancel {sl_order_id}: {e}")

                # 2. Place actual market exit order so it logs in Upstox
                is_virtual_sl = not sl_order_id or str(sl_order_id).startswith("virtual_")
                skip_market_exit = False

                if reason == "EMERGENCY_SKIPPED_SL_EXIT":
                    skip_market_exit = True
                elif reason == "SL Hit" and not is_virtual_sl:
                    # Broker already executed the real SL order
                    skip_market_exit = True

                if not skip_market_exit:
                    symbol_key = trade.get('instrument_token') or Config.SYMBOLS_MAPPING.get(trade['symbol'])
                    if symbol_key:
                        counter_side = "SELL" if trade['side'] == "BUY" else "BUY"
                        logger.info(f"Submitting square-off order to Upstox for {trade['symbol']} ({reason}, virtual_sl={is_virtual_sl})")
                        try:
                            execution_engine.place_order_with_retry(symbol_key, counter_side, trade['qty'], order_type='MARKET')
                        except Exception as e:
                            logger.error(f"Failed to place exit order on broker: {e}")

        
        # Consecutive Losses Tracking (Now isolated by TradeManager instance)
        if not hasattr(self, 'consecutive_losses'):
            self.consecutive_losses = 0
            
        pnl_val = trade.get('pnl', 0.0)
        if pnl_val < 0:
            self.consecutive_losses += 1
            logger.info(f"[USER {self.user_id}] PnL negative. Consecutive losses count: {self.consecutive_losses}")
        elif trade['pnl'] > 0:
            self.consecutive_losses = 0
            logger.info(f"[USER {self.user_id}] PnL positive. Consecutive losses reset to 0.")

        # Update bot state if bot registered
        from user_bot_registry import get_bot
        bot = get_bot(self.user_id)
        if bot:
            bot.state.consecutive_losses = self.consecutive_losses
            
        # Move lists
        self.trade_history.append(trade)
        if trade in self.active_trades:
            self.active_trades.remove(trade)

        # If consecutive losses reach 2, pause trading
        if self.consecutive_losses >= Config.MAX_CONSECUTIVE_LOSSES:
            from telegram_alert import alert_consecutive_losses
            if bot:
                bot.state.is_running = False
                bot.state.status = "STOPPED"
                bot.state.trading_halted = True
                logger.warning(f"[USER {self.user_id}] Pausing bot due to {self.consecutive_losses} consecutive losses.")
            alert_consecutive_losses(self.user_id)

        # Session P&L Circuit Breaker
        session_pnl = self.get_daily_pnl()
        active_capital = getattr(bot.state, 'active_capital', Config.MANUAL_CAPITAL) if bot else Config.MANUAL_CAPITAL
        daily_drawdown_limit = -(active_capital * 0.5 / 100) # 0.5% limit
        if session_pnl <= daily_drawdown_limit:
            if bot and not getattr(bot.state, 'trading_halted', False):
                bot.state.is_running = False
                bot.state.status = "STOPPED"
                bot.state.trading_halted = True
                logger.warning(f"[USER {self.user_id}] Session P&L {session_pnl} hit drawdown limit {daily_drawdown_limit}. Halting.")
                from telegram_alert import send_telegram
                send_telegram(f"🚨 Daily drawdown limit hit (P&L: {session_pnl:.2f}) — trading paused for the day.")
            
        logger.info(f"Trade closed: {trade['id']}, Reason: {reason}, PnL: {trade['pnl']}, Consecutive Losses: {self.consecutive_losses}")
        trade_logger.info(f"CLOSED TRADE: [id={trade['id']}, symbol={trade['symbol']}, reason={reason}, final_pnl={trade['pnl']}, exit_price={trade['current_price']}]")

        # Telegram Alert for Exit
        try:
            from telegram_alert import alert_trade_exit
            alert_trade_exit(trade['symbol'], reason, trade['entry_price'], trade['current_price'], trade['pnl'])
        except Exception as ta_err:
            logger.error(f"Failed to send trade exit alert: {ta_err}")

        # Update Database
        db = SessionLocal()
        try:
            db_trade = db.query(Trade).filter(Trade.id == trade['id'], Trade.user_id == self.user_id).first()
            if db_trade:
                db_trade.status = 'CLOSED'
                db_trade.exit_price = float(trade['current_price'])
                db_trade.exit_time = get_ist_now()
                db_trade.exit_reason = reason
                db_trade.realized_pnl = float(trade['pnl'])
                db_trade.sl_price = float(trade['sl'])
                db_trade.mfe = float(trade.get('mfe', trade['entry_price']))
                db_trade.mae = float(trade.get('mae', trade['entry_price']))
                db.commit()
                
            # Update Daily Stats aggregate for THIS user
            today = get_ist_date()
            stats = db.query(DailyStats).filter(DailyStats.session_date == today, DailyStats.user_id == self.user_id).first()
            if not stats:
                from database import UserConfig
                user_cfg = db.query(UserConfig).filter(UserConfig.user_id == self.user_id).first()
                initial_capital = user_cfg.manual_capital if user_cfg else Config.MANUAL_CAPITAL
                stats = DailyStats(
                    session_date=today, user_id=self.user_id,
                    start_capital=initial_capital, end_capital=initial_capital,
                    total_trades_taken=0, winning_trades=0, net_realized_pnl=0.0
                )
                db.add(stats)
            
            stats.total_trades_taken += 1
            if trade['pnl'] > 0:
                stats.winning_trades += 1
            stats.net_realized_pnl += float(trade['pnl'])
            stats.end_capital += float(trade['pnl'])
            db.commit()
        except Exception as e:
            logger.error(f"Failed to update trade exit in DB: {e}")
        finally:
            db.close()

    def get_daily_pnl(self) -> float:
        from sqlalchemy import func
        today = get_ist_date()
        db = SessionLocal()
        try:
            result = db.query(func.sum(Trade.realized_pnl)).filter(
                Trade.user_id == self.user_id,
                Trade.trade_date == today,
                Trade.status == 'CLOSED'
            ).scalar()
            closed_pnl = float(result or 0.0)
        except Exception as e:
            logger.error(f"Error querying daily closed PnL from DB: {e}")
            # Fallback to local history
            closed_pnl = sum([t['pnl'] for t in self.trade_history if t.get('exit_time') and t.get('exit_time')[:10] == get_ist_now().isoformat()[:10]])
        finally:
            db.close()
        active_pnl = sum([t['pnl'] for t in self.active_trades])
        return closed_pnl + active_pnl

    def get_trades_taken_today(self) -> int:
        today = get_ist_date()
        db = SessionLocal()
        try:
            count = db.query(Trade).filter(
                Trade.user_id == self.user_id,
                Trade.trade_date == today
            ).count()
        except Exception as e:
            logger.error(f"Error querying trades count from DB: {e}")
            count = len([t for t in self.trade_history + self.active_trades if t['timestamp'][:10] == get_ist_now().isoformat()[:10]])
        finally:
            db.close()
        return count

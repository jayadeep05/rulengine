import logging
import datetime
import uuid
from config import Config
from database import SessionLocal, Trade, DailyStats, SystemLog, get_ist_now, get_ist_date
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
    def __init__(self):
        self.active_trades = []
        self.trade_history = []
        self._load_from_db()
        
    def _load_from_db(self):
        db = SessionLocal()
        try:
            today = get_ist_date()
            # Load OPEN trades
            open_db_trades = db.query(Trade).filter(Trade.status == 'OPEN').all()
            for t in open_db_trades:
                self.active_trades.append({
                    'id': t.id,
                    'timestamp': t.entry_time.isoformat() if t.entry_time else get_ist_now().isoformat(),
                    'symbol': t.symbol,
                    'side': t.side,
                    'entry_price': t.entry_price,
                    'current_price': t.entry_price, # placeholder until LTP fetch
                    'max_favorable_price': t.entry_price,
                    'qty': t.quantity,
                    'sl': t.sl_price,
                    'target': t.target_price,
                    'probability': 0.0,
                    'score': t.ai_score or 0.0,
                    'pnl': 0.0,
                    'status': 'OPEN',
                    'sl_order_id': None # Might need re-fetching, but assuming UI reset
                })
            
            # Load Today's CLOSED trades
            closed_db_trades = db.query(Trade).filter(Trade.status == 'CLOSED', Trade.trade_date == today).all()
            for t in closed_db_trades:
                self.trade_history.append({
                    'id': t.id,
                    'timestamp': t.entry_time.isoformat() if t.entry_time else get_ist_now().isoformat(),
                    'symbol': t.symbol,
                    'side': t.side,
                    'entry_price': t.entry_price,
                    'current_price': t.exit_price or 0.0,
                    'qty': t.quantity,
                    'sl': t.sl_price,
                    'target': t.target_price,
                    'probability': 0.0,
                    'score': t.ai_score or 0.0,
                    'pnl': t.realized_pnl or 0.0,
                    'status': 'CLOSED',
                    'exit_reason': t.exit_reason,
                    'exit_time': t.exit_time.isoformat() if t.exit_time else None,
                    'sl_order_id': None
                })
            logger.info(f"DB Recovery: Loaded {len(self.active_trades)} OPEN and {len(self.trade_history)} CLOSED trades for today.")
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
            'target': target,
            'probability': probability,
            'score': score,
            'pnl': 0.0,
            'status': 'OPEN',
            'metadata': metadata or {},
            'sl_order_id': sl_order_id
        }
        self.active_trades.append(trade)
        logger.info(f"Trade added: {trade}")
        trade_logger.info(f"EXECUTED NEW TRADE: [symbol={symbol}, side={side}, qty={qty}, entry={entry_price}, sl={sl}, target={target}, sl_order_id={sl_order_id}]")
        
        # Insert into Database
        db = SessionLocal()
        try:
            db_trade = Trade(
                id=trade_id,
                trade_date=get_ist_date(),
                symbol=symbol,
                side=side,
                quantity=qty,
                entry_price=entry_price,
                sl_price=sl,
                target_price=target,
                entry_time=get_ist_now(),
                status='OPEN',
                ai_score=float(score)
            )
            db.add(db_trade)
            db.commit()
        except Exception as e:
            logger.error(f"Failed to insert trade into DB: {e}")
        finally:
            db.close()
        
    def update_prices(self, current_data: dict, execution_engine=None):
        '''
        current_data is a dict of symbol: current_price
        '''
        for trade in self.active_trades:
            sym = trade['symbol']
            if sym in current_data:
                price = current_data[sym]
                trade['current_price'] = price
                
                # Calculate PnL based on side
                if trade['side'] == 'BUY':
                    trade['pnl'] = (price - trade['entry_price']) * trade['qty']
                elif trade['side'] == 'SELL':
                    trade['pnl'] = (trade['entry_price'] - price) * trade['qty']
                
                self.check_exit_conditions(trade)
                self.manage_trailing_sl(trade, execution_engine)

    def check_exit_conditions(self, trade: dict):
        price = trade['current_price']
        sl = trade['sl']
        target = trade['target']
        
        exit_trade = False
        exit_reason = ""
        
        if trade['side'] == 'BUY':
            if price <= sl:
                exit_trade = True
                exit_reason = "SL Hit"
            elif price >= target:
                exit_trade = True
                exit_reason = "Target Hit"
        elif trade['side'] == 'SELL':
            if price >= sl:
                exit_trade = True
                exit_reason = "SL Hit"
            elif price <= target:
                exit_trade = True
                exit_reason = "Target Hit"
                
        # Also check time > 3:15 PM
        now = get_ist_now()
        if now.hour == 15 and now.minute >= 15:
            exit_trade = True
            exit_reason = "Time Exit (> 3:15 PM)"

        if exit_trade:
            self.close_trade(trade, exit_reason)

    def manage_trailing_sl(self, trade: dict, execution_engine=None):
        '''
        ADAPTIVE ATR TRAILING SL (Phase 3)
        '''
        entry = trade['entry_price']
        qty = trade['qty']
        side = trade['side']
        price = trade['current_price']
        sl_order_id = trade.get('sl_order_id')
        metadata = trade.get('metadata', {})
        
        mkt_condition = metadata.get('market_condition', 'SIDEWAYS')
        # Use initial ATR saved entirely into trade on entry
        atr_14 = metadata.get('atr_14')
        if not atr_14:
            atr_pct = metadata.get('atr_pct', 0.5) / 100  # Stored as % (e.g. 0.5)
            atr_14 = entry * atr_pct if entry > 0 else 10.0
            
        if atr_14 <= 0: return
        
        # Track Peak Price
        max_p = trade.get('max_favorable_price', entry)
        if side == 'BUY': max_p = max(max_p, price)
        else: max_p = min(max_p, price) if max_p > 0 else price
        trade['max_favorable_price'] = max_p
        
        # Determine Multiplier
        trailing_mult = 2.5 if mkt_condition == 'TRENDING' else 1.2
        
        new_sl = trade['sl']
        if side == 'BUY':
            candidate_sl = max_p - (trailing_mult * atr_14)
            # Only move SL up
            if candidate_sl > trade['sl']:
                new_sl = candidate_sl
        else:
            candidate_sl = max_p + (trailing_mult * atr_14)
            # Only move SL down
            if candidate_sl < trade['sl']:
                new_sl = candidate_sl

        # If a physically new SL triggers, process it on the server
        if new_sl and abs(new_sl - trade['sl']) > 0.05: # Prevent micro-modifications
            if execution_engine and sl_order_id:
                rounded_trigger = round(new_sl * 20) / 20.0
                mod_res = execution_engine.modify_order(
                    order_id=sl_order_id, 
                    new_price=0, 
                    new_trigger=rounded_trigger, 
                    qty=qty, 
                    order_type="SL-M"
                )
                if mod_res.get('status') == 'success':
                    trade_logger.info(f"TRAILED PHYSICAL SL: {trade['symbol']} (Mult: {trailing_mult}x) | Old: {trade['sl']} | New: {new_sl}")
                    trade['sl'] = new_sl
                else:
                    logger.error(f"Failed to trail SL physically via Upstox for {trade['symbol']}")
            else:
                trade_logger.info(f"TRAILED LOCAL SL: {trade['symbol']} (Mult: {trailing_mult}x) | Old: {trade['sl']} | New: {new_sl}")
                trade['sl'] = new_sl

    def close_trade(self, trade: dict, reason: str):
        trade['status'] = 'CLOSED'
        trade['exit_reason'] = reason
        trade['exit_time'] = get_ist_now().isoformat()
        
        # Overtrading Control (Phase 4): Consecutive Losses Tracking
        from config import SystemState
        if not hasattr(SystemState, 'consecutive_losses'):
            SystemState.consecutive_losses = 0
            
        if trade['pnl'] < 0:
            SystemState.consecutive_losses += 1
            if SystemState.consecutive_losses >= 2:
                SystemState.loss_cooldown_until = datetime.datetime.now(IST) + datetime.timedelta(minutes=30)
                logger.warning("2 Consecutive losses hit! Global Cooldown enabled for 30 minutes.")
        else:
            if trade['pnl'] > 0:
                SystemState.consecutive_losses = 0 # reset on win

        # Move lists
        self.trade_history.append(trade)
        if trade in self.active_trades:
            self.active_trades.remove(trade)
            
        logger.info(f"Trade closed: {trade['id']}, Reason: {reason}, PnL: {trade['pnl']}, Consecutive Losses: {getattr(SystemState, 'consecutive_losses', 0)}")
        trade_logger.info(f"CLOSED TRADE: [id={trade['id']}, symbol={trade['symbol']}, reason={reason}, final_pnl={trade['pnl']}, exit_price={trade['current_price']}]")

        # Update Database
        db = SessionLocal()
        try:
            db_trade = db.query(Trade).filter(Trade.id == trade['id']).first()
            if db_trade:
                db_trade.status = 'CLOSED'
                db_trade.exit_price = float(trade['current_price'])
                db_trade.exit_time = get_ist_now()
                db_trade.exit_reason = reason
                db_trade.realized_pnl = float(trade['pnl'])
                db_trade.sl_price = float(trade['sl'])
                db.commit()
                
            # Update Daily Stats aggregate
            today = get_ist_date()
            stats = db.query(DailyStats).filter(DailyStats.session_date == today).first()
            if not stats:
                stats = DailyStats(
                    session_date=today, start_capital=Config.MANUAL_CAPITAL, end_capital=Config.MANUAL_CAPITAL,
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
        history_pnl = sum([t['pnl'] for t in self.trade_history if t['timestamp'][:10] == get_ist_now().isoformat()[:10]])
        active_pnl = sum([t['pnl'] for t in self.active_trades])
        return history_pnl + active_pnl

    def get_trades_taken_today(self) -> int:
        count = 0
        today_str = get_ist_now().isoformat()[:10]
        for t in self.trade_history + self.active_trades:
            if t['timestamp'][:10] == today_str:
                count += 1
        return count

import logging
import datetime

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
        
    def add_trade(self, symbol: str, side: str, entry_price: float, qty: int, sl: float, target: float, probability: float, score: int, sl_order_id: str = None):
        trade = {
            'id': f"{symbol}-{len(self.trade_history) + len(self.active_trades)}",
            'timestamp': datetime.datetime.now().isoformat(),
            'symbol': symbol,
            'side': side,
            'entry_price': entry_price,
            'current_price': entry_price, # initial
            'qty': qty,
            'sl': sl,
            'target': target,
            'probability': probability,
            'score': score,
            'pnl': 0.0,
            'status': 'OPEN',
            'sl_order_id': sl_order_id
        }
        self.active_trades.append(trade)
        logger.info(f"Trade added: {trade}")
        trade_logger.info(f"EXECUTED NEW TRADE: [symbol={symbol}, side={side}, qty={qty}, entry={entry_price}, sl={sl}, target={target}, sl_order_id={sl_order_id}]")
        
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
        now = datetime.datetime.now()
        if now.hour == 15 and now.minute >= 15:
            exit_trade = True
            exit_reason = "Time Exit (> 3:15 PM)"

        if exit_trade:
            self.close_trade(trade, exit_reason)

    def manage_trailing_sl(self, trade: dict, execution_engine=None):
        '''
        Move SL to breakeven at +1R
        Trail after +1.5R 
        '''
        entry = trade['entry_price']
        qty = trade['qty']
        side = trade['side']
        price = trade['current_price']
        sl_order_id = trade.get('sl_order_id')
        
        # Original sl should be recorded theoretically, here we approximate R based on entry - original SL
        risk = abs(entry - trade['sl'])
        if risk == 0:
            return
            
        r_multiple = 0
        if side == 'BUY':
            r_multiple = (price - entry) / risk
        else:
            r_multiple = (entry - price) / risk
            
        new_sl = None
        # Move SL to breakeven
        if r_multiple >= 1.0 and r_multiple < 1.5:
            if side == 'BUY' and trade['sl'] < entry:
                new_sl = entry
            elif side == 'SELL' and trade['sl'] > entry:
                new_sl = entry
                
        # Trail after +1.5R - Simple trailing logic: lock in +1R
        if r_multiple >= 1.5:
            if side == 'BUY':
                candidate_sl = entry + risk
                if trade['sl'] < candidate_sl:
                    new_sl = candidate_sl
            else:
                candidate_sl = entry - risk
                if trade['sl'] > candidate_sl:
                    new_sl = candidate_sl

        # If a physically new SL triggers, process it on the server
        if new_sl and new_sl != trade['sl']:
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
                    trade_logger.info(f"TRAILED PHYSICAL SL: {trade['symbol']} | Old SL: {trade['sl']} | New SL: {new_sl}")
                    trade['sl'] = new_sl
                    logger.info(f"Successfully trailing SL to {new_sl} for {trade['symbol']}")
                else:
                    logger.error(f"Failed to trail SL physically via Upstox for {trade['symbol']}")
            else:
                trade_logger.info(f"TRAILED LOCAL SL: {trade['symbol']} | Old SL: {trade['sl']} | New SL: {new_sl}")
                trade['sl'] = new_sl
                logger.info(f"Trailing local SL to {new_sl} for {trade['symbol']}")

    def close_trade(self, trade: dict, reason: str):
        trade['status'] = 'CLOSED'
        trade['exit_reason'] = reason
        trade['exit_time'] = datetime.datetime.now().isoformat()
        self.trade_history.append(trade)
        self.active_trades.remove(trade)
        logger.info(f"Trade closed: {trade['id']}, Reason: {reason}, PnL: {trade['pnl']}")
        trade_logger.info(f"CLOSED TRADE: [id={trade['id']}, symbol={trade['symbol']}, reason={reason}, final_pnl={trade['pnl']}, exit_price={trade['current_price']}]")

    def get_daily_pnl(self) -> float:
        history_pnl = sum([t['pnl'] for t in self.trade_history if t['timestamp'][:10] == datetime.datetime.now().isoformat()[:10]])
        active_pnl = sum([t['pnl'] for t in self.active_trades])
        return history_pnl + active_pnl

    def get_trades_taken_today(self) -> int:
        count = 0
        today_str = datetime.datetime.now().isoformat()[:10]
        for t in self.trade_history + self.active_trades:
            if t['timestamp'][:10] == today_str:
                count += 1
        return count

import asyncio
import logging
from datetime import datetime
import pytz

IST = pytz.timezone('Asia/Kolkata')
logger = logging.getLogger(__name__)

class DualStrategyEngine:
    def __init__(self, execution_engine):
        self.ee = execution_engine
        self.is_running = False
        self.state = {
            "status": "STOPPED", # STOPPED, RUNNING, LEG_A_CLOSED, CLOSED
            "exec_mode": "MANUAL",
            "instrument_A": "",
            "instrument_B": "",
            "qty_A": 0,
            "qty_B": 0,
            "entry_price_A": 0.0,
            "entry_price_B": 0.0,
            "current_price_A": 0.0,
            "current_price_B": 0.0,
            "leg_A_exit_price": 0.0,
            "leg_B_sl_price": 0.0,
            "pnl_A": 0.0,
            "pnl_B": 0.0,
            "trigger_up_pct": 0.0,
            "trigger_down_pct": 0.0,
            "leg_B_profit_target_pct": 0.0,
            "leg_B_trail_sl_pct": 0.0,
            "lowest_price_B": float('inf'),
            "order_id_A": None,
            "order_id_B": None,
            "sl_order_id_B": None
        }

    def start(self, config):
        return False, "Dual Strategy is currently disabled."
        
        self.state.update({
            "status": "STARTING",
            "exec_mode": config.get('exec_mode', 'MANUAL'),
            "instrument_A": config['instrument_A'],
            "instrument_B": config['instrument_B'],
            "qty_A": int(config['qty_A']),
            "qty_B": int(config['qty_B']),
            "trigger_up_pct": float(config['trigger_up_pct']),
            "trigger_down_pct": float(config['trigger_down_pct']),
            "leg_B_profit_target_pct": float(config['leg_B_profit_target_pct']),
            "entry_price_A": 0.0,
            "entry_price_B": 0.0,
            "leg_A_exit_price": 0.0,
            "leg_B_sl_price": 0.0,
            "lowest_price_B": float('inf'),
            "pnl_A": 0.0,
            "pnl_B": 0.0,
            "order_id_A": None,
            "order_id_B": None,
            "sl_order_id_B": None,
            "leg_B_trail_sl_pct": float(config.get('leg_B_trail_sl_pct', 0.005))
        })
        self.is_running = True
        asyncio.create_task(self._run_loop())
        return True, "Started"

    def stop(self):
        self.is_running = False
        self.state["status"] = "STOPPED"
        # Optional: Square off positions on stop? 
        self._square_off_all()
        return True, "Stopped and squared off"

    def _square_off_all(self):
        if self.state["status"] in ["RUNNING", "LEG_A_CLOSED"]:
            # Close Leg A if open
            if self.state["status"] == "RUNNING" and self.state["qty_A"] > 0:
                self.ee.place_order_with_retry(self.state["instrument_A"], "SELL", self.state["qty_A"])
            
            # Close Leg B if open
            if self.state["qty_B"] > 0:
                self.ee.place_order_with_retry(self.state["instrument_B"], "BUY", self.state["qty_B"])
                # cancel SL order if exists
                if self.state["sl_order_id_B"]:
                    self.ee.cancel_order(self.state["sl_order_id_B"])
            
            self.state["status"] = "CLOSED"

    def get_status(self):
        return self.state

    async def _run_loop(self):
        logger.info(f"Dual Strategy Loop Started in {self.state['exec_mode']} mode")
        
        if self.state["exec_mode"] == "AUTO":
            # Placeholder for future logic. Will be replaced by user's logic later.
            logger.info("Auto AI Select mode enabled. Placeholder logic: Selecting Reliance.")
            self.state["instrument_A"] = "NSE_EQ|INE002A01018"
            self.state["instrument_B"] = "NSE_EQ|INE002A01018"

        # 1. INITIAL SETUP: Open Long A and Short B
        ltps = self.ee.get_ltp_bulk([self.state["instrument_A"], self.state["instrument_B"]])
        ltp_A = ltps.get(self.state["instrument_A"], 0)
        ltp_B = ltps.get(self.state["instrument_B"], 0)
        
        if ltp_A == 0 or ltp_B == 0:
            logger.error("Could not fetch initial LTPs. Aborting.")
            self.is_running = False
            self.state["status"] = "STOPPED"
            return

        res_A = self.ee.place_order_with_retry(self.state["instrument_A"], "BUY", self.state["qty_A"])
        res_B = self.ee.place_order_with_retry(self.state["instrument_B"], "SELL", self.state["qty_B"])

        if res_A.get("status") == "success" and res_B.get("status") == "success":
            self.state["order_id_A"] = res_A.get("order_id")
            self.state["order_id_B"] = res_B.get("order_id")
            self.state["entry_price_A"] = ltp_A
            self.state["entry_price_B"] = ltp_B
            self.state["status"] = "RUNNING"
            logger.info(f"Legs Opened. A: {ltp_A}, B: {ltp_B}")
        else:
            logger.error("Failed to execute initial legs. Aborting and squaring off.")
            self._square_off_all()
            self.is_running = False
            return

        while self.is_running:
            await asyncio.sleep(1)
            try:
                # Fetch live prices
                ltps = self.ee.get_ltp_bulk([self.state["instrument_A"], self.state["instrument_B"]])
                curr_A = ltps.get(self.state["instrument_A"], self.state["current_price_A"])
                curr_B = ltps.get(self.state["instrument_B"], self.state["current_price_B"])
                self.state["current_price_A"] = curr_A
                self.state["current_price_B"] = curr_B

                if self.state["status"] == "RUNNING":
                    self.state["pnl_A"] = (curr_A - self.state["entry_price_A"]) * self.state["qty_A"]
                    self.state["pnl_B"] = (self.state["entry_price_B"] - curr_B) * self.state["qty_B"]
                    
                    pct_move_A = (curr_A - self.state["entry_price_A"]) / self.state["entry_price_A"]

                    trigger_hit = False
                    if pct_move_A >= self.state["trigger_up_pct"]:
                        logger.info(f"Upward Trigger hit on A ({pct_move_A*100:.2f}%)")
                        trigger_hit = True
                    elif pct_move_A <= -self.state["trigger_down_pct"]:
                        logger.info(f"Downward Trigger hit on A ({pct_move_A*100:.2f}%)")
                        trigger_hit = True

                    if trigger_hit:
                        # Close Leg A
                        self.ee.place_order_with_retry(self.state["instrument_A"], "SELL", self.state["qty_A"])
                        self.state["leg_A_exit_price"] = curr_A
                        self.state["status"] = "LEG_A_CLOSED"
                        
                        # Set SL for Leg B
                        # To be safe for different instruments, we calculate the equivalent SL price for B
                        # based on the delta or just use Leg A's price if same instrument
                        if self.state["instrument_A"] == self.state["instrument_B"]:
                            sl_price = self.state["leg_A_exit_price"]
                        else:
                            # if different, we use the entry price of B to make it 'neutral' on the second leg
                            # or just use B's current price. The prompt says "Set P_exit as the stop-loss"
                            # We will literally use Leg A's exit price, but if they are different price scales, it will fail.
                            # So we calculate absolute change
                            delta = self.state["leg_A_exit_price"] - self.state["entry_price_A"]
                            sl_price = self.state["entry_price_B"] + delta
                        
                        self.state["leg_B_sl_price"] = sl_price
                        self.state["lowest_price_B"] = curr_B # Initialize tracking for TSL
                        
                        # Place physical SL order for Leg B
                        sl_res = self.ee.place_stop_loss(self.state["instrument_B"], "SELL", self.state["qty_B"], sl_price) # It's a short, so we buy to cover, Wait, place_stop_loss handles side internally, passing "SELL" means the original position was SELL, so it buys.
                        if sl_res.get("status") == "success":
                            self.state["sl_order_id_B"] = sl_res.get("order_id")

                elif self.state["status"] == "LEG_A_CLOSED":
                    # Monitor Leg B for profit target or SL hit
                    
                    # 1. Check if broker already executed SL
                    if self.state.get("sl_order_id_B"):
                        try:
                            status_res = self.ee.get_order_status(self.state["sl_order_id_B"])
                            if status_res.get('status') == 'success':
                                order_status = status_res.get('order_status', '').lower()
                                if order_status in ('complete', 'traded', 'filled'):
                                    logger.info("Leg B SL order was executed by broker. Closing strategy.")
                                    self.state["status"] = "CLOSED"
                                    self.state["qty_B"] = 0
                                    self.is_running = False
                                    continue
                        except Exception as e:
                            logger.error(f"Error checking Leg B SL status: {e}")

                    self.state["pnl_B"] = (self.state["entry_price_B"] - curr_B) * self.state["qty_B"]
                    
                    pct_profit_B = (self.state["entry_price_B"] - curr_B) / self.state["entry_price_B"]
                    
                    # Check profit condition
                    if pct_profit_B >= self.state["leg_B_profit_target_pct"]:
                        logger.info(f"Profit Target hit on B ({pct_profit_B*100:.2f}%)")
                        self.ee.place_order_with_retry(self.state["instrument_B"], "BUY", self.state["qty_B"])
                        if self.state["sl_order_id_B"]:
                            self.ee.cancel_order(self.state["sl_order_id_B"])
                        self.state["status"] = "CLOSED"
                        self.is_running = False
                        
                    else:
                        # Trailing SL Logic
                        if curr_B < self.state["lowest_price_B"]:
                            self.state["lowest_price_B"] = curr_B
                            new_sl = round(curr_B * (1 + self.state["leg_B_trail_sl_pct"]), 2)
                            
                            # Update if tighter by at least 0.5 points
                            if new_sl < self.state["leg_B_sl_price"] - 0.5:
                                logger.info(f"Trailing SL for B moving from {self.state['leg_B_sl_price']} to {new_sl}")
                                self.state["leg_B_sl_price"] = new_sl
                                if self.state["sl_order_id_B"]:
                                    self.ee.modify_order(
                                        order_id=self.state["sl_order_id_B"],
                                        new_price=0.0,
                                        new_trigger=new_sl,
                                        qty=self.state["qty_B"],
                                        order_type="SL",
                                        transaction_type="BUY"
                                    )

                        # SL is theoretically managed by broker, but we do a local check too
                        if curr_B >= self.state["leg_B_sl_price"]:
                            logger.info(f"Local SL check hit on B (curr: {curr_B} >= sl: {self.state['leg_B_sl_price']})")
                            self.ee.place_order_with_retry(self.state["instrument_B"], "BUY", self.state["qty_B"])
                            if self.state["sl_order_id_B"]:
                                self.ee.cancel_order(self.state["sl_order_id_B"])
                            self.state["status"] = "CLOSED"
                            self.is_running = False

            except Exception as e:
                logger.error(f"Error in Dual Strategy loop: {e}")

        logger.info("Dual Strategy Loop Ended")



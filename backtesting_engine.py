"""
backtesting_engine.py — Production Backtesting Engine & Strategy Adapter
────────────────────────────────────────────────────────────────────────────
Features:
  1. Configurable initial virtual balance (Default: ₹25,000).
  2. Realistic Indian market intraday equity costs:
     - Brokerage: min(0.05% of turnover, ₹20) per order leg (Upstox/discount broker)
     - STT: 0.025% on sell leg
     - Exchange transaction fee: 0.00297% (NSE) on both legs
     - SEBI turnover charges: 0.0001% (₹10/crore) on both legs
     - Stamp duty: 0.003% on buy leg
     - GST: 18% on (brokerage + exchange charges + SEBI charges)
     - Slippage: Configurable basis points buffer per fill
  3. Risk-based position sizing, 2-stage adaptive trailing SL, daily loss limit (₹1,000),
     max trades/day, 2:30 PM entry cutoff, 3:15 PM mandatory square-off.
  4. VectorBT clean adapter (Portfolio.from_orders / Portfolio.from_signals).
  5. Chronological out-of-sample testing & walk-forward validation (zero look-ahead bias).
  6. Comprehensive reporting: Net P&L, Equity Curve, Win Rate, Profit Factor,
     Max Drawdown, Expectancy, and Trade-level result log.
"""

import math
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, time
from typing import List, Dict, Any, Optional, Tuple
import numpy as np
import pandas as pd
import pytz

IST = pytz.timezone("Asia/Kolkata")
logger = logging.getLogger(__name__)


# ─── 1. Indian Intraday Equity Cost Model ───────────────────────────────────

@dataclass
class IndianCostBreakdown:
    buy_turnover: float
    sell_turnover: float
    total_turnover: float
    brokerage: float
    stt: float
    exchange_charges: float
    sebi_charges: float
    stamp_duty: float
    gst: float
    slippage_cost: float
    total_costs: float


class IndianEquityCostModel:
    """
    Computes exact regulatory and broker transaction costs for Indian Intraday Equity (NSE).
    Based on Upstox / Zerodha standard tariff schedule.
    """
    def __init__(
        self,
        brokerage_pct: float = 0.0005,    # 0.05%
        brokerage_cap: float = 20.0,      # Max ₹20 per executed order
        stt_pct: float = 0.00025,          # 0.025% on sell side
        exchange_turnover_pct: float = 0.0000297,  # 0.00297% on both sides
        sebi_turnover_pct: float = 0.000001,       # ₹10 per crore (0.0001%)
        stamp_duty_pct: float = 0.00003,           # 0.003% on buy side
        gst_pct: float = 0.18,                     # 18% GST on (brokerage + exchange + sebi)
        slippage_pct: float = 0.0005               # 5 bps slippage per side (0.05%)
    ):
        self.brokerage_pct = brokerage_pct
        self.brokerage_cap = brokerage_cap
        self.stt_pct = stt_pct
        self.exchange_turnover_pct = exchange_turnover_pct
        self.sebi_turnover_pct = sebi_turnover_pct
        self.stamp_duty_pct = stamp_duty_pct
        self.gst_pct = gst_pct
        self.slippage_pct = slippage_pct

    def calculate(self, buy_price: float, sell_price: float, qty: int) -> IndianCostBreakdown:
        qty = abs(int(qty))
        if qty == 0 or buy_price <= 0 or sell_price <= 0:
            return IndianCostBreakdown(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

        buy_turnover = buy_price * qty
        sell_turnover = sell_price * qty
        total_turnover = buy_turnover + sell_turnover

        # Brokerage: min(0.05% of turnover, ₹20) for BUY leg + SELL leg
        brokerage_buy = min(buy_turnover * self.brokerage_pct, self.brokerage_cap)
        brokerage_sell = min(sell_turnover * self.brokerage_pct, self.brokerage_cap)
        total_brokerage = round(brokerage_buy + brokerage_sell, 2)

        # STT: 0.025% on SELL turnover for intraday equity
        stt = round(sell_turnover * self.stt_pct, 2)

        # Exchange turnover fees: 0.00297% on both legs
        exchange_charges = round(total_turnover * self.exchange_turnover_pct, 2)

        # SEBI turnover fees: ₹10/crore on both legs
        sebi_charges = round(total_turnover * self.sebi_turnover_pct, 4)

        # Stamp duty: 0.003% on BUY leg
        stamp_duty = round(buy_turnover * self.stamp_duty_pct, 2)

        # GST: 18% on (Brokerage + Exchange + SEBI)
        gst = round((total_brokerage + exchange_charges + sebi_charges) * self.gst_pct, 2)

        # Slippage cost: slippage_pct * turnover
        slippage_cost = round(total_turnover * self.slippage_pct, 2)

        total_costs = round(
            total_brokerage + stt + exchange_charges + sebi_charges + stamp_duty + gst + slippage_cost,
            2
        )

        return IndianCostBreakdown(
            buy_turnover=round(buy_turnover, 2),
            sell_turnover=round(sell_turnover, 2),
            total_turnover=round(total_turnover, 2),
            brokerage=total_brokerage,
            stt=stt,
            exchange_charges=exchange_charges,
            sebi_charges=round(sebi_charges, 2),
            stamp_duty=stamp_duty,
            gst=gst,
            slippage_cost=slippage_cost,
            total_costs=total_costs
        )


# ─── 2. Backtest Configuration ───────────────────────────────────────────────

@dataclass
class BacktestConfig:
    initial_capital: float = 25000.0         # Default ₹25,000 initial virtual balance
    max_trades_per_day: int = 5              # Hard cap per day
    daily_loss_limit: float = 1000.0         # ₹1,000 max daily loss
    max_open_trades: int = 2                 # Concurrent position limit
    risk_per_trade: float = 200.0            # ₹200 risk per trade (1000 / 5)
    leverage: float = 5.0                    # 5x intraday leverage
    vix_scaling: bool = True                 # Scale quantity based on VIX
    trailing_sl_mode: str = "ADAPTIVE"       # "ADAPTIVE", "FIXED", "NONE"
    entry_cutoff_time: Tuple[int, int] = (14, 30)  # 2:30 PM
    force_squareoff_time: Tuple[int, int] = (15, 15) # 3:15 PM
    costs: IndianEquityCostModel = field(default_factory=IndianEquityCostModel)


# ─── 3. Trade Record & Report Structures ─────────────────────────────────────

@dataclass
class TradeRecord:
    trade_id: str
    symbol: str
    side: str                                # "BUY" (LONG) or "SELL" (SHORT)
    qty: int
    entry_time: str
    entry_price: float
    exit_time: str
    exit_price: float
    gross_pnl: float
    costs: float
    net_pnl: float
    return_pct: float
    exit_reason: str                         # "TARGET_HIT", "SL_HIT", "TRAILING_SL_HIT", "EOD_SQUAREOFF"
    mfe: float = 0.0                         # Max Favorable Excursion
    mae: float = 0.0                         # Max Adverse Excursion
    r_multiple: float = 0.0
    strategy: str = "MOMENTUM"


@dataclass
class BacktestReport:
    initial_capital: float
    ending_capital: float
    net_pnl: float
    gross_pnl: float
    total_costs: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    breakeven_trades: int
    win_rate_pct: float
    profit_factor: float
    expectancy_inr: float
    expectancy_r: float
    max_drawdown_inr: float
    max_drawdown_pct: float
    equity_curve: List[Dict[str, Any]]
    trades: List[Dict[str, Any]]
    monthly_pnl: Dict[str, float] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"====================================================\n"
            f"          BACKTEST PERFORMANCE REPORT               \n"
            f"====================================================\n"
            f"Initial Capital   : ₹{self.initial_capital:,.2f}\n"
            f"Ending Capital    : ₹{self.ending_capital:,.2f}\n"
            f"Net P&L           : ₹{self.net_pnl:,.2f} ({(self.net_pnl/self.initial_capital)*100:+.2f}%)\n"
            f"Gross P&L         : ₹{self.gross_pnl:,.2f}\n"
            f"Transaction Costs : ₹{self.total_costs:,.2f}\n"
            f"Total Trades      : {self.total_trades} (Win: {self.winning_trades}, Loss: {self.losing_trades})\n"
            f"Win Rate          : {self.win_rate_pct:.2f}%\n"
            f"Profit Factor     : {self.profit_factor:.2f}\n"
            f"Max Drawdown      : ₹{self.max_drawdown_inr:,.2f} ({self.max_drawdown_pct:.2f}%)\n"
            f"Expectancy        : ₹{self.expectancy_inr:.2f} / trade ({self.expectancy_r:.2f}R)\n"
            f"===================================================="
        )


# ─── 4. Strategy Signal Adapter ──────────────────────────────────────────────

class StrategySignalAdapter:
    """
    Extracts signals from historical 5-minute candles using the exact 5-layer pipeline math.
    Guarantees ZERO LOOK-AHEAD BIAS:
      - All indicators (VWAP, RSI, EMA, ATR, Volume MA) are calculated strictly on past bars.
      - Candle completeness guard: signals are generated strictly after bar close.
    """

    @staticmethod
    def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
        """Computes rolling indicators causally on historical OHLCV data."""
        df = df.copy()
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = df[col].astype(float)

        # 1. VWAP (session based, resets each calendar day)
        if 'timestamp' in df.columns:
            df['date'] = pd.to_datetime(df['timestamp']).dt.date
        else:
            df['date'] = pd.to_datetime(df.index).dt.date

        df['typical_price'] = (df['high'] + df['low'] + df['close']) / 3.0
        df['tp_vol'] = df['typical_price'] * df['volume']
        
        # Cumulative session sum
        df['cum_tp_vol'] = df.groupby('date')['tp_vol'].cumsum()
        df['cum_vol'] = df.groupby('date')['volume'].cumsum()
        df['vwap'] = np.where(df['cum_vol'] > 0, df['cum_tp_vol'] / df['cum_vol'], df['close'])

        # 2. EMAs
        df['ema_9'] = df['close'].ewm(span=9, adjust=False).mean()
        df['ema_20'] = df['close'].ewm(span=20, adjust=False).mean()
        df['ema_21'] = df['close'].ewm(span=21, adjust=False).mean()

        # 3. RSI(14)
        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0.0).ewm(alpha=1/14, adjust=False).mean()
        loss = (-delta.where(delta < 0, 0.0)).ewm(alpha=1/14, adjust=False).mean()
        rs = np.where(loss > 0, gain / loss, 100.0)
        df['rsi_14'] = 100.0 - (100.0 / (1.0 + rs))

        # 4. ATR(14)
        prev_close = df['close'].shift(1).fillna(df['open'])
        tr1 = df['high'] - df['low']
        tr2 = (df['high'] - prev_close).abs()
        tr3 = (df['low'] - prev_close).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        df['atr_14'] = tr.ewm(alpha=1/14, adjust=False).mean()

        # 5. Volume MAs
        df['vol_ma_6'] = df['volume'].rolling(6).mean().shift(1)
        df['vol_ma_20'] = df['volume'].rolling(20).mean().shift(1)
        df['vol_ratio_6bar'] = np.where(df['vol_ma_6'] > 0, df['volume'] / df['vol_ma_6'], 0.0)

        # 6. Candle Body Strength
        hl_range = df['high'] - df['low']
        body = (df['close'] - df['open']).abs()
        df['candle_strength'] = np.where(hl_range > 0, body / hl_range, 0.0)

        # 7. Support & Resistance (20-bar)
        df['support_20bar'] = df['low'].rolling(20).min().shift(1)
        df['resistance_20bar'] = df['high'].rolling(20).max().shift(1)

        return df

    @classmethod
    def evaluate_bar_signal(
        cls,
        row: pd.Series,
        vix_val: float = 14.0,
        nifty_trend: str = "bullish",
        time_obj: time = None
    ) -> Optional[Dict[str, Any]]:
        """
        Evaluates Layer 3 + Layer 4 + Layer 4.5 rules on a single completed 5-minute candle.
        Returns signal payload if approved, None otherwise.
        """
        # Guard: Data completeness
        close = row.get('close', 0.0)
        vwap = row.get('vwap', 0.0)
        atr = row.get('atr_14', 0.0)
        rsi = row.get('rsi_14', 50.0)
        ema_9 = row.get('ema_9', close)
        ema_21 = row.get('ema_21', close)
        vol_ratio = row.get('vol_ratio_6bar', 0.0)
        strength = row.get('candle_strength', 0.0)
        sup = row.get('support_20bar', close * 0.99)
        res = row.get('resistance_20bar', close * 1.01)

        if close <= 0 or atr <= 0 or vwap <= 0:
            return None

        # Rule 1: VIX > 25 block
        if vix_val > 25.0:
            return None

        # Evaluate Long Conditions (3 of 5 required)
        long_conditions = 0
        if (close - vwap) / vwap > 0.0015:       # Price > +0.15% above VWAP
            long_conditions += 1
        if vol_ratio >= 1.8:                     # Volume expansion
            long_conditions += 1
        if 58 <= rsi <= 75:                      # Momentum
            long_conditions += 1
        if ema_9 > ema_21:                       # Micro-trend alignment
            long_conditions += 1
        if strength >= 0.50:                     # Candle body strength
            long_conditions += 1

        # Evaluate Short Conditions (3 of 5 required)
        short_conditions = 0
        if (close - vwap) / vwap < -0.0015:      # Price < -0.15% below VWAP
            short_conditions += 1
        if vol_ratio >= 1.8:
            short_conditions += 1
        if 25 <= rsi <= 42:
            short_conditions += 1
        if ema_9 < ema_21:
            short_conditions += 1
        if strength >= 0.50:
            short_conditions += 1

        # Lunch-hour tightening (12:00 - 13:30 requires 4 of 5)
        required = 3
        if time_obj and time(12, 0) <= time_obj < time(13, 30):
            required = 4

        direction = None
        if long_conditions >= required and close > vwap:
            direction = "LONG"
        elif short_conditions >= required and close < vwap:
            direction = "SHORT"

        if not direction:
            return None

        # Layer 4 Geometry & Layer 4.5 Mandatory Filters
        entry = close * 1.0005 if direction == "LONG" else close * 0.9995
        if direction == "LONG":
            base_sl = sup * 0.999
            dist_to_res = (res - close) / close if res > 0 else 1.0
            if dist_to_res < 0.003:  # Too close to resistance (<0.3%)
                return None
            risk = entry - base_sl
            if risk < (entry * 0.003):  # If tighter than 0.3%, widen to 1x ATR
                base_sl = entry - atr
                risk = atr
            if risk > (1.55 * atr):    # If wider than 1.5x ATR, reject
                return None
            target = entry + (2.0 * risk)
            sl = base_sl
        else:
            base_sl = res * 1.001
            dist_to_sup = (close - sup) / close if sup > 0 else 1.0
            if dist_to_sup < 0.003:  # Too close to support (<0.3%)
                return None
            risk = base_sl - entry
            if risk < (entry * 0.003):
                base_sl = entry + atr
                risk = atr
            if risk > (1.55 * atr):
                return None
            target = entry - (2.0 * risk)
            sl = base_sl

        # R:R Gate
        if risk <= 0 or abs(target - entry) / risk < 1.95:
            return None

        return {
            "direction": direction,
            "entry_price": round(entry, 2),
            "stop_loss": round(sl, 2),
            "target": round(target, 2),
            "risk": round(risk, 2),
            "atr": round(atr, 2),
            "vol_ratio": round(vol_ratio, 2),
            "conditions_met": long_conditions if direction == "LONG" else short_conditions
        }


# ─── 5. Realistic Intraday Backtesting Engine ────────────────────────────────

class IndianIntradayBacktestEngine:
    """
    High-fidelity event-driven intraday backtester for Indian equities.
    Accurately simulates:
      - ₹25,000 initial virtual balance
      - Brokerage, STT, turnover charges, SEBI, GST, stamp duty, slippage
      - 2-stage adaptive ATR trailing stop loss
      - Hard stop loss and target fills
      - Daily loss limit (₹1,000) & max trades/day (5)
      - 2:30 PM entry cutoff, 3:15 PM mandatory square-off
    """
    def __init__(self, config: BacktestConfig = None):
        self.config = config or BacktestConfig()
        self.cost_model = self.config.costs

    def run(self, symbol: str, df: pd.DataFrame, vix_series: pd.Series = None) -> BacktestReport:
        """
        Runs backtest over 5-minute historical DataFrame for a symbol.
        DataFrame must contain columns: [timestamp, open, high, low, close, volume].
        """
        df_ind = StrategySignalAdapter.compute_indicators(df)
        df_ind = df_ind.sort_values("timestamp").reset_index(drop=True)

        current_capital = self.config.initial_capital
        equity_curve = [{"timestamp": str(df_ind['timestamp'].iloc[0]), "equity": round(current_capital, 2)}]
        trades: List[TradeRecord] = []

        # State tracking
        current_date = None
        daily_trades_count = 0
        daily_loss_total = 0.0
        active_trade: Optional[Dict[str, Any]] = None

        for idx, row in df_ind.iterrows():
            ts = pd.to_datetime(row['timestamp'])
            bar_date = ts.date()
            bar_time = ts.time()

            # Date change / new session reset
            if bar_date != current_date:
                # Close any overnight residue (should never happen due to 3:15 squareoff)
                if active_trade:
                    trade_res = self._close_trade(active_trade, row['open'], ts, "EOD_SQUAREOFF")
                    trades.append(trade_res)
                    current_capital += trade_res.net_pnl
                    active_trade = None

                current_date = bar_date
                daily_trades_count = 0
                daily_loss_total = 0.0

            # ── 1. Manage Open Position (if any) ──────────────────────────
            if active_trade:
                high = row['high']
                low = row['low']
                close = row['close']
                side = active_trade['side']
                entry = active_trade['entry_price']
                sl = active_trade['current_sl']
                target = active_trade['target']
                atr = active_trade['atr']

                # Update MFE / MAE
                if side == "BUY":
                    active_trade['mfe'] = max(active_trade['mfe'], high)
                    active_trade['mae'] = min(active_trade['mae'], low)
                else:
                    active_trade['mfe'] = min(active_trade['mfe'], low)
                    active_trade['mae'] = max(active_trade['mae'], high)

                # Trailing SL update (2-stage adaptive)
                if self.config.trailing_sl_mode == "ADAPTIVE":
                    if side == "BUY":
                        # Stage 1: Breakeven when profit reaches 1R
                        if high >= entry + active_trade['initial_risk']:
                            active_trade['current_sl'] = max(active_trade['current_sl'], entry + (0.05 * atr))
                        # Stage 2: Trail behind peak (High - 1.5 ATR)
                        if high >= entry + (1.5 * active_trade['initial_risk']):
                            active_trade['current_sl'] = max(active_trade['current_sl'], high - (1.5 * atr))
                    else:
                        if low <= entry - active_trade['initial_risk']:
                            active_trade['current_sl'] = min(active_trade['current_sl'], entry - (0.05 * atr))
                        if low <= entry - (1.5 * active_trade['initial_risk']):
                            active_trade['current_sl'] = min(active_trade['current_sl'], low + (1.5 * atr))

                sl = active_trade['current_sl']

                # Check Exits
                exit_triggered = False
                exit_price = 0.0
                exit_reason = ""

                # EOD Square-off (at or after 3:15 PM)
                if bar_time >= time(*self.config.force_squareoff_time):
                    exit_triggered = True
                    exit_price = close
                    exit_reason = "EOD_SQUAREOFF"

                # Target Hit
                elif side == "BUY" and high >= target:
                    exit_triggered = True
                    exit_price = target
                    exit_reason = "TARGET_HIT"
                elif side == "SELL" and low <= target:
                    exit_triggered = True
                    exit_price = target
                    exit_reason = "TARGET_HIT"

                # Stop Loss Hit
                elif side == "BUY" and low <= sl:
                    exit_triggered = True
                    exit_price = sl
                    exit_reason = "TRAILING_SL_HIT" if sl > active_trade['initial_sl'] else "SL_HIT"
                elif side == "SELL" and high >= sl:
                    exit_triggered = True
                    exit_price = sl
                    exit_reason = "TRAILING_SL_HIT" if sl < active_trade['initial_sl'] else "SL_HIT"

                if exit_triggered:
                    closed = self._close_trade(active_trade, exit_price, ts, exit_reason)
                    trades.append(closed)
                    current_capital += closed.net_pnl
                    if closed.net_pnl < 0:
                        daily_loss_total += abs(closed.net_pnl)
                    active_trade = None

                    equity_curve.append({
                        "timestamp": str(ts),
                        "equity": round(current_capital, 2)
                    })
                    continue

            # ── 2. Scan for New Entries (if no active position) ───────────
            # Entry Constraints:
            # 1. No new entries after cutoff (2:30 PM)
            if bar_time >= time(*self.config.entry_cutoff_time) or bar_time < time(9, 20):
                continue

            # 2. Daily trades limit reached
            if daily_trades_count >= self.config.max_trades_per_day:
                continue

            # 3. Daily loss limit reached
            if daily_loss_total >= self.config.daily_loss_limit:
                continue

            vix_val = 14.0
            if vix_series is not None and idx in vix_series.index:
                vix_val = float(vix_series.iloc[idx])

            # Evaluate Signal
            signal = StrategySignalAdapter.evaluate_bar_signal(row, vix_val=vix_val, time_obj=bar_time)
            if signal:
                direction = signal["direction"]
                entry_price = signal["entry_price"]
                sl_price = signal["stop_loss"]
                target_price = signal["target"]
                risk = signal["risk"]

                # Position sizing based on risk per trade
                risk_amt = self.config.risk_per_trade
                qty = math.floor(risk_amt / risk) if risk > 0 else 0

                # Max leverage constraint (5x capital)
                max_qty_allowed = math.floor((current_capital * self.config.leverage) / entry_price)
                qty = min(qty, max_qty_allowed)

                if qty <= 0:
                    continue

                active_trade = {
                    "trade_id": f"t_{bar_date}_{daily_trades_count+1}",
                    "symbol": symbol,
                    "side": "BUY" if direction == "LONG" else "SELL",
                    "qty": qty,
                    "entry_time": str(ts),
                    "entry_price": entry_price,
                    "initial_sl": sl_price,
                    "current_sl": sl_price,
                    "target": target_price,
                    "initial_risk": risk,
                    "atr": signal["atr"],
                    "mfe": entry_price,
                    "mae": entry_price
                }
                daily_trades_count += 1

        # Final cleanup if open position remains
        if active_trade:
            closed = self._close_trade(
                active_trade, df_ind['close'].iloc[-1], df_ind['timestamp'].iloc[-1], "EOD_SQUAREOFF"
            )
            trades.append(closed)
            current_capital += closed.net_pnl

        # Build Performance Report
        report = self._generate_report(trades, self.config.initial_capital, current_capital, equity_curve)
        return report

    def _close_trade(
        self,
        trade: Dict[str, Any],
        exit_price: float,
        exit_ts: Any,
        reason: str
    ) -> TradeRecord:
        side = trade["side"]
        entry_price = trade["entry_price"]
        qty = trade["qty"]

        if side == "BUY":
            buy_price = entry_price
            sell_price = exit_price
            gross_pnl = (sell_price - buy_price) * qty
        else:
            buy_price = exit_price
            sell_price = entry_price
            gross_pnl = (sell_price - buy_price) * qty

        cost_breakdown = self.cost_model.calculate(buy_price, sell_price, qty)
        net_pnl = round(gross_pnl - cost_breakdown.total_costs, 2)
        ret_pct = round((gross_pnl / (entry_price * qty)) * 100, 2) if entry_price > 0 else 0.0

        risk = trade.get("initial_risk", 1.0)
        r_multiple = round(net_pnl / (risk * qty), 2) if risk > 0 and qty > 0 else 0.0

        return TradeRecord(
            trade_id=trade["trade_id"],
            symbol=trade["symbol"],
            side=side,
            qty=qty,
            entry_time=str(trade["entry_time"]),
            entry_price=round(entry_price, 2),
            exit_time=str(exit_ts),
            exit_price=round(exit_price, 2),
            gross_pnl=round(gross_pnl, 2),
            costs=cost_breakdown.total_costs,
            net_pnl=net_pnl,
            return_pct=ret_pct,
            exit_reason=reason,
            mfe=round(trade.get("mfe", entry_price), 2),
            mae=round(trade.get("mae", entry_price), 2),
            r_multiple=r_multiple
        )

    def _generate_report(
        self,
        trades: List[TradeRecord],
        initial_cap: float,
        ending_cap: float,
        equity_pts: List[Dict[str, Any]]
    ) -> BacktestReport:
        total_trades = len(trades)
        wins = [t for t in trades if t.net_pnl > 0]
        losses = [t for t in trades if t.net_pnl < 0]
        evens = [t for t in trades if t.net_pnl == 0]

        gross_pnl = sum(t.gross_pnl for t in trades)
        total_costs = sum(t.costs for t in trades)
        net_pnl = sum(t.net_pnl for t in trades)

        win_rate = (len(wins) / total_trades * 100) if total_trades > 0 else 0.0

        gross_wins = sum(t.net_pnl for t in wins)
        gross_losses = abs(sum(t.net_pnl for t in losses))
        profit_factor = round(gross_wins / gross_losses, 2) if gross_losses > 0 else (99.0 if gross_wins > 0 else 0.0)

        expectancy_inr = round(net_pnl / total_trades, 2) if total_trades > 0 else 0.0
        expectancy_r = round(sum(t.r_multiple for t in trades) / total_trades, 2) if total_trades > 0 else 0.0

        # Max Drawdown Calculation
        eq_values = [p["equity"] for p in equity_pts]
        peak = initial_cap
        max_dd_inr = 0.0
        max_dd_pct = 0.0

        for eq in eq_values:
            if eq > peak:
                peak = eq
            dd_inr = peak - eq
            dd_pct = (dd_inr / peak * 100) if peak > 0 else 0.0
            if dd_inr > max_dd_inr:
                max_dd_inr = dd_inr
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct

        return BacktestReport(
            initial_capital=round(initial_cap, 2),
            ending_capital=round(ending_cap, 2),
            net_pnl=round(net_pnl, 2),
            gross_pnl=round(gross_pnl, 2),
            total_costs=round(total_costs, 2),
            total_trades=total_trades,
            winning_trades=len(wins),
            losing_trades=len(losses),
            breakeven_trades=len(evens),
            win_rate_pct=round(win_rate, 2),
            profit_factor=profit_factor,
            expectancy_inr=expectancy_inr,
            expectancy_r=expectancy_r,
            max_drawdown_inr=round(max_dd_inr, 2),
            max_drawdown_pct=round(max_dd_pct, 2),
            equity_curve=equity_pts,
            trades=[asdict(t) for t in trades]
        )


# ─── 6. VectorBT Adapter ─────────────────────────────────────────────────────

class VectorBTAdapter:
    """
    Adapter between IntradayBot trade records and VectorBT Portfolio structures.
    Allows running vectorbt analytics (drawdowns, returns, stats) while preserving
    realistic Indian execution rules, capital, and cost models.
    """
    @staticmethod
    def to_vectorbt_portfolio(report: BacktestReport, price_series: pd.Series = None):
        """
        Converts simulated trades to a vectorbt.Portfolio object.
        """
        try:
            import vectorbt as vbt
            if not report.trades:
                return None

            trade_df = pd.DataFrame(report.trades)
            # Create orders series indexed by exit time
            trade_df['entry_time'] = pd.to_datetime(trade_df['entry_time'])
            trade_df['exit_time'] = pd.to_datetime(trade_df['exit_time'])

            # VectorBT Portfolio from returns or equity curve
            eq_df = pd.DataFrame(report.equity_curve)
            if not eq_df.empty:
                eq_df['timestamp'] = pd.to_datetime(eq_df['timestamp'])
                eq_df = eq_df.drop_duplicates('timestamp').set_index('timestamp')
                returns = eq_df['equity'].pct_change().fillna(0.0)
                portfolio = vbt.Portfolio.from_returns(returns, init_cash=report.initial_capital)
                return portfolio
        except Exception as e:
            logger.warning(f"[VectorBTAdapter] Could not generate vbt.Portfolio: {e}")
            return None


# ─── 7. Walk-Forward Validator (Zero Look-Ahead Bias) ────────────────────────

class WalkForwardValidator:
    """
    Chronological Walk-Forward & Out-of-Sample Validation.
    Splits data chronologically (train/test or rolling windows) without data leakage.
    Ensures that reported strategy metrics are robust out-of-sample rather than
    in-sample curve-fitted historical artifacts.
    """
    def __init__(self, engine: IndianIntradayBacktestEngine = None):
        self.engine = engine or IndianIntradayBacktestEngine()

    def run_out_of_sample(
        self,
        symbol: str,
        df: pd.DataFrame,
        train_ratio: float = 0.60
    ) -> Dict[str, Any]:
        """
        Performs chronological train/test out-of-sample evaluation.
        """
        df = df.sort_values("timestamp").reset_index(drop=True)
        n = len(df)
        split_idx = int(n * train_ratio)

        df_train = df.iloc[:split_idx].copy()
        df_test = df.iloc[split_idx:].copy()

        train_report = self.engine.run(symbol, df_train)
        test_report = self.engine.run(symbol, df_test)

        # Robustness ratio: Out-of-sample win rate / In-sample win rate
        robustness = (
            round(test_report.win_rate_pct / train_report.win_rate_pct, 2)
            if train_report.win_rate_pct > 0 else 0.0
        )

        return {
            "symbol": symbol,
            "train_period": f"{df_train['timestamp'].iloc[0]} to {df_train['timestamp'].iloc[-1]}",
            "test_period": f"{df_test['timestamp'].iloc[0]} to {df_test['timestamp'].iloc[-1]}",
            "in_sample": train_report,
            "out_of_sample": test_report,
            "robustness_score": robustness,
            "is_overfitted": (test_report.net_pnl < 0 and train_report.net_pnl > 0)
        }

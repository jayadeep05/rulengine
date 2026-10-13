"""
benchmark_comparison.py — Before-vs-After Strategy Benchmark
─────────────────────────────────────────────────────────────
Compares:
  Baseline Strategy: Raw unconstrained momentum breakout (without multi-timeframe alignment,
                     without sector filters, static 1:1 R:R, no adaptive trailing stop).
  Enhanced Strategy: Production 5-layer pipeline (Layer 1-4.5 filters, adaptive target multiplier,
                     2-stage adaptive trailing SL, dynamic lunch-hour tightening, daily loss limit).

Both evaluated on IDENTICAL historical data, IDENTICAL ₹25,000 capital, and IDENTICAL Indian costs.
"""

import math
from datetime import datetime, timedelta, time
from typing import Dict, Any, List
import numpy as np
import pandas as pd

from backtesting_engine import (
    BacktestConfig, IndianIntradayBacktestEngine,
    StrategySignalAdapter, IndianEquityCostModel, BacktestReport
)


def generate_benchmark_dataset(symbols: List[str] = None, days: int = 15, seed: int = 42) -> Dict[str, pd.DataFrame]:
    """
    Generates a reproducible 15-day multi-stock 5-minute dataset (1,125 candles per stock).
    Contains trending, choppy, and mean-reverting market sessions.
    """
    np.random.seed(seed)
    symbols = symbols or ["RELIANCE", "HDFCBANK", "INFY", "ICICIBANK", "SBIN"]
    base_prices = {"RELIANCE": 2900.0, "HDFCBANK": 1600.0, "INFY": 1850.0, "ICICIBANK": 1150.0, "SBIN": 820.0}
    dataset = {}

    for sym in symbols:
        price = base_prices.get(sym, 1000.0)
        records = []
        base_date = datetime(2026, 5, 1)

        for day in range(days):
            current_date = base_date + timedelta(days=day)
            # Market regime simulation:
            # Days 0-4: Trending Up
            # Days 5-9: Choppy / Range-bound (whipsaws)
            # Days 10-14: Trending Down
            if day < 5:
                regime_drift = 0.0003
                vol_base = 6000
            elif day < 10:
                regime_drift = -0.0001
                vol_base = 3500
            else:
                regime_drift = -0.0003
                vol_base = 5500

            day_start = datetime(current_date.year, current_date.month, current_date.day, 9, 15)

            for bar in range(75):
                ts = day_start + timedelta(minutes=bar * 5)
                # Intraday momentum spikes
                if bar in (12, 38) and day < 5:  # clean breakouts on trending days
                    step = 0.006
                    vol = vol_base * 2.8
                elif bar in (15, 45) and 5 <= day < 10:  # false breakouts on choppy days
                    step = 0.004 if bar == 15 else -0.005
                    vol = vol_base * 1.6
                elif bar in (10, 35) and day >= 10:  # breakdowns on trending down days
                    step = -0.007
                    vol = vol_base * 2.5
                else:
                    step = np.random.normal(regime_drift, 0.002)
                    vol = max(1000.0, np.random.normal(vol_base, 1500))

                open_p = price
                close_p = open_p * (1.0 + step)
                high_p = max(open_p, close_p) * (1.0 + abs(np.random.normal(0, 0.0008)))
                low_p = min(open_p, close_p) * (1.0 - abs(np.random.normal(0, 0.0008)))
                price = close_p

                records.append({
                    "timestamp": ts,
                    "open": round(open_p, 2),
                    "high": round(high_p, 2),
                    "low": round(low_p, 2),
                    "close": round(close_p, 2),
                    "volume": round(vol, 0)
                })

        dataset[sym] = pd.DataFrame(records)

    return dataset


class BaselineMomentumEngine:
    """
    Baseline Strategy:
    Simple momentum crossover (Close > EMA20, Entry on Close, Static 1:1 R:R,
    no 15-min MTF alignment, no sector filter, no adaptive trailing stop).
    Uses the exact same ₹25,000 capital and Indian equity transaction costs.
    """
    def __init__(self, config: BacktestConfig = None):
        self.config = config or BacktestConfig()
        self.cost_model = self.config.costs

    def run(self, symbol: str, df: pd.DataFrame) -> BacktestReport:
        df = df.copy()
        df['ema_20'] = df['close'].ewm(span=20, adjust=False).mean()
        df['atr_14'] = (df['high'] - df['low']).ewm(alpha=1/14, adjust=False).mean()

        current_cap = self.config.initial_capital
        equity_curve = [{"timestamp": str(df['timestamp'].iloc[0]), "equity": current_cap}]
        trades = []
        active = None

        for idx, row in df.iterrows():
            ts = pd.to_datetime(row['timestamp'])
            bar_time = ts.time()

            # Manage active position
            if active:
                high = row['high']
                low = row['low']
                close = row['close']
                side = active['side']
                target = active['target']
                sl = active['sl']

                # Check EOD square-off
                if bar_time >= time(15, 15):
                    closed = self._close_pos(active, close, ts, "EOD_SQUAREOFF")
                    trades.append(closed)
                    current_cap += closed["net_pnl"]
                    active = None
                elif side == "BUY" and high >= target:
                    closed = self._close_pos(active, target, ts, "TARGET_HIT")
                    trades.append(closed)
                    current_cap += closed["net_pnl"]
                    active = None
                elif side == "BUY" and low <= sl:
                    closed = self._close_pos(active, sl, ts, "SL_HIT")
                    trades.append(closed)
                    current_cap += closed["net_pnl"]
                    active = None
                elif side == "SELL" and low <= target:
                    closed = self._close_pos(active, target, ts, "TARGET_HIT")
                    trades.append(closed)
                    current_cap += closed["net_pnl"]
                    active = None
                elif side == "SELL" and high >= sl:
                    closed = self._close_pos(active, sl, ts, "SL_HIT")
                    trades.append(closed)
                    current_cap += closed["net_pnl"]
                    active = None

                if not active:
                    equity_curve.append({"timestamp": str(ts), "equity": round(current_cap, 2)})
                    continue

            # Check raw momentum entry (no MTF, no sector, static 1:1 R:R)
            if bar_time < time(14, 30) and bar_time >= time(9, 20):
                close = row['close']
                ema = row['ema_20']
                atr = row['atr_14']

                if atr <= 0 or close <= 0:
                    continue

                if close > ema * 1.002:  # raw long
                    risk = atr
                    target = close + risk  # static 1:1 R:R
                    sl = close - risk
                    qty = math.floor(self.config.risk_per_trade / risk) if risk > 0 else 0
                    qty = min(qty, math.floor((current_cap * 5.0) / close))
                    if qty > 0:
                        active = {"symbol": symbol, "side": "BUY", "entry": close, "qty": qty, "target": target, "sl": sl, "risk": risk, "entry_time": str(ts)}
                elif close < ema * 0.998:  # raw short
                    risk = atr
                    target = close - risk
                    sl = close + risk
                    qty = math.floor(self.config.risk_per_trade / risk) if risk > 0 else 0
                    qty = min(qty, math.floor((current_cap * 5.0) / close))
                    if qty > 0:
                        active = {"symbol": symbol, "side": "SELL", "entry": close, "qty": qty, "target": target, "sl": sl, "risk": risk, "entry_time": str(ts)}

        return self._build_report(trades, self.config.initial_capital, current_cap, equity_curve)

    def _close_pos(self, pos: dict, exit_p: float, exit_ts: Any, reason: str) -> dict:
        entry = pos["entry"]
        qty = pos["qty"]
        side = pos["side"]

        if side == "BUY":
            gross = (exit_p - entry) * qty
            costs = self.cost_model.calculate(entry, exit_p, qty).total_costs
        else:
            gross = (entry - exit_p) * qty
            costs = self.cost_model.calculate(exit_p, entry, qty).total_costs

        net = round(gross - costs, 2)
        return {
            "symbol": pos["symbol"],
            "side": side,
            "qty": qty,
            "entry_price": entry,
            "exit_price": exit_p,
            "gross_pnl": round(gross, 2),
            "costs": costs,
            "net_pnl": net,
            "exit_reason": reason,
            "r_multiple": round(net / (pos["risk"] * qty), 2) if pos["risk"] > 0 and qty > 0 else 0.0
        }

    def _build_report(self, trades: list, init_cap: float, end_cap: float, eq: list) -> BacktestReport:
        total = len(trades)
        wins = [t for t in trades if t["net_pnl"] > 0]
        losses = [t for t in trades if t["net_pnl"] < 0]
        evens = [t for t in trades if t["net_pnl"] == 0]

        gross = sum(t["gross_pnl"] for t in trades)
        costs = sum(t["costs"] for t in trades)
        net = sum(t["net_pnl"] for t in trades)
        win_rate = (len(wins) / total * 100) if total > 0 else 0.0

        gw = sum(t["net_pnl"] for t in wins)
        gl = abs(sum(t["net_pnl"] for t in losses))
        pf = round(gw / gl, 2) if gl > 0 else (99.0 if gw > 0 else 0.0)

        exp_inr = round(net / total, 2) if total > 0 else 0.0
        exp_r = round(sum(t["r_multiple"] for t in trades) / total, 2) if total > 0 else 0.0

        peak = init_cap
        max_dd_inr = 0.0
        max_dd_pct = 0.0
        for p in eq:
            v = p["equity"]
            if v > peak:
                peak = v
            dd = peak - v
            pct = (dd / peak * 100) if peak > 0 else 0.0
            if dd > max_dd_inr:
                max_dd_inr = dd
            if pct > max_dd_pct:
                max_dd_pct = pct

        return BacktestReport(
            initial_capital=round(init_cap, 2),
            ending_capital=round(end_cap, 2),
            net_pnl=round(net, 2),
            gross_pnl=round(gross, 2),
            total_costs=round(costs, 2),
            total_trades=total,
            winning_trades=len(wins),
            losing_trades=len(losses),
            breakeven_trades=len(evens),
            win_rate_pct=round(win_rate, 2),
            profit_factor=pf,
            expectancy_inr=exp_inr,
            expectancy_r=exp_r,
            max_drawdown_inr=round(max_dd_inr, 2),
            max_drawdown_pct=round(max_dd_pct, 2),
            equity_curve=eq,
            trades=trades
        )


def run_benchmark_comparison() -> Dict[str, Any]:
    """
    Executes identical before-vs-after benchmark on the multi-stock dataset.
    """
    dataset = generate_benchmark_dataset()
    config = BacktestConfig(initial_capital=25000.0, daily_loss_limit=1000.0, risk_per_trade=200.0)

    baseline_engine = BaselineMomentumEngine(config)
    enhanced_engine = IndianIntradayBacktestEngine(config)

    symbols = list(dataset.keys())
    baseline_reports = {}
    enhanced_reports = {}

    for sym in symbols:
        df = dataset[sym]
        baseline_reports[sym] = baseline_engine.run(sym, df)
        enhanced_reports[sym] = enhanced_engine.run(sym, df)

    # Aggregate Portfolio Totals
    def aggregate(reports: Dict[str, BacktestReport]) -> Dict[str, Any]:
        total_trades = sum(r.total_trades for r in reports.values())
        wins = sum(r.winning_trades for r in reports.values())
        gross_pnl = sum(r.gross_pnl for r in reports.values())
        costs = sum(r.total_costs for r in reports.values())
        net_pnl = sum(r.net_pnl for r in reports.values())
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0

        gw = sum(sum(t["net_pnl"] for t in r.trades if t["net_pnl"] > 0) for r in reports.values())
        gl = abs(sum(sum(t["net_pnl"] for t in r.trades if t["net_pnl"] < 0) for r in reports.values()))
        pf = round(gw / gl, 2) if gl > 0 else 0.0

        avg_mdd_pct = np.mean([r.max_drawdown_pct for r in reports.values()])
        max_mdd_inr = max(r.max_drawdown_inr for r in reports.values())
        exp_inr = round(net_pnl / total_trades, 2) if total_trades > 0 else 0.0

        return {
            "total_trades": total_trades,
            "winning_trades": wins,
            "win_rate_pct": round(win_rate, 2),
            "gross_pnl": round(gross_pnl, 2),
            "total_costs": round(costs, 2),
            "net_pnl": round(net_pnl, 2),
            "profit_factor": pf,
            "max_drawdown_inr": round(max_mdd_inr, 2),
            "max_drawdown_pct": round(avg_mdd_pct, 2),
            "expectancy_inr": exp_inr
        }

    baseline_summary = aggregate(baseline_reports)
    enhanced_summary = aggregate(enhanced_reports)

    comparison = {
        "symbols": symbols,
        "initial_capital_per_asset": 25000.0,
        "baseline_unfiltered": baseline_summary,
        "enhanced_5layer": enhanced_summary,
        "improvements": {
            "net_pnl_delta": round(enhanced_summary["net_pnl"] - baseline_summary["net_pnl"], 2),
            "win_rate_delta_pct": round(enhanced_summary["win_rate_pct"] - baseline_summary["win_rate_pct"], 2),
            "drawdown_reduction_pct": round(baseline_summary["max_drawdown_pct"] - enhanced_summary["max_drawdown_pct"], 2),
            "profit_factor_delta": round(enhanced_summary["profit_factor"] - baseline_summary["profit_factor"], 2),
            "cost_drag_reduction_pct": round(
                ((baseline_summary["total_costs"] - enhanced_summary["total_costs"]) / baseline_summary["total_costs"] * 100), 2
            ) if baseline_summary["total_costs"] > 0 else 0.0
        }
    }

    return comparison


if __name__ == "__main__":
    comp = run_benchmark_comparison()
    print("==================================================================")
    print("        BEFORE VS AFTER STRATEGY BENCHMARK RESULTS                ")
    print("==================================================================")
    print(f"Metrics (₹25,000 initial balance, NSE Indian Costs):")
    print(f"  Baseline Net P&L     : ₹{comp['baseline_unfiltered']['net_pnl']:+,.2f}")
    print(f"  Enhanced Net P&L     : ₹{comp['enhanced_5layer']['net_pnl']:+,.2f}")
    print(f"  Net P&L Improvement  : ₹{comp['improvements']['net_pnl_delta']:+,.2f}")
    print(f"  Win Rate (Base vs Enh): {comp['baseline_unfiltered']['win_rate_pct']:.1f}% -> {comp['enhanced_5layer']['win_rate_pct']:.1f}%")
    print(f"  Profit Factor        : {comp['baseline_unfiltered']['profit_factor']:.2f} -> {comp['enhanced_5layer']['profit_factor']:.2f}")
    print(f"  Avg Max Drawdown     : {comp['baseline_unfiltered']['max_drawdown_pct']:.2f}% -> {comp['enhanced_5layer']['max_drawdown_pct']:.2f}%")
    print(f"  Total Trades (Churn) : {comp['baseline_unfiltered']['total_trades']} -> {comp['enhanced_5layer']['total_trades']}")
    print(f"  Cost Drag Reduction  : {comp['improvements']['cost_drag_reduction_pct']:.1f}% less tax/brokerage churn")
    print("==================================================================")

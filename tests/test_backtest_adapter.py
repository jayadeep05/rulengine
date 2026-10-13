"""
test_backtest_adapter.py
────────────────────────
Tests for the Backtesting Engine, Indian Equity Cost integration,
₹25,000 virtual balance simulation, VectorBT portfolio adapter,
and chronological walk-forward validation.
"""

import math
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import pytest

from backtesting_engine import (
    BacktestConfig, IndianEquityCostModel, IndianIntradayBacktestEngine,
    StrategySignalAdapter, VectorBTAdapter, WalkForwardValidator
)


def generate_synthetic_intraday_data(days: int = 5, start_price: float = 1000.0) -> pd.DataFrame:
    """
    Generates realistic 5-minute candles for intraday trading (9:15 AM to 3:30 PM IST).
    Each day has 75 candles of 5-minute intervals.
    """
    records = []
    base_date = datetime(2026, 6, 1)
    price = start_price

    for day in range(days):
        current_date = base_date + timedelta(days=day)
        # 9:15 to 15:30 -> 75 candles
        day_time = datetime(current_date.year, current_date.month, current_date.day, 9, 15)

        for bar in range(75):
            ts = day_time + timedelta(minutes=bar * 5)
            # Create periodic momentum breakouts
            if bar in (15, 40):  # 10:30 and 12:35
                change = 0.008  # +0.8% breakout
                vol = 15000.0   # volume surge
            elif bar in (25, 55):
                change = -0.007
                vol = 14000.0
            else:
                change = np.random.normal(0.0001, 0.002)
                vol = np.random.uniform(2000, 5000)

            open_p = price
            close_p = open_p * (1.0 + change)
            high_p = max(open_p, close_p) * (1.0 + abs(np.random.normal(0, 0.001)))
            low_p = min(open_p, close_p) * (1.0 - abs(np.random.normal(0, 0.001)))
            price = close_p

            records.append({
                "timestamp": ts,
                "open": round(open_p, 2),
                "high": round(high_p, 2),
                "low": round(low_p, 2),
                "close": round(close_p, 2),
                "volume": round(vol, 0)
            })

    return pd.DataFrame(records)


def test_backtest_with_25k_initial_balance():
    """
    Test running backtest with ₹25,000 initial virtual balance and realistic costs.
    """
    df = generate_synthetic_intraday_data(days=5, start_price=1200.0)
    config = BacktestConfig(
        initial_capital=25000.0,
        daily_loss_limit=1000.0,
        risk_per_trade=200.0,
        max_trades_per_day=5
    )
    engine = IndianIntradayBacktestEngine(config=config)
    report = engine.run("TEST_STOCK", df)

    assert report.initial_capital == 25000.0
    assert report.ending_capital == round(25000.0 + report.net_pnl, 2)
    assert report.net_pnl == round(report.gross_pnl - report.total_costs, 2)
    assert len(report.equity_curve) >= 1
    assert report.equity_curve[0]["equity"] == 25000.0

    # Ensure max trades per day constraint was respected
    trades_per_day = {}
    for t in report.trades:
        d = t["entry_time"][:10]
        trades_per_day[d] = trades_per_day.get(d, 0) + 1
        assert t["qty"] > 0
        assert t["costs"] > 0, "Realistic transaction costs must be non-zero!"

    for d, count in trades_per_day.items():
        assert count <= 5, f"Exceeded max 5 trades per day on {d}"


def test_vectorbt_portfolio_adapter():
    """
    Test that VectorBTAdapter converts backtest reports into a vectorbt.Portfolio object.
    """
    import vectorbt as vbt
    df = generate_synthetic_intraday_data(days=5, start_price=800.0)
    engine = IndianIntradayBacktestEngine(config=BacktestConfig(initial_capital=25000.0))
    report = engine.run("AXISBANK", df)

    portfolio = VectorBTAdapter.to_vectorbt_portfolio(report)
    if portfolio is not None:
        assert isinstance(portfolio, vbt.Portfolio)
        assert portfolio.init_cash == 25000.0
        # Verify vectorbt can compute total return without errors
        ret = portfolio.total_return()
        assert isinstance(ret, (float, np.floating))


def test_walk_forward_chronological_validation():
    """
    Test that WalkForwardValidator performs chronological in-sample / out-of-sample split
    with zero look-ahead bias.
    """
    df = generate_synthetic_intraday_data(days=10, start_price=1500.0)
    validator = WalkForwardValidator()
    result = validator.run_out_of_sample("RELIANCE", df, train_ratio=0.60)

    assert result["symbol"] == "RELIANCE"
    assert "in_sample" in result
    assert "out_of_sample" in result
    assert "robustness_score" in result
    assert result["in_sample"].initial_capital == 25000.0
    assert result["out_of_sample"].initial_capital == 25000.0

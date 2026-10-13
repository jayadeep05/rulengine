"""
test_cost_model.py
──────────────────
Tests for Indian Intraday Equity Transaction Cost Model (NSE).
Validates brokerage, STT, turnover charges, SEBI charges, stamp duty, GST, and slippage.
"""

from backtesting_engine import IndianEquityCostModel


def test_brokerage_cap_applied():
    """
    Test that for high turnover trades, brokerage is capped at ₹20 per leg (₹40 round trip).
    """
    cost_model = IndianEquityCostModel()
    # Turnover = 100 * 2500 = ₹2,50,000 per leg
    # 0.05% of ₹2,50,000 = ₹125 -> capped at ₹20 per leg
    breakdown = cost_model.calculate(buy_price=2500.0, sell_price=2520.0, qty=100)

    assert breakdown.brokerage == 40.0, f"Expected brokerage 40.0, got {breakdown.brokerage}"
    assert breakdown.buy_turnover == 250000.0
    assert breakdown.sell_turnover == 252000.0


def test_brokerage_percent_when_under_cap():
    """
    Test that for small turnover trades, 0.05% is charged if less than ₹20.
    """
    cost_model = IndianEquityCostModel()
    # Turnover = 10 * 100 = ₹1,000
    # 0.05% of ₹1,000 = ₹0.50 per leg -> Total ₹1.00
    breakdown = cost_model.calculate(buy_price=100.0, sell_price=102.0, qty=10)
    assert breakdown.brokerage < 2.0


def test_stt_only_on_sell_leg():
    """
    Test that STT (0.025%) is levied strictly on the SELL leg for intraday equity.
    """
    cost_model = IndianEquityCostModel(stt_pct=0.00025)
    # Sell turnover = 2520.0 * 100 = ₹2,52,000
    # STT = 252000 * 0.00025 = ₹63.00
    breakdown = cost_model.calculate(buy_price=2500.0, sell_price=2520.0, qty=100)
    expected_stt = round(252000.0 * 0.00025, 2)
    assert breakdown.stt == expected_stt


def test_gst_calculation():
    """
    Test that GST (18%) is applied on brokerage + exchange charges + SEBI charges.
    """
    cost_model = IndianEquityCostModel()
    breakdown = cost_model.calculate(buy_price=1000.0, sell_price=1010.0, qty=50)

    taxable_sum = breakdown.brokerage + breakdown.exchange_charges + breakdown.sebi_charges
    expected_gst = round(taxable_sum * 0.18, 2)
    assert abs(breakdown.gst - expected_gst) <= 0.02


def test_total_cost_includes_all_components():
    """
    Test that total_costs equals the sum of all individual fees.
    """
    cost_model = IndianEquityCostModel()
    b = cost_model.calculate(buy_price=500.0, sell_price=510.0, qty=20)
    manual_sum = round(
        b.brokerage + b.stt + b.exchange_charges + b.sebi_charges + b.stamp_duty + b.gst + b.slippage_cost,
        2
    )
    assert abs(b.total_costs - manual_sum) <= 0.02

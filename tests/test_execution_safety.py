"""
test_execution_safety.py
────────────────────────
Proves that paper trading mode cannot make network calls to Upstox broker endpoints,
validates paper mode isolation, and verifies security guards on live order routing.
"""

import pytest
from unittest.mock import patch, MagicMock
from config import Config
from execution import UpstoxExecutionEngine, PaperExecutionEngine


def test_paper_trading_mode_blocks_network_calls():
    """
    Test that when is_paper is True (via Config.PAPER_TRADING_MODE or mode="TEST"),
    place_order_with_retry, place_stop_loss, modify_order, and cancel_order
    DO NOT make any HTTP network requests to broker endpoints.
    """
    engine = UpstoxExecutionEngine(mode="TEST")
    assert engine.is_paper is True

    with patch("requests.post") as mock_post, \
         patch("requests.put") as mock_put, \
         patch("requests.delete") as mock_delete:

        # 1. Place order
        res_order = engine.place_order_with_retry(
            symbol="NSE_EQ|INE002A01018",
            side="BUY",
            qty=10,
            order_type="MARKET",
            price=2500.0
        )
        assert res_order["status"] == "success"
        assert res_order.get("paper_trade") is True
        assert "paper_ord_" in res_order["order_id"]
        assert mock_post.call_count == 0, "Security violation: Network request made during paper order placement!"

        # 2. Place stop loss
        res_sl = engine.place_stop_loss(
            symbol="NSE_EQ|INE002A01018",
            side="BUY",
            qty=10,
            sl_price=2480.0
        )
        assert res_sl["status"] == "success"
        assert res_sl.get("paper_trade") is True
        assert "paper_sl_" in res_sl["order_id"]
        assert mock_post.call_count == 0, "Security violation: Network request made during paper SL placement!"

        # 3. Modify order
        res_mod = engine.modify_order(
            order_id="paper_sl_12345",
            new_price=2490.0,
            new_trigger=2490.0,
            qty=10
        )
        assert res_mod["status"] == "success"
        assert res_mod.get("paper_trade") is True
        assert mock_put.call_count == 0, "Security violation: Network request made during paper modify order!"

        # 4. Cancel order
        res_cancel = engine.cancel_order("paper_sl_12345")
        assert res_cancel["status"] == "success"
        assert res_cancel.get("paper_trade") is True
        assert mock_delete.call_count == 0, "Security violation: Network request made during paper cancel order!"

        # 5. Exit all positions
        res_exit = engine.exit_all_positions()
        assert res_exit["status"] == "success"
        assert res_exit.get("paper_trade") is True
        assert mock_post.call_count == 0, "Security violation: Network request made during paper exit all positions!"


def test_paper_execution_engine_isolation():
    """
    Test that PaperExecutionEngine is strictly isolated and maintains internal virtual state.
    """
    paper_engine = PaperExecutionEngine(initial_balance=25000.0)
    assert paper_engine.is_paper is True
    assert paper_engine.get_funds() == 25000.0

    with patch("requests.post") as mock_post:
        res = paper_engine.place_order_with_retry("INFY", "BUY", 15, price=1800.0)
        assert res["status"] == "success"
        assert res["filled_quantity"] == 15
        assert res["paper_trade"] is True
        assert mock_post.call_count == 0

        book = paper_engine.get_order_book()
        assert len(book) == 1
        assert book[0]["symbol"] == "INFY"
        assert book[0]["quantity"] == 15

        status = paper_engine.get_order_status(res["order_id"])
        assert status["status"] == "success"
        assert status["filled_quantity"] == 15


def test_live_order_without_credentials_is_rejected():
    """
    Test that attempting to place an order in LIVE mode without active Upstox credentials
    is strictly rejected and does not make a raw network call.
    """
    live_engine = UpstoxExecutionEngine(credentials={"upstox_access_token": ""}, mode="LIVE")
    # Temporarily override Config.PAPER_TRADING_MODE to test the live guard
    orig_paper = Config.PAPER_TRADING_MODE
    try:
        Config.PAPER_TRADING_MODE = False
        with patch("requests.post") as mock_post:
            res = live_engine.place_order_with_retry("RELIANCE", "BUY", 5, price=2500.0)
            assert res["status"] == "rejected"
            assert "Missing live" in res["reason"]
            assert mock_post.call_count == 0
    finally:
        Config.PAPER_TRADING_MODE = orig_paper

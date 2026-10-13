"""
test_fastapi_endpoints.py
─────────────────────────
Tests the FastAPI Backtesting and Benchmark endpoints.
"""

from fastapi.testclient import TestClient
import pytest
from main import app
from auth_middleware import require_auth


@pytest.fixture
def client():
    # Override auth dependency for tests
    app.dependency_overrides[require_auth] = lambda: {"user_id": 1, "username": "tester"}
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_api_run_backtest(client):
    payload = {
        "symbol": "RELIANCE",
        "initial_capital": 25000.0,
        "daily_loss_limit": 1000.0,
        "risk_per_trade": 200.0,
        "days": 5,
        "trailing_sl_mode": "ADAPTIVE"
    }
    response = client.post("/api/v2/backtest/run", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["initial_capital"] == 25000.0
    assert "net_pnl" in data
    assert "win_rate_pct" in data
    assert "profit_factor" in data
    assert "equity_curve" in data


def test_api_walk_forward(client):
    payload = {
        "symbol": "INFY",
        "initial_capital": 25000.0,
        "days": 6
    }
    response = client.post("/api/v2/backtest/walk-forward", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["symbol"] == "INFY"
    assert "in_sample" in data
    assert "out_of_sample" in data
    assert "robustness_score" in data


def test_api_benchmark_comparison(client):
    response = client.get("/api/v2/backtest/benchmark")
    assert response.status_code == 200
    data = response.json()
    assert "baseline_unfiltered" in data
    assert "enhanced_5layer" in data
    assert "improvements" in data

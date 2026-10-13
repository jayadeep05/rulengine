"""
test_ai_evaluators.py
─────────────────────
Tests for AI multi-model evaluators and deterministic risk controls.
Proves that AI decisions are validated against strict Pydantic schemas and
CANNOT override deterministic risk bounds (SL limit, R:R >= 2x, ATR constraints).
"""

from ai_evaluator import (
    TradeDecisionOutput, DecisionEnum, DirectionEnum,
    clean_json_response, parse_structured_decisions,
    enforce_deterministic_risk_controls, MultiModelEvaluator
)


def test_clean_json_response_with_markdown_fences():
    """Test that clean_json_response strips fences, preambles, and extracts valid JSON."""
    raw = (
        "Here is my analysis of the setups:\n"
        "```json\n"
        "[\n"
        "  {\"symbol\": \"RELIANCE\", \"decision\": \"ENTER\", \"direction\": \"LONG\", \"confidence\": 4, \"reasoning\": \"Clean consolidation breakout. Strong volume surge.\", \"red_flags\": \"none\"}\n"
        "]\n"
        "```\n"
        "Good luck with your trading!"
    )
    cleaned = clean_json_response(raw)
    assert cleaned.startswith("[")
    assert cleaned.endswith("]")


def test_pydantic_schema_validation_and_confidence_clamping():
    """Test that TradeDecisionOutput parses valid attributes and clamps confidence."""
    # Confidence > 5 clamped to 5
    d = TradeDecisionOutput(
        symbol="TCS",
        decision=DecisionEnum.ENTER,
        direction=DirectionEnum.LONG,
        confidence=10,
        reasoning="Strong breakout. Sector aligned."
    )
    assert d.confidence == 5

    # Confidence < 1 clamped to 1
    d2 = TradeDecisionOutput(
        symbol="INFY",
        decision=DecisionEnum.SKIP,
        direction=DirectionEnum.SHORT,
        confidence=-2,
        reasoning="Too close to support. Low conviction."
    )
    assert d2.confidence == 1


def test_deterministic_risk_controls_blocks_failed_rr():
    """
    Test that even if AI outputs ENTER with confidence 5,
    if the R:R ratio is below 1.95x, deterministic risk gate FORCES SKIP.
    """
    decision = TradeDecisionOutput(
        symbol="HDFCBANK",
        decision=DecisionEnum.ENTER,
        direction=DirectionEnum.LONG,
        confidence=5,
        reasoning="Looks great. Strong volume.",
        red_flags="none"
    )

    # Entry: 1000, SL: 950 (Risk = 50), Target: 1050 (Reward = 50 -> R:R = 1.0x < 1.95x)
    payload = {
        "calculated_entry": 1000.0,
        "calculated_sl": 950.0,
        "calculated_target": 1050.0,
        "price_context": {"atr_14_5min": 40.0}
    }

    result = enforce_deterministic_risk_controls(decision, payload)
    assert result["decision"] == "SKIP"
    assert result["risk_gate_passed"] is False
    assert "below 1.95x" in result["override_reason"]


def test_deterministic_risk_controls_blocks_excessive_sl_distance():
    """
    Test that if SL distance exceeds 1.55x ATR, AI ENTER is overridden to SKIP.
    """
    decision = TradeDecisionOutput(
        symbol="ICICIBANK",
        decision=DecisionEnum.ENTER,
        direction=DirectionEnum.LONG,
        confidence=4,
        reasoning="Breakout setup.",
        red_flags="none"
    )

    # ATR = 10, Risk = 20 (2.0x ATR > 1.55x ATR limit)
    payload = {
        "calculated_entry": 1000.0,
        "calculated_sl": 980.0,
        "calculated_target": 1050.0,
        "price_context": {"atr_14_5min": 10.0}
    }

    result = enforce_deterministic_risk_controls(decision, payload)
    assert result["decision"] == "SKIP"
    assert result["risk_gate_passed"] is False
    assert "exceeds 1.55x ATR" in result["override_reason"]


def test_deterministic_risk_controls_allows_valid_setup():
    """
    Test that when mathematical geometry and AI agreement both pass, ENTER is preserved.
    """
    decision = TradeDecisionOutput(
        symbol="SBIN",
        decision=DecisionEnum.ENTER,
        direction=DirectionEnum.LONG,
        confidence=4,
        reasoning="Consolidation breakout. Clean volume expansion.",
        red_flags="none"
    )

    # ATR = 10, Risk = 10 (1.0x ATR <= 1.55x ATR), Target = 1020 (Reward = 20 -> R:R = 2.0x >= 1.95x)
    payload = {
        "calculated_entry": 1000.0,
        "calculated_sl": 990.0,
        "calculated_target": 1020.0,
        "price_context": {"atr_14_5min": 10.0}
    }

    result = enforce_deterministic_risk_controls(decision, payload)
    assert result["decision"] == "ENTER"
    assert result["risk_gate_passed"] is True
    assert result["rr_ratio"] == 2.0

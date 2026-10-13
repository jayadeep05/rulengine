"""
ai_evaluator.py — Multi-Model AI Evaluator & Deterministic Risk Guard
────────────────────────────────────────────────────────────────────────
Provides optional multi-model AI evaluation (Claude + Second AI Model e.g. Groq/OpenAI)
with Pydantic schema validation, consensus mechanisms, and deterministic risk controls
that CANNOT be overridden by AI outputs.
"""

import os
import json
import logging
import re
from typing import List, Dict, Any, Optional
from enum import Enum
from pydantic import BaseModel, Field, field_validator
from config import Config

logger = logging.getLogger(__name__)


class DecisionEnum(str, Enum):
    ENTER = "ENTER"
    SKIP = "SKIP"


class DirectionEnum(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class TradeDecisionOutput(BaseModel):
    symbol: str
    decision: DecisionEnum
    direction: DirectionEnum
    confidence: int = 1
    reasoning: str
    red_flags: str = "none"
    model_name: Optional[str] = "unknown"

    @field_validator("confidence", mode="before")
    @classmethod
    def clamp_confidence(cls, v: Any) -> int:
        try:
            return max(1, min(5, int(v)))
        except Exception:
            return 1


SYSTEM_EVALUATOR_PROMPT = """You are a disciplined, risk-first intraday trading setup validator for Indian equity markets (NSE).
Your task is to analyze pre-screened 5-minute candle setups and output ONLY a JSON array with validation decisions.

OUTPUT FORMAT:
Return ONLY a valid JSON array of objects. Exactly one object per stock in the same order as input.
Do NOT include markdown fences, preambles, or text outside the JSON.

Schema per object:
{
  "symbol": "<symbol>",
  "decision": "ENTER" or "SKIP",
  "direction": "LONG" or "SHORT",
  "confidence": <integer from 1 to 5>,
  "reasoning": "<2 concise sentences explaining why ENTER or naming the specific flaw triggering SKIP>",
  "red_flags": "<comma-separated list of flaws, or 'none'>"
}

DECISION CRITERIA:
1. Confidence 1-2: Always SKIP.
2. Confidence 3: ENTER only if 0 red flags.
3. Confidence 4-5: ENTER allowed with minor red flags.
4. If market regime is RANGING or HIGH_VOLATILITY: require confidence >= 4 to ENTER.
5. If VIX > 18: require confidence >= 4 to ENTER.
6. If stock price action is choppy or lacks volume breakout conviction: SKIP.
"""


def clean_json_response(raw_text: str) -> str:
    """Strips markdown fences and surrounding commentary to isolate JSON."""
    text = raw_text.strip()
    if "```" in text:
        # Extract content between ```json and ``` or between ``` and ```
        match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
        if match:
            text = match.group(1).strip()
    # Find start array [ and end array ]
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        return text[start:end+1]
    return text


class ClaudeEvaluator:
    """Evaluates setups using Anthropic Claude API."""
    def __init__(self, api_key: str = None, model: str = None):
        self.api_key = api_key or Config.ANTHROPIC_API_KEY
        self.model = model or Config.ANTHROPIC_MODEL

    def evaluate(self, payloads: List[Dict[str, Any]], market_context: Dict[str, Any]) -> List[TradeDecisionOutput]:
        if not self.api_key or not payloads:
            return []
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=self.api_key)
            prompt = (
                f"Analyze these intraday setups.\n"
                f"Market context: Nifty trend is {market_context.get('nifty_trend', 'sideways')}, "
                f"VIX is {market_context.get('vix', 15.0)}.\n\n"
                f"{json.dumps(payloads, indent=2)}"
            )
            resp = client.messages.create(
                model=self.model,
                max_tokens=2000,
                temperature=0.0,
                system=SYSTEM_EVALUATOR_PROMPT,
                messages=[{"role": "user", "content": prompt}]
            )
            raw = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
            return parse_structured_decisions(raw, payloads, model_name=f"claude-{self.model}")
        except Exception as e:
            logger.error(f"[ClaudeEvaluator] Failed: {e}")
            return []


class SecondAIEvaluator:
    """
    Evaluates setups using free or open-source AI providers:
      - Groq (Free tier: Llama 3.3 70B, Llama 3.1 8B at console.groq.com)
      - Google Gemini (Free tier: Gemini 1.5 Flash, Gemini 2.0 Flash at aistudio.google.com)
      - OpenRouter (Free models: Llama 3.3 70B Free, DeepSeek R1 Free at openrouter.ai)
      - Ollama (100% Free local: Llama 3.2, Qwen 2.5 on localhost:11434)
      - OpenAI (Paid/Credits: GPT-4o-mini at platform.openai.com)
    """
    def __init__(self, provider: str = None, api_key: str = None, model: str = None, base_url: str = None):
        self.provider = (provider or getattr(Config, "SECOND_AI_PROVIDER", "groq")).lower()
        self.api_key = api_key or getattr(Config, "SECOND_AI_API_KEY", "")
        self.base_url = base_url

        # Default model selection per provider
        default_models = {
            "groq": "llama-3.3-70b-versatile",
            "gemini": "gemini-1.5-flash",
            "openrouter": "meta-llama/llama-3.3-70b-instruct:free",
            "ollama": "llama3.2",
            "openai": "gpt-4o-mini"
        }
        self.model = model or getattr(Config, "SECOND_AI_MODEL", "") or default_models.get(self.provider, "llama-3.3-70b-versatile")

    def evaluate(self, payloads: List[Dict[str, Any]], market_context: Dict[str, Any]) -> List[TradeDecisionOutput]:
        if not payloads:
            return []
        if not self.api_key and self.provider != "ollama":
            logger.info(f"[SecondAIEvaluator] No API key configured for provider '{self.provider}'. Skipping call.")
            return []

        try:
            import requests

            prompt = (
                f"Analyze these intraday setups.\n"
                f"Market context: Nifty trend is {market_context.get('nifty_trend', 'sideways')}, "
                f"VIX is {market_context.get('vix', 15.0)}.\n\n"
                f"{json.dumps(payloads, indent=2)}"
            )

            endpoint_map = {
                "groq": "https://api.groq.com/openai/v1/chat/completions",
                "openai": "https://api.openai.com/v1/chat/completions",
                "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                "openrouter": "https://openrouter.ai/api/v1/chat/completions",
                "ollama": os.getenv("OLLAMA_HOST", "http://localhost:11434/v1/chat/completions")
            }
            url = self.base_url or endpoint_map.get(self.provider, endpoint_map["groq"])

            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            if self.provider == "openrouter":
                headers["HTTP-Referer"] = "https://github.com/jayadeep05/rulengine"
                headers["X-Title"] = "Intrabot Trading Engine"

            body = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": SYSTEM_EVALUATOR_PROMPT},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.0
            }
            if "llama-3.3" in self.model or "json" in self.model.lower():
                body["response_format"] = {"type": "json_object"}

            res = requests.post(url, headers=headers, json=body, timeout=15)
            res.raise_for_status()
            raw = res.json()["choices"][0]["message"]["content"]
            return parse_structured_decisions(raw, payloads, model_name=f"{self.provider}-{self.model}")

        except Exception as e:
            logger.error(f"[SecondAIEvaluator] Failed via {self.provider} ({self.model}): {e}")
            return []


def parse_structured_decisions(raw_text: str, original_payloads: List[Dict[str, Any]], model_name: str = "ai") -> List[TradeDecisionOutput]:
    """Parses and strictly validates AI output into Pydantic models."""
    cleaned = clean_json_response(raw_text)
    try:
        data = json.loads(cleaned)
        # In case wrapped in {"decisions": [...]}
        if isinstance(data, dict):
            for k in ["decisions", "results", "trades"]:
                if k in data and isinstance(data[k], list):
                    data = data[k]
                    break
            if isinstance(data, dict):
                data = [data]
    except Exception as e:
        logger.error(f"[AI Parsing] JSON Decode Error: {e}. Raw text sample: {raw_text[:200]}")
        return []

    if not isinstance(data, list):
        return []

    valid_symbols = {p.get("symbol") for p in original_payloads}
    results = []

    for item in data:
        if not isinstance(item, dict):
            continue
        sym = item.get("symbol")
        if sym not in valid_symbols:
            continue

        try:
            decision_obj = TradeDecisionOutput(
                symbol=sym,
                decision=DecisionEnum(str(item.get("decision", "SKIP")).upper()),
                direction=DirectionEnum(str(item.get("direction", "LONG")).upper()),
                confidence=int(item.get("confidence", 1)),
                reasoning=str(item.get("reasoning", "")),
                red_flags=str(item.get("red_flags", "none")),
                model_name=model_name
            )
            results.append(decision_obj)
        except Exception as val_err:
            logger.warning(f"[AI Validation] Invalid structure for {sym}: {val_err}. Defaulting to SKIP.")
            results.append(TradeDecisionOutput(
                symbol=sym,
                decision=DecisionEnum.SKIP,
                direction=DirectionEnum.LONG,
                confidence=1,
                reasoning=f"Failed validation schema: {val_err}",
                red_flags="invalid_schema",
                model_name=model_name
            ))

    return results


def run_local_rule_evaluator(payloads: List[Dict[str, Any]]) -> List[TradeDecisionOutput]:
    """
    Deterministic rule-based evaluator that mirrors the trading rules.
    Runs locally without external API dependencies.
    """
    decisions = []
    for p in payloads:
        symbol = p.get("symbol", "UNKNOWN")
        direction = p.get("signal_direction", "LONG")
        price_ctx = p.get("price_context", {})
        levels = p.get("levels", {})
        volume = p.get("volume", {})
        mkt = p.get("market_context", {})

        ltp = price_ctx.get("ltp", 0.0)
        atr = price_ctx.get("atr_14_5min", 1.0)
        vwap_pct = price_ctx.get("price_vs_vwap_pct", 0.0)
        dist_res = levels.get("distance_to_resistance_pct", 1.0)
        dist_sup = levels.get("distance_to_support_pct", 1.0)
        vol_ratio = volume.get("vol_ratio_6bar_5min", 1.0)
        vol_trend = volume.get("volume_trend", "flat")
        vix = mkt.get("india_vix", 15.0)

        red_flags = []
        is_skip = False
        reason = "Meets quantitative criteria."

        # Rule checks
        if direction == "LONG" and vwap_pct <= 0:
            is_skip = True
            red_flags.append("below_vwap")
            reason = "Price is at or below VWAP for LONG."
        elif direction == "SHORT" and vwap_pct >= 0:
            is_skip = True
            red_flags.append("above_vwap")
            reason = "Price is at or above VWAP for SHORT."

        if direction == "LONG" and dist_res < 0.3:
            is_skip = True
            red_flags.append("near_resistance")
            reason = "Too close to 20-bar resistance."
        elif direction == "SHORT" and dist_sup < 0.3:
            is_skip = True
            red_flags.append("near_support")
            reason = "Too close to 20-bar support."

        if vol_trend == "contracting" and vol_ratio < 1.5:
            is_skip = True
            red_flags.append("volume_contracting")
            reason = "Volume contracting and volume ratio < 1.5x."

        if vix > 25.0:
            is_skip = True
            red_flags.append("high_vix")
            reason = f"India VIX {vix:.1f} > 25.0 extreme volatility."

        confidence = 3
        if vol_ratio >= 2.5:
            confidence += 1
        if mkt.get("nifty_trend_today") == ("bullish" if direction == "LONG" else "bearish"):
            confidence += 1

        if is_skip:
            decision = DecisionEnum.SKIP
            confidence = min(2, confidence)
        else:
            decision = DecisionEnum.ENTER if confidence >= 3 else DecisionEnum.SKIP

        decisions.append(TradeDecisionOutput(
            symbol=symbol,
            decision=decision,
            direction=DirectionEnum(direction),
            confidence=confidence,
            reasoning=reason,
            red_flags=",".join(red_flags) if red_flags else "none",
            model_name="local_rule_engine"
        ))

    return decisions


def enforce_deterministic_risk_controls(
    decision: TradeDecisionOutput,
    payload: Dict[str, Any]
) -> Dict[str, Any]:
    """
    CRITICAL RISK GATE:
    Never let AI outputs override mathematical and deterministic risk controls.
    Enforces SL bounds, R:R >= 2.0x, max ATR risk, and pre-filter checks.
    
    Returns:
        dict containing verified trade geometry and final decision.
    """
    symbol = decision.symbol
    direction = decision.direction.value
    raw_decision = decision.decision.value
    confidence = decision.confidence

    entry = payload.get("calculated_entry", 0.0)
    sl = payload.get("calculated_sl", 0.0)
    target = payload.get("calculated_target", 0.0)
    price_ctx = payload.get("price_context", {})
    atr = price_ctx.get("atr_14_5min", 0.0)

    final_decision = raw_decision
    override_reason = None

    # Check 1: Incomplete or missing mathematical targets
    if entry <= 0 or sl <= 0 or target <= 0:
        final_decision = "SKIP"
        override_reason = "Missing deterministic targets from Layer 4"

    # Check 2: SL geometry validation
    risk = abs(entry - sl)
    reward = abs(target - entry)

    if risk <= 0:
        final_decision = "SKIP"
        override_reason = "Risk is zero or invalid"

    # Check 3: Max SL constraint (must not exceed 1.55x ATR)
    if atr > 0 and risk > (1.55 * atr):
        final_decision = "SKIP"
        override_reason = f"SL risk ({risk:.2f}) exceeds 1.55x ATR ({1.55*atr:.2f})"

    # Check 4: R:R ratio constraint (must be at least 1.95x target)
    if risk > 0 and (reward / risk) < 1.95:
        final_decision = "SKIP"
        override_reason = f"R:R ratio ({reward/risk:.2f}x) is below 1.95x minimum"

    # Check 5: AI Confidence gate
    if confidence < 3 and final_decision == "ENTER":
        final_decision = "SKIP"
        override_reason = f"AI confidence ({confidence}) below minimum threshold 3"

    # Log any override
    if raw_decision == "ENTER" and final_decision == "SKIP":
        logger.warning(
            f"[DETERMINISTIC RISK GUARD] AI recommended ENTER for {symbol}, "
            f"but deterministic risk controls FORCED SKIP: {override_reason}"
        )

    return {
        "symbol": symbol,
        "decision": final_decision,
        "direction": direction,
        "entry_price": entry,
        "stop_loss": sl,
        "target": target,
        "risk": risk,
        "reward": reward,
        "rr_ratio": round(reward / risk, 2) if risk > 0 else 0.0,
        "confidence": confidence,
        "ai_model": decision.model_name,
        "reasoning": decision.reasoning,
        "red_flags": decision.red_flags,
        "risk_gate_passed": (final_decision == "ENTER"),
        "override_reason": override_reason
    }


class MultiModelEvaluator:
    """
    Coordinates evaluation across Claude, Second AI model, and Local Rule Engine.
    Supports consensus modes and enforces deterministic risk controls.
    """
    def __init__(self):
        self.claude = ClaudeEvaluator()
        self.second_ai = SecondAIEvaluator()

    def evaluate_setups(
        self,
        payloads: List[Dict[str, Any]],
        market_context: Dict[str, Any] = None,
        evaluator_mode: str = None
    ) -> List[Dict[str, Any]]:
        """
        Args:
            payloads: Layer 4 feature payloads
            market_context: Dict containing Nifty trend, VIX, etc.
            evaluator_mode: "claude", "second_ai", "consensus", "local_rule"
        """
        if not payloads:
            return []
        market_context = market_context or {}
        mode = evaluator_mode or getattr(Config, "ACTIVE_AI_EVALUATOR", "local_rule")

        # 1. Fetch AI decisions based on chosen mode
        decisions: List[TradeDecisionOutput] = []

        if mode == "claude" and Config.ANTHROPIC_API_KEY:
            decisions = self.claude.evaluate(payloads, market_context)
        elif mode == "second_ai":
            decisions = self.second_ai.evaluate(payloads, market_context)
        elif mode == "consensus":
            # Both Claude and Second AI must evaluate; ENTER only if BOTH approve
            claude_res = {d.symbol: d for d in self.claude.evaluate(payloads, market_context)}
            second_res = {d.symbol: d for d in self.second_ai.evaluate(payloads, market_context)}

            for p in payloads:
                sym = p["symbol"]
                c_dec = claude_res.get(sym)
                s_dec = second_res.get(sym)

                if c_dec and s_dec:
                    if c_dec.decision == DecisionEnum.ENTER and s_dec.decision == DecisionEnum.ENTER:
                        decisions.append(TradeDecisionOutput(
                            symbol=sym,
                            decision=DecisionEnum.ENTER,
                            direction=c_dec.direction,
                            confidence=min(c_dec.confidence, s_dec.confidence),
                            reasoning=f"Consensus ENTER: Claude ({c_dec.confidence}) + 2nd AI ({s_dec.confidence})",
                            red_flags=f"Claude: {c_dec.red_flags} | 2nd: {s_dec.red_flags}",
                            model_name="consensus(claude+second)"
                        ))
                    else:
                        decisions.append(TradeDecisionOutput(
                            symbol=sym,
                            decision=DecisionEnum.SKIP,
                            direction=c_dec.direction,
                            confidence=max(c_dec.confidence, s_dec.confidence),
                            reasoning=f"Consensus NOT met (Claude={c_dec.decision.value}, 2nd={s_dec.decision.value})",
                            red_flags="consensus_divergence",
                            model_name="consensus(claude+second)"
                        ))

        # Fallback to local rule engine if no decisions or if mode is local_rule
        if not decisions:
            decisions = run_local_rule_evaluator(payloads)

        # 2. Strict Deterministic Risk Gate on each decision
        payload_map = {p["symbol"]: p for p in payloads}
        guarded_results = []
        for d in decisions:
            p = payload_map.get(d.symbol, {})
            guarded = enforce_deterministic_risk_controls(d, p)
            guarded_results.append(guarded)

        return guarded_results

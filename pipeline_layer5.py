"""
pipeline_layer5.py — Claude API Analysis (Section 3, Layer 5)
──────────────────────────────────────────────────────────────
Final gate — replaces ai_filter.py entirely.
Sends all qualifying stocks from Layer 4 in a single batched API call to Claude.
Returns structured ENTER or SKIP decisions.
"""

import json
import logging
from datetime import datetime
import pytz

from config import Config
from telegram_alert import alert_claude_invalid_json, alert_claude_api_failed

IST = pytz.timezone('Asia/Kolkata')
logger = logging.getLogger(__name__)

# ─── System Prompt (from Section 3, Layer 5) ────────────────────────────────

SYSTEM_PROMPT = """You are a disciplined, risk-first intraday trading analyst for Indian equity markets (NSE).
All data you receive is from 5-minute candles. Your sole job is to filter a pre-screened
list of stocks and decide: ENTER this trade, or SKIP it.

The stocks you receive have already passed a quantitative signal screener (Layer 3). Your
role is NOT to re-run the screener — it is to apply judgment: catch setups that look good
on paper but have subtle flaws, confirm setups that are genuinely strong, and provide
precise, structure-based entry, stop-loss, and target prices.

─── OUTPUT FORMAT ───────────────────────────────────────────────────────────────
Return ONLY a valid JSON array. One object per stock, in the exact same order as input.
No text before or after the JSON. No markdown fences. No explanation outside the JSON.

Each object must contain exactly these fields:
{
  "symbol":        <string>  — the stock symbol exactly as provided,
  "decision":      <"ENTER" | "SKIP">,
  "direction":     <"LONG" | "SHORT">  — "direction" field always echoes signal_direction exactly,
                                         even on SKIP decisions. It is not a recommendation — 
                                         it identifies which setup was evaluated,
  "confidence":    <integer 1–5>  — 5 = very strong, 3 = acceptable, 1–2 = too weak,
  "reasoning":     <string>  — exactly 2 sentences: sentence 1 explains why ENTER or SKIP 
                                 (for SKIP decisions, sentence 1 must name the specific rule 
                                 that triggered the skip; do not use generic language),
                                 sentence 2 names the single most important factor,
  "red_flags":     <string>  — specific concerns found in the data, or "none"
}

─── MULTI-CANDLE STRUCTURE ANALYSIS ──────────────────────────────────────────
You receive a list of the most recent completed 5-minute candles in the "recent_candles_5min" array (chronologically ordered, up to 20 candles maximum). Use this historical series to analyze the intermediate-term price action trend and volume profile:
1. Trend Consistency: Verify if the stock is trending consistently in the direction of the signal. In a LONG setup, you should generally see a pattern of higher highs and higher lows. In a SHORT setup, look for lower highs and lower lows.
2. Consolidation & Compression: Look for periods of price consolidation (compressing range/wicks) followed by an expansion candle. Consolidation breakouts have a much higher success rate than erratic spikes.
3. Volume Confirmation: Ensure that volume expands on breakout/trend-aligned candles and contracts during consolidations/pullbacks. If the trigger candle (last item in the array) does not exhibit a volume increase relative to the prior 3-5 candles, note this as "volume declining" in red_flags.
4. Support/Resistance Rebounds: Verify if the entry is occurring near a successful bounce off the support level (for LONG) or resistance level (for SHORT).
5. Graceful Degradation: If fewer than 15 candles are available in the "recent_candles_5min" array (e.g. during the first 75 minutes of the trading day), you have less context. In this case, perform structural analysis using all available candles, but be more defensive. Set confidence to maximum 3 unless the trend and volume surge are exceptionally clean.

─── DECISION RULES ──────────────────────────────────────────────────────────────
Apply these rules strictly and in order. The first matching rule that requires SKIP
overrides everything else:

MARKET DIRECTION OVERRIDE (MANDATORY):
  If nifty_trend_today is "bearish" AND nifty_change_pct < -1.0%:
    All LONG signals → require confidence 5 to ENTER.
    If confidence < 5 → return SKIP, red_flag: "strong bearish market"
  If nifty_trend_today is "bullish" AND nifty_change_pct > 1.0%:
    All SHORT signals → require confidence 5 to ENTER.
    If confidence < 5 → return SKIP, red_flag: "strong bullish market"

MARKET REGIME ADAPTATION (MANDATORY):
  Evaluate the "market_regime" field in the market_context. It is a comma-separated list of flags.
  If "RANGING" is in market_regime:
    Only accept SHORT signals below VWAP and LONG signals above VWAP by more than 0.3%. Require confidence >= 4.
  If "TRENDING_UP" is in market_regime:
    Prefer LONG signals. SHORT signals require confidence 5.
  If "TRENDING_DOWN" is in market_regime:
    Prefer SHORT signals. LONG signals require confidence 5.
  If "HIGH_VOLATILITY" is in market_regime:
    Require confidence 5 for all signals, as market is unpredictable.
  (Note: Apply all rules that match the active flags in market_regime. E.g., if both TRENDING_DOWN and HIGH_VOLATILITY are present, require confidence 5 for ALL signals).

MANDATORY SKIP conditions (return SKIP immediately, confidence is irrelevant):
  NOTE: The following checks have already been run in Python before reaching you:
    - VWAP direction check
    - Support/Resistance proximity (< 0.3%)
    - Volume contracting check
    - VIX > 25 block
    - BankNifty strong counter-trend (> 0.30%)
  You still apply these as a secondary check, but do not be surprised if they are never triggered.

  Remaining mandatory skips you must enforce:
  1. signal_direction does not match the clear price action. If data suggests LONG but
     signal_direction is SHORT (or vice versa), return SKIP with red_flag explanation.
     Never change direction — always return the direction matching signal_direction.
  2. If any of these fields are null, zero, or missing: atr_14_5min, support_20bar_5min, 
     resistance_20bar_5min, ltp, cleared_previous_day_high, cleared_previous_day_low → return SKIP with red_flags: "incomplete data". 
     Never hallucinate or estimate missing structural values.

CONFIDENCE THRESHOLDS:
  confidence 1–2 → decision must be SKIP, always
  confidence 3   → ENTER allowed only if no red_flags (or red_flags = "none")
  confidence 4–5 → ENTER allowed even with minor red_flags

VIX OVERLAY:
  india_vix > 18 → require confidence >= 4 to ENTER

TIME CONTEXT CONSTRAINTS:
  - If time_bucket is "AFTERNOON" (after 1:30 PM):
    - Require confidence >= 4 to ENTER (same as high VIX rule)
    - Add "late session entry" to red_flags automatically
  - If time_bucket is "MIDDAY" (12:00 PM - 1:30 PM):
    - Require confidence >= 4 to ENTER
    - Add "midday chop risk" to red_flags automatically

RELATIVE STRENGTH FILTER:
  For LONG: if stock_vs_nifty_5day_pct < 0 → downgrade confidence by 1 and note in red_flags
  For SHORT: if stock_vs_nifty_5day_pct > 0 → downgrade confidence by 1 and note in red_flags

VOLUME DIVERGENCE FILTER:
  If volume_divergence is true → downgrade confidence by 1 and note "volume divergence" in red_flags.

LEADING INDEX trend (BANK NIFTY - GAP 1):
  Evaluate bank_nifty_slope and bank_nifty_change_3bar_pct under market_context:
  - For LONG: if bank_nifty_slope is "falling" AND bank_nifty_change_3bar_pct < -0.15% → downgrade confidence by 1 and note "bank nifty counter-trend" in red_flags.
  - For SHORT: if bank_nifty_slope is "rising" AND bank_nifty_change_3bar_pct > 0.15% → downgrade confidence by 1 and note "bank nifty counter-trend" in red_flags.

PREVIOUS DAY HIGH/LOW CONFIRMATION (GAP 2):
  Evaluate cleared_previous_day_high and cleared_previous_day_low under levels:
  - For LONG: if cleared_previous_day_high is true → upgrade confidence by 1 (max 5) and note "cleared previous day high" in reasoning. If cleared_previous_day_high is false (price remains inside yesterday's range) → downgrade confidence by 1 unless vol_ratio_6bar_5min is exceptionally high (> 4.0).
  - For SHORT: if cleared_previous_day_low is true → upgrade confidence by 1 (max 5) and note "cleared previous day low" in reasoning. If cleared_previous_day_low is false (price remains inside yesterday's range) → downgrade confidence by 1 unless vol_ratio_6bar_5min is exceptionally high (> 4.0).

─── RED FLAG DETECTION ──────────────────────────────────────────────────────────
Always check for and report any of the following as red_flags (comma-separated string):
  - "volume declining": last candle volume is lower than both of the previous two candles
  - "weak candle body": candle_strength_last < 0.4 (indecision, potential reversal)
  - "near resistance": distance_to_resistance_pct < 0.5% for LONG (crowded exit above)
  - "near support": distance_to_support_pct < 0.5% for SHORT
  - "bearish market": nifty_trend_today is "bearish" for a LONG signal
  - "bullish market": nifty_trend_today is "bullish" for a SHORT signal
  - "sector underperforming": sector_vs_nifty_today_pct < -0.5% for LONG setups
  - "weak relative strength": stock_vs_nifty_5day_pct < 0 for LONG
  - "strong relative strength against short": stock_vs_nifty_5day_pct > 0 for SHORT
  - "extended daily RSI": rsi_14_daily > 72 for LONG (stock overbought on daily chart)
  - "oversold daily RSI": rsi_14_daily < 28 for SHORT (stock oversold on daily chart)
  - "bank nifty counter-trend": Bank Nifty slope/change is moving against trade direction
  - "inside yesterday range": price has not cleared yesterday's high (for LONG) or low (for SHORT)
If none of the above are present, set red_flags to "none"."""


class ClaudeAnalyzer:
    """Layer 5: Claude API batch analysis — final trade decision gate."""

    def __init__(self):
        self.api_calls_today = 0
        self.enter_decisions_today = 0

    def analyze(self, feature_payloads: list, nifty_trend: str = "sideways",
                nifty_change_pct: float = 0.0, vix_value: float = 0.0) -> list:
        """
        Send all qualifying stocks to Claude in a single batched API call.
        
        Args:
            feature_payloads: List of feature dicts from Layer 4
            nifty_trend: Current Nifty trend (bullish/bearish/sideways)
            nifty_change_pct: Nifty % change today
            vix_value: Current India VIX value
            
        Returns:
            List of decision dicts with ENTER/SKIP decisions.
        """
        if not feature_payloads:
            return []

        active_evaluator = getattr(Config, "ACTIVE_AI_EVALUATOR", "claude")
        if active_evaluator in ("second_ai", "consensus", "local_rule") or not Config.ANTHROPIC_API_KEY:
            try:
                from ai_evaluator import MultiModelEvaluator
                evaluator = MultiModelEvaluator()
                mkt_ctx = {"nifty_trend": nifty_trend, "nifty_change_pct": nifty_change_pct, "vix": vix_value}
                logger.info(f"[Layer 5] Routing analysis via MultiModelEvaluator (mode: {active_evaluator})")
                guarded_decisions = evaluator.evaluate_setups(feature_payloads, mkt_ctx, evaluator_mode=active_evaluator)
                return guarded_decisions
            except Exception as eval_err:
                logger.error(f"[Layer 5] MultiModelEvaluator error: {eval_err}")

        now = datetime.now(IST)
        time_str = now.strftime("%H:%M")
        date_str = now.strftime("%d-%b-%Y")

        import copy
        # Create a blinded payload for Claude (remove targets and multipliers)
        blinded_payloads = copy.deepcopy(feature_payloads)
        for p in blinded_payloads:
            p.pop("calculated_entry", None)
            p.pop("calculated_sl", None)
            p.pop("calculated_target", None)
            p.pop("target_multiplier", None)

        # Build user message
        user_message = (
            f"Analyze these intraday trade setups for {date_str}.\n"
            f"Market context: Nifty is {nifty_trend} today ({nifty_change_pct:+.2f}% from yesterday's close). "
            f"India VIX is {vix_value:.1f}.\n"
            f"Time of scan: {time_str} IST.\n\n"
            f"{json.dumps(blinded_payloads, indent=2)}\n\n"
            f"Return a JSON array with exactly one decision object per stock, in the same order as input.\n"
            f"Note: Target prices and Risk:Reward ratios have been hidden from you. Base your decision PURELY on structural setup quality, volume expansion, and breakout conviction."
        )

        # Make Claude API call
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)
            
            response = client.messages.create(
                model=Config.ANTHROPIC_MODEL,
                max_tokens=Config.ANTHROPIC_MAX_TOKENS,
                temperature=Config.ANTHROPIC_TEMPERATURE,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )
            
            self.api_calls_today += 1
            if hasattr(response, 'content') and response.content:
                raw_text = "".join(block.text for block in response.content if hasattr(block, 'text')).strip()
            else:
                raw_text = str(response).strip()
            logger.info(f"[Layer 5] Claude API call #{self.api_calls_today} completed.")

        except Exception as e:
            error_type = type(e).__name__
            logger.error(f"[Layer 5] Claude API call failed: {error_type}: {e}. Falling back to MultiModelEvaluator.")
            alert_claude_api_failed(time_str, error_type)
            try:
                from ai_evaluator import MultiModelEvaluator
                evaluator = MultiModelEvaluator()
                mkt_ctx = {"nifty_trend": nifty_trend, "nifty_change_pct": nifty_change_pct, "vix": vix_value}
                return evaluator.evaluate_setups(feature_payloads, mkt_ctx, evaluator_mode="local_rule")
            except Exception:
                return []

        # Parse response
        decisions = self._parse_response(raw_text, feature_payloads, time_str)
        
        # Count ENTER decisions
        for d in decisions:
            if d.get("decision") == "ENTER" and d.get("confidence", 0) >= 3:
                self.enter_decisions_today += 1

        return decisions

    def _parse_response(self, raw_text: str, original_payloads: list, time_str: str) -> list:
        """Parse Claude's JSON response with fallback handling."""
        # Attempt 1: Direct parse
        try:
            decisions = json.loads(raw_text)
            if isinstance(decisions, list):
                return self._validate_decisions(decisions, original_payloads)
        except json.JSONDecodeError:
            pass

        # Attempt 2: Strip backtick fences and retry
        cleaned = raw_text
        if "```" in cleaned:
            # Remove ```json ... ``` or ``` ... ```
            lines = cleaned.split('\n')
            filtered = [l for l in lines if not l.strip().startswith('```')]
            cleaned = '\n'.join(filtered)

        try:
            decisions = json.loads(cleaned)
            if isinstance(decisions, list):
                return self._validate_decisions(decisions, original_payloads)
        except json.JSONDecodeError:
            pass

        # Both attempts failed
        logger.error(f"[Layer 5] Failed to parse Claude response as JSON. Raw: {raw_text[:500]}")
        alert_claude_invalid_json(time_str)
        return []

    def _validate_decisions(self, decisions: list, original_payloads: list) -> list:
        """Validate and filter Claude's decisions."""
        validated = []
        input_symbols = {p["symbol"]: p for p in original_payloads}

        for decision in decisions:
            symbol = decision.get("symbol", "")
            
            if symbol not in input_symbols:
                logger.warning(f"[Layer 5] Unknown symbol in response: {symbol}")
                continue

            original = input_symbols.pop(symbol, None)
            if not original:
                continue

            # Ensure all required fields exist
            STRING_FIELDS = {"symbol", "decision", "direction", "reasoning", "red_flags"}
            for field in ["symbol", "decision", "direction", "confidence", "reasoning", "red_flags"]:
                if field not in decision:
                    decision[field] = "N/A" if field in STRING_FIELDS else 0
                    
            decision["discovery_score"] = original.get("discovery_score")
            decision["discovery_rank"] = original.get("discovery_rank")

            # Attach calculated targets from Layer 4 payload
            if decision.get("decision") == "ENTER":
                decision["entry_price"] = original.get("calculated_entry", 0.0)
                decision["stop_loss"] = original.get("calculated_sl", 0.0)
                decision["target"] = original.get("calculated_target", 0.0)
                
                # If Layer 4 didn't provide valid targets (should never happen), fallback
                if decision["entry_price"] == 0.0 or decision["stop_loss"] == 0.0:
                    logger.warning(f"[Layer 5 Validation] Symbol {symbol} missing Layer 4 computed targets. Downgraded to SKIP.")
                    decision["decision"] = "SKIP"
                    decision["red_flags"] = (decision.get("red_flags", "none") + ", missing_computed_targets").replace("none, ", "")

            # Enforce hard numerical checks on ENTER decisions to protect against LLM calculation errors
            if decision.get("decision") == "ENTER":
                try:
                    entry = float(decision.get("entry_price", 0.0))
                    sl = float(decision.get("stop_loss", 0.0))
                    target = float(decision.get("target", 0.0))
                    
                    risk = abs(entry - sl)
                    reward = abs(target - entry)
                    
                    # 1. R:R ratio check (minimum 2.0x target)
                    if risk > 0 and (reward / risk) < 1.95:  # minor buffer for rounding
                        decision["decision"] = "SKIP"
                        decision["red_flags"] = (decision.get("red_flags", "none") + ", failed_rr_validation").replace("none, ", "")
                        logger.warning(f"[Layer 5 Validation] Symbol {symbol} failed R:R validation (computed {reward/risk:.2f}x). Downgraded to SKIP.")
                        
                    # 2. Risk check (max SL check: 1.5x ATR from payload)
                    atr = original.get("price_context", {}).get("atr_14_5min", 0.0) if original else 0.0
                    if atr > 0 and risk > 1.55 * atr:
                        decision["decision"] = "SKIP"
                        decision["red_flags"] = (decision.get("red_flags", "none") + ", risk_exceeded_atr_limit").replace("none, ", "")
                        logger.warning(f"[Layer 5 Validation] Symbol {symbol} failed Max SL validation (risk={risk:.2f} vs 1.5xATR={1.5*atr:.2f}). Downgraded to SKIP.")
                except Exception as eval_err:
                    logger.error(f"[Layer 5 Validation] Error validating entry math for {symbol}: {eval_err}. Downgraded to SKIP.")
                    decision["decision"] = "SKIP"

            validated.append(decision)

        # Check for missing stocks (present in input but absent from response)
        for missing_symbol in input_symbols:
            logger.warning(f"[Layer 5] MISSING_FROM_RESPONSE: {missing_symbol}. Treating as SKIP.")
            validated.append({
                "symbol": missing_symbol,
                "decision": "SKIP",
                "direction": input_symbols[missing_symbol].get("signal_direction", "LONG"),
                "entry_price": 0,
                "stop_loss": 0,
                "target": 0,
                "confidence": 0,
                "reasoning": "Stock missing from Claude response. Treated as SKIP.",
                "red_flags": "missing_from_response"
            })

        return validated

    def get_enter_decisions(self, decisions: list) -> list:
        """Filter to only ENTER decisions with confidence >= 3."""
        return [
            d for d in decisions
            if d.get("decision") == "ENTER" and d.get("confidence", 0) >= 3
        ]

    def reset_daily_stats(self):
        """Reset daily counters (called at start of each trading day)."""
        self.api_calls_today = 0
        self.enter_decisions_today = 0

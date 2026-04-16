from groq import Groq
import logging
from config import Config

logger = logging.getLogger(__name__)

client = Groq(api_key=Config.GROQ_API_KEY)

def analyze_trade(data: dict) -> str:
    """
    Analyzes trading setup using Groq LLM.
    SAFE FALLBACK: Returns AVOID on any failure (capital protection).
    """
    try:
        score = float(data.get('score', 0))
        if score < 4:
            return "AVOID"

        vwap_dist = data.get('vwap_distance', 0)
        vol_ratio = data.get('volume_ratio', 0)
        candle_str = data.get('candle_strength', 0)
        brk_str = data.get('breakout_strength', 0)
        direction = "LONG" if candle_str > 0 else "SHORT"
        vwap_side = "ABOVE" if vwap_dist > 0 else "BELOW"

        prompt = f"""You are a professional Indian equity intraday trader on 1-minute charts.

LIVE SETUP:
- Direction: {direction}
- Price vs VWAP: {vwap_dist*100:.2f}% {vwap_side} VWAP
- Volume vs 20-bar avg: {vol_ratio:.2f}x
- Candle Body Quality: {candle_str:.2f} (range: -1 to +1, higher = cleaner move)
- Breakout Strength: {brk_str*100:.2f}% beyond resistance/support level

DECISION RULES:
Answer STRONG only if ALL are true:
1. Volume >= 1.5x (institutional participation confirmed)
2. Candle strength magnitude >= 0.4 (clean directional candle, not wick-heavy)
3. Price is at least 0.05% beyond VWAP in signal direction
4. Breakout is >= 0.1% beyond resistance/support (not a micro-pierce)

Answer AVOID if ANY is true:
- Volume < 1.3x (retail noise, no institutional flow)
- Candle strength < 0.3 (wick-heavy = rejection candle)
- Price barely touching VWAP or level (< 0.05% clearance)
- Direction contradicts VWAP side (e.g. LONG but price BELOW VWAP)

Answer WEAK if conditions partially met but not all STRONG criteria satisfied.

Respond with exactly ONE word: STRONG, WEAK, or AVOID."""

        response = client.chat.completions.create(
            model="llama3-70b-8192",  # Upgraded from 8B to 70B for better reasoning
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,          # Deterministic — no hallucination
            max_completion_tokens=10,
        )

        decision = response.choices[0].message.content.strip().upper()
        cleaned = ''.join(filter(str.isalpha, decision))

        if "AVOID" in cleaned:
            return "AVOID"
        elif "WEAK" in cleaned:
            return "WEAK"
        else:
            return "STRONG"

    except Exception as e:
        logger.error(f"Groq AI Error: {e}. SAFE FALLBACK → AVOID (capital protection)")
        return "AVOID"  # FIX: Was "STRONG" which disabled the filter on network failures

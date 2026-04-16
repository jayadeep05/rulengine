import pandas as pd
import numpy as np

def is_fake_breakout(latest: pd.Series, direction: str) -> bool:
    """
    Returns True if the breakout shows classic rejection/trap characteristics.
    Three rules: wick rejection, low volume, bearish close inside range.
    """
    high = latest['high']
    low = latest['low']
    close = latest['close']
    volume_ratio = latest.get('volume_ratio', 1.0)

    candle_range = high - low
    if candle_range == 0:
        return True  # Doji = indecision = skip

    candle_mid = low + (candle_range / 2)

    if direction == 'LONG':
        upper_wick = high - close
        # Rule 1: Upper wick > 35% of range = buyers rejected at the high
        if upper_wick / candle_range > 0.35:
            return True
        # Rule 2: Closed in bottom half of candle range = failed to hold breakout
        if close < candle_mid:
            return True

    elif direction == 'SHORT':
        lower_wick = close - low
        # Rule 1: Lower wick > 35% of range = sellers rejected at the low
        if lower_wick / candle_range > 0.35:
            return True
        # Rule 2: Closed in top half = failed to hold breakdown
        if close > candle_mid:
            return True

    # Rule 3: Volume too low = retail noise, not institutional
    if volume_ratio < 1.5:
        return True

    return False


def generate_signals(df: pd.DataFrame) -> dict:
    '''
    Evaluates the most recently COMPLETED candle for signals.
    Returns dict: decision, score, current_price, sl, target, metadata
    '''
    from config import Config

    empty_schema = {
        'decision': 'AVOID', 'score': 0, 'current_price': 0.0,
        'sl': 0.0, 'target': 0.0, 'direction': 'NONE',
        'reason': 'Not enough data',
        'metadata': {
            'breakout': False, 'volume_spike': False,
            'strong_candle': False, 'vwap_aligned': False,
            'market_condition': 'WAITING'
        }
    }

    if df.empty or len(df) < 22:  # Need 20 candles for baseline + 2 buffer
        cp = df.iloc[-1]['close'] if not df.empty else 0.0
        empty_schema['current_price'] = cp
        return empty_schema

    latest = df.iloc[-2]  # Always evaluate last COMPLETED candle

    close_price = latest['close']
    low_price = latest['low']
    high_price = latest['high']
    vwap = latest['vwap']
    vol_ratio = latest['volume_ratio']
    candle_str = latest['candle_strength']
    avg_candle_str = latest.get('avg_candle_str', 0.3)
    brk_out = latest['breakout_level']
    brk_down = latest['breakdown_level']
    atr_pct = latest.get('atr_pct', 0.005)
    dir_flips = latest.get('dir_flips', 0)
    price_slope = latest.get('price_slope', 0)

    # ── MARKET CONDITION GUARD RAILS ─────────────────────────────────────────
    # Skip sideways markets (ATR% too low = no directional movement)
    if atr_pct < 0.002:
        return {**empty_schema, 'current_price': close_price, 'reason': 'Sideways market (ATR < 0.2%)'}

    # Skip choppy markets (too many direction flips)
    if dir_flips >= 4:
        return {**empty_schema, 'current_price': close_price, 'reason': 'Choppy market (4+ flips in 5 candles)'}

    # ── DYNAMIC CANDLE STRENGTH THRESHOLD ────────────────────────────────────
    # A candle must be at least 1.4x stronger than the session's own average
    min_strength = max(Config.MIN_CANDLE_STRENGTH, avg_candle_str * 1.4)

    # ── ABSOLUTE VOLUME FLOOR ─────────────────────────────────────────────────
    MIN_ABSOLUTE_VOLUME = 5000
    if latest['volume'] < MIN_ABSOLUTE_VOLUME:
        return {**empty_schema, 'current_price': close_price, 'reason': 'Volume too low (absolute floor)'}

    # ── STRATEGY CONDITIONS ───────────────────────────────────────────────────
    is_long = (
        (close_price > brk_out) and
        (vol_ratio >= Config.VOLUME_SPIKE_RATIO) and
        (candle_str >= min_strength) and
        (close_price > vwap)
    )
    is_short = (
        (close_price < brk_down) and
        (vol_ratio >= Config.VOLUME_SPIKE_RATIO) and
        (candle_str <= -min_strength) and
        (close_price < vwap)
    )

    # ── CONSECUTIVE CANDLE CONTEXT CHECK ─────────────────────────────────────
    if is_long and len(df) >= 7:
        recent = df.iloc[-7:-2]
        bullish_count = (recent['close'] > recent['open']).sum()
        if bullish_count < 2:
            is_long = False  # No trend context — lone candle in downtrend

    if is_short and len(df) >= 7:
        recent = df.iloc[-7:-2]
        bearish_count = (recent['close'] < recent['open']).sum()
        if bearish_count < 2:
            is_short = False  # No trend context — lone candle in uptrend

    # ── FAKE BREAKOUT DETECTOR ────────────────────────────────────────────────
    if is_long and is_fake_breakout(latest, 'LONG'):
        is_long = False
    if is_short and is_fake_breakout(latest, 'SHORT'):
        is_short = False

    # ── SCORING SYSTEM ────────────────────────────────────────────────────────
    score = 0
    direction = 'NONE'

    if is_long:
        direction = 'LONG'
    elif is_short:
        direction = 'SHORT'
    else:
        if close_price > brk_out:
            direction = 'LONG'
        elif close_price < brk_down:
            direction = 'SHORT'

    if vol_ratio >= 2.0:
        score += 2
    elif vol_ratio >= 1.5:
        score += 1

    if abs(candle_str) >= min_strength:
        score += 2

    if direction == 'LONG':
        if close_price > vwap:
            score += 1
        if close_price > brk_out:
            score += 1
    elif direction == 'SHORT':
        if close_price < vwap:
            score += 1
        if close_price < brk_down:
            score += 1

    decision = 'AVOID'
    sl = 0.0
    target = 0.0

    # ── SL WITH ATR BUFFER (no more pinpoint SL on candle low) ───────────────
    atr_abs = latest.get('atr_5', abs(high_price - low_price))

    if score >= 4:
        if is_long:
            decision = 'BUY'
            sl = low_price - (0.3 * atr_abs)  # Buffer below candle low
            risk = close_price - sl
            if risk <= 0:
                decision = 'AVOID'
            else:
                target = close_price + (risk * 2.0)  # 2R target

        elif is_short:
            decision = 'SELL'
            sl = high_price + (0.3 * atr_abs)  # Buffer above candle high
            risk = sl - close_price
            if risk <= 0:
                decision = 'AVOID'
            else:
                target = close_price - (risk * 2.0)

    # Determine market condition for UI
    if abs(price_slope) > 0.05:
        mkt_condition = 'TRENDING'
    elif dir_flips >= 3:
        mkt_condition = 'VOLATILE'
    else:
        mkt_condition = 'SIDEWAYS'

    risk = abs(close_price - sl) if sl != 0.0 else 0.0
    rr_ratio = round((abs(target - close_price) / risk), 2) if risk > 0 else 0.0

    return {
        'decision': decision,
        'score': score,
        'current_price': close_price,
        'sl': sl,
        'target': target,
        'direction': direction,
        'metadata': {
            'breakout': bool((direction == 'LONG' and close_price > brk_out) or (direction == 'SHORT' and close_price < brk_down)),
            'volume_spike': bool(vol_ratio >= 1.5),
            'strong_candle': bool(abs(candle_str) >= min_strength),
            'vwap_aligned': bool((direction == 'LONG' and close_price > vwap) or (direction == 'SHORT' and close_price < vwap)),
            'market_condition': mkt_condition,
            'breakout_level': round(float(brk_out), 2),
            'breakdown_level': round(float(brk_down), 2),
            'vwap': round(float(vwap), 2),
            'volume_ratio': round(float(vol_ratio), 2),
            'rr_ratio': rr_ratio,
            'atr_pct': round(float(atr_pct) * 100, 3),
        }
    }

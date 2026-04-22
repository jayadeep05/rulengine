import pandas as pd
import numpy as np

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
        'metadata': {}
    }

    if df.empty or len(df) < 22:
        cp = df.iloc[-1]['close'] if not df.empty else 0.0
        empty_schema['current_price'] = cp
        return empty_schema

    latest = df.iloc[-2]
    prev = df.iloc[-3]

    close_price = latest['close']
    low_price = latest['low']
    high_price = latest['high']
    vwap = latest['vwap']
    vol_ratio = latest.get('vol_ratio_6', 0.0)
    rsi_14 = latest.get('rsi_14', 50.0)
    ema_20 = latest.get('ema_20', 0.0)
    prev_ema_20 = prev.get('ema_20', 0.0)
    atr_14 = latest.get('atr_14', abs(high_price - low_price))

    # Time Filter: Only trade after 9:20 AM
    ts = pd.to_datetime(latest.get('timestamp', df['timestamp'].iloc[-2] if 'timestamp' in df.columns else None))
    if pd.notnull(ts):
        if ts.hour < 9 or (ts.hour == 9 and ts.minute < 20):
            return {**empty_schema, 'current_price': close_price, 'reason': 'Before 9:20 AM'}
        if ts.hour == 15 and ts.minute > 15:
            return {**empty_schema, 'current_price': close_price, 'reason': 'After 3:15 PM'}

    # ── VWAP Breakout Momentum (Strategy 1) ───────────────────────────────────
    is_long = (
        (close_price > vwap) and
        (vol_ratio >= 2.0) and
        (rsi_14 >= 60) and
        (ema_20 > prev_ema_20)
    )
    
    is_short = (
        (close_price < vwap) and
        (vol_ratio >= 2.0) and
        (rsi_14 <= 40) and
        (ema_20 < prev_ema_20)
    )

    score = 0
    direction = 'NONE'

    if is_long:
        direction = 'LONG'
        score = 80
    elif is_short:
        direction = 'SHORT'
        score = 80
        
    decision = 'AVOID'
    sl = 0.0
    target = 0.0

    if score >= Config.SCORE_THRESHOLD:
        if is_long:
            decision = 'BUY'
            sl = low_price - (1.5 * atr_14)  # 1.5 ATR Volatility Stop
            risk = close_price - sl
            if risk <= 0:
                decision = 'AVOID'
            else:
                target = close_price + (risk * 2.0)  # 2R target

        elif is_short:
            decision = 'SELL'
            sl = high_price + (1.5 * atr_14)  # 1.5 ATR Volatility Stop
            risk = sl - close_price
            if risk <= 0:
                decision = 'AVOID'
            else:
                target = close_price - (risk * 2.0)

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
            'vwap_aligned': True if decision != 'AVOID' else False,
            'vwap': round(float(vwap), 2) if not np.isnan(vwap) else 0,
            'vol_ratio_6': round(float(vol_ratio), 2) if not np.isnan(vol_ratio) else 0,
            'rsi_14': round(float(rsi_14), 2) if not np.isnan(rsi_14) else 0,
            'ema_20': round(float(ema_20), 2) if not np.isnan(ema_20) else 0,
            'rr_ratio': rr_ratio,
        }
    }

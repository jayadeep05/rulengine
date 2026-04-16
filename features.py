import pandas as pd
import numpy as np

def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    '''
    Input DataFrame expected columns: 'timestamp', 'open', 'high', 'low', 'close', 'volume'
    Should be ordered chronologically oldest to newest.
    '''
    df = df.copy()
    df['timestamp'] = pd.to_datetime(df['timestamp'])

    # ── FIX #1: Anchor VWAP to 9:15 AM session start ──────────────────────────
    # If the server restarts mid-day, VWAP still resets to market open, not restart time.
    session_start = df['timestamp'].iloc[0].normalize() + pd.Timedelta(hours=9, minutes=15)
    df_session = df[df['timestamp'] >= session_start].copy()
    if df_session.empty:
        df_session = df.copy()  # Fallback: use all data if no 9:15 anchor found

    typical_price = (df_session['high'] + df_session['low'] + df_session['close']) / 3
    df_session['cumulative_pv'] = (typical_price * df_session['volume']).cumsum()
    df_session['cumulative_vol'] = df_session['volume'].cumsum()
    df_session['vwap'] = df_session['cumulative_pv'] / df_session['cumulative_vol']

    # Merge computed VWAP back into main df
    df = df.merge(df_session[['timestamp', 'vwap']], on='timestamp', how='left')
    df['vwap'] = df['vwap'].ffill().bfill()

    # ── FIX #2: Volume baseline 5 → 20 candles (removes morning open bias) ─────
    df['volume_ma_20'] = df['volume'].shift(1).rolling(window=20).mean()
    df['volume_ratio'] = df['volume'] / df['volume_ma_20']
    df['volume_ratio'] = df['volume_ratio'].fillna(1.0)

    # ── Candle Strength (unchanged — correct formula) ─────────────────────────
    df['high_low_range'] = df['high'] - df['low']
    df['high_low_range'] = df['high_low_range'].replace(0, np.nan)
    df['candle_strength'] = (df['close'] - df['open']) / df['high_low_range']
    df['candle_strength'] = df['candle_strength'].fillna(0)

    # ── FIX #3: Dynamic candle strength threshold (session-normalized) ─────────
    df['avg_candle_str'] = df['candle_strength'].abs().rolling(10).mean().fillna(0.3)

    # ── FIX #4: Breakout/Breakdown level 10 → 20 candles ─────────────────────
    df['breakout_level'] = df['high'].shift(1).rolling(window=20).max()
    df['breakdown_level'] = df['low'].shift(1).rolling(window=20).min()

    # Derived breakout metrics
    df['breakout_strength'] = (df['close'] - df['breakout_level']) / df['breakout_level']
    df['vwap_distance'] = (df['close'] - df['vwap']) / df['vwap']

    # ── NEW: ATR% for volatility/sideways market detection ────────────────────
    df['tr1'] = df['high'] - df['low']
    df['tr2'] = (df['high'] - df['close'].shift(1)).abs()
    df['tr3'] = (df['low'] - df['close'].shift(1)).abs()
    df['true_range'] = df[['tr1', 'tr2', 'tr3']].max(axis=1)
    df['atr_14'] = df['true_range'].rolling(14).mean().fillna(df['high'] - df['low'])
    df['atr_pct'] = df['atr_14'] / df['close']

    # ── NEW: Price slope for trend detection ──────────────────────────────────
    def rolling_slope(series, window=10):
        slopes = [np.nan] * len(series)
        arr = series.values
        for i in range(window - 1, len(arr)):
            y = arr[i - window + 1:i + 1]
            if not np.any(np.isnan(y)):
                slope = np.polyfit(range(window), y, 1)[0]
                slopes[i] = slope / arr[i] * 100  # Normalize as % per candle
        return pd.Series(slopes, index=series.index)

    df['price_slope'] = rolling_slope(df['close'], window=10)

    # ── NEW: Choppiness detector ──────────────────────────────────────────────
    df['candle_dir'] = np.sign(df['close'] - df['open'])
    df['dir_flips'] = df['candle_dir'].rolling(5).apply(
        lambda x: sum(1 for i in range(1, len(x)) if x.iloc[i] != x.iloc[i-1]) if len(x) > 1 else 0,
        raw=False
    ).fillna(0)

    return df

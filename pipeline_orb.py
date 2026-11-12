"""
pipeline_orb.py — Opening Range Breakout Strategy (Parallel Strategy)
───────────────────────────────────────────────────────────────────────
Runs alongside the continuous momentum scanner as a first-of-day strategy.

Phase 1 — Range Recording (9:15 AM – 9:30 AM):
    Records the highest high and lowest low across all 1-min candles
    from 9:15 AM to 9:30 AM for each morning watchlist stock.

Phase 2 — Breakout Detection (9:30 AM – 11:00 AM ONLY):
    ORB window closes at 11:00 AM. After that, no new ORB signals.
    Checks for candle closes above/below the range with 20-bar volume
    confirmation and Nifty direction agreement.

Phase 3 — Trade Parameters:
    Entry:   or_high + 0.1% for LONG | or_low - 0.1% for SHORT
    SL:      or_low  - 0.2% for LONG | or_high + 0.2% for SHORT
    Target:  Entry + (or_range × 1.5)  |  Entry - (or_range × 1.5)
    R:R < 1.5 → skip

Phase 4 — Integration:
    ORB sends to Claude (same system prompt as momentum)
    Counts toward MAX_TRADES_PER_DAY
    Uses VIX-based position sizing
    Stores strategy_type = "ORB" in DB
"""

import logging
from datetime import time
import pytz

from config import Config
from database import get_ist_now, get_trades_count_today, get_global_trading_enabled

IST = pytz.timezone('Asia/Kolkata')
logger = logging.getLogger(__name__)

# ── ORB timing constants ─────────────────────────────────────────────────────
ORB_OBSERVE_START = time(9, 15)   # Start recording opening range
ORB_OBSERVE_END   = time(9, 30)   # Observation window ends
ORB_SCAN_START    = time(9, 30)   # Breakout detection begins
ORB_SCAN_END      = time(11, 0)   # Breakout window CLOSES at 11:00 AM
ORB_MAX_RANGE_PCT = 1.0           # Skip if range > 1.0% (too volatile)
ORB_MIN_RANGE_PCT = 0.5           # Skip if range < 0.5% (too narrow, low conviction)
ORB_MIN_VOL_RATIO = 2.0           # Min volume vs 20-bar average on breakout candle
ORB_MIN_RR        = 2.0           # Skip if calculated R:R < 2.0
ORB_NIFTY_DROP_LIMIT = -0.5       # Skip LONG if Nifty is down > 0.5%
ORB_NIFTY_RISE_LIMIT =  0.5       # Skip SHORT if Nifty is up > 0.5%


class OpeningRangeBreakout:
    """
    Parallel ORB strategy — observes opening 15-min range then scans for breakout.
    One instance per user, reset daily.
    """

    def __init__(self, user_id: int):
        self.user_id   = user_id
        self._ranges   = {}    # symbol -> {"high": float, "low": float, "obs_count": int}
        self._fired    = set() # symbols that already generated an ORB signal today
        self._obs_done = False # True once 9:30 AM passes
        self.signals_today = 0 # Track ORB signals count
        self._date = None # Date for which the current state is valid
        logger.info(f"[ORB] Initialized Opening Range Breakout strategy for user_id={user_id}")

    def reset(self):
        """Reset daily state (called at midnight / new day detection)."""
        self._ranges.clear()
        self._fired.clear()
        self._obs_done = False
        self.signals_today = 0
        self._date = None
        logger.info("[ORB] Daily state reset.")

    def _check_date_reset(self):
        """Auto-reset if the calendar day has changed."""
        today = get_ist_now().date()
        if self._date and self._date != today:
            self.reset()
        self._date = today

    # ── Phase 1: Observation ─────────────────────────────────────────────────

    def observe(self, active_watchlist: list, prices_cache: dict):
        """
        Called during 9:15 AM – 9:30 AM to record each stock's intraday extremes.
        Must be called at high frequency (every 30s) during this window.

        Args:
            active_watchlist:  Layer 1 watchlist [{symbol, instrument_key, ...}]
            prices_cache:      Live price cache {symbol: price}
        """
        self._check_date_reset()
        now = get_ist_now()

        if not (ORB_OBSERVE_START <= now.time() < ORB_OBSERVE_END):
            return

        for stock in active_watchlist:
            symbol = stock["symbol"]
            ltp = prices_cache.get(symbol, 0.0)
            if ltp <= 0:
                continue

            if symbol not in self._ranges:
                self._ranges[symbol] = {"high": ltp, "low": ltp, "obs_count": 1}
            else:
                entry = self._ranges[symbol]
                if ltp > entry["high"]:
                    entry["high"] = ltp
                if ltp < entry["low"]:
                    entry["low"] = ltp
                entry["obs_count"] += 1

    def recover_ranges_from_candles(self, active_watchlist: list, engine):
        """
        Recover/backfill ORB high and low ranges from 1-minute historical candles
        if starting the bot mid-day.
        """
        from datetime import datetime
        self._check_date_reset()
        today = get_ist_now().date()
        logger.info(f"[ORB] Starting mid-day range recovery for {len(active_watchlist)} watchlist stocks...")
        
        recovered_count = 0
        for stock in active_watchlist:
            symbol = stock["symbol"]
            instrument_key = stock.get("instrument_key", "")
            if not instrument_key:
                continue
                
            try:
                # Fetch today's 1-minute historical candles
                df = engine.get_ohlc(instrument_key, interval="1minute")
                if df.empty:
                    continue
                
                # Filter candles between 9:15 AM and 9:30 AM IST today
                df['ist_time'] = df['timestamp'].dt.tz_convert('Asia/Kolkata') if df['timestamp'].dt.tz else df['timestamp'].dt.tz_localize('Asia/Kolkata')
                
                orb_candles = df[
                    (df['ist_time'].dt.date == today) &
                    (df['ist_time'].dt.time >= time(9, 15)) &
                    (df['ist_time'].dt.time < time(9, 30))
                ]
                
                if not orb_candles.empty:
                    highest = orb_candles['high'].max()
                    lowest = orb_candles['low'].min()
                    or_low = float(lowest)
                    or_high = float(highest)
                    or_range_pct = ((or_high - or_low) / or_low * 100) if or_low > 0 else 0.0
                    
                    if or_range_pct > ORB_MAX_RANGE_PCT:
                        logger.info(f"[ORB] Recovered range for {symbol} ({or_range_pct:.1f}%) is too wide (> {ORB_MAX_RANGE_PCT}%). Skipping.")
                        continue
                    elif or_range_pct < ORB_MIN_RANGE_PCT:
                        logger.info(f"[ORB] Recovered range for {symbol} ({or_range_pct:.1f}%) is too narrow (< {ORB_MIN_RANGE_PCT}%). Skipping.")
                        continue
                        
                    self._ranges[symbol] = {
                        "high": or_high,
                        "low": or_low,
                        "obs_count": len(orb_candles)
                    }
                    recovered_count += 1
                    logger.info(f"[ORB] Recovered valid range for {symbol}: H={highest:.2f}/L={lowest:.2f} ({or_range_pct:.1f}%) using {len(orb_candles)} candles")
            except Exception as re:
                logger.error(f"[ORB] Failed to recover range for {symbol}: {re}")
                
        self._obs_done = True
        logger.info(f"[ORB] Recovery complete. Recovered ranges for {recovered_count}/{len(active_watchlist)} stocks.")

    def mark_observation_complete(self):
        """
        Call exactly once when 9:30 AM is reached.
        Validates range quality and logs results.
        """
        if self._obs_done:
            return

        valid_ranges = {}
        for symbol, r in self._ranges.items():
            if r["obs_count"] < 2:
                logger.debug(f"[ORB] {symbol}: too few observations ({r['obs_count']}) — skipped.")
                continue

            or_low = r["low"]
            or_high = r["high"]
            or_range_pct = ((or_high - or_low) / or_low * 100) if or_low > 0 else 0

            status = "VALID"
            if or_range_pct > ORB_MAX_RANGE_PCT:
                status = "SKIPPED_TOO_WIDE"
                logger.info(
                    f"[ORB] {symbol}: range {or_range_pct:.1f}% > {ORB_MAX_RANGE_PCT}% max — "
                    f"too wide, skipping this stock."
                )
            elif or_range_pct < ORB_MIN_RANGE_PCT:
                status = "SKIPPED_TOO_NARROW"
                logger.info(
                    f"[ORB] {symbol}: range {or_range_pct:.1f}% < {ORB_MIN_RANGE_PCT}% min — "
                    f"too narrow, skipping this stock."
                )
            else:
                valid_ranges[symbol] = r
                logger.debug(
                    f"[ORB] {symbol}: range recorded H={or_high:.2f}/L={or_low:.2f} "
                    f"({or_range_pct:.1f}%)"
                )

            # Send Telegram Alert
            try:
                from telegram_alert import alert_orb_range_set
                alert_orb_range_set(symbol, or_high, or_low, or_range_pct, status)
            except Exception as ta_err:
                logger.error(f"Failed to send ORB range alert for {symbol}: {ta_err}")

        self._ranges = valid_ranges
        self._obs_done = True
        logger.info(
            f"[ORB] Observation complete. {len(valid_ranges)} stocks with valid ranges: "
            + (", ".join(
                f"{s} H={r['high']:.2f}/L={r['low']:.2f}"
                for s, r in valid_ranges.items()
            ) or "none")
        )

    # ── Phase 2: Breakout Scanning ───────────────────────────────────────────

    def scan(self, active_watchlist: list, prices_cache: dict,
             active_trade_symbols: list, engine,
             nifty_change_pct: float = 0.0) -> list:
        """
        Called every 1 minute from 9:30 AM – 11:00 AM.
        Returns ORB signal dicts for confirmed breakouts.

        Args:
            active_watchlist:      Layer 1 watchlist
            prices_cache:          Live price cache
            active_trade_symbols:  Currently open trade symbols
            engine:                Execution engine for candle data
            nifty_change_pct:      Today's Nifty 50 % change from previous close
        """
        self._check_date_reset()
        now = get_ist_now()

        # ORB window: 9:30 AM to 11:00 AM only
        if not (ORB_SCAN_START <= now.time() < ORB_SCAN_END):
            return []

        if not self._obs_done:
            return []

        if not get_global_trading_enabled():
            return []

        max_trades = int(self.config.get('MAX_TRADES_PER_DAY', Config.MAX_TRADES_PER_DAY) or 5)
        if get_trades_count_today(self.user_id) >= max_trades:
            return []

        active_set = set(active_trade_symbols)
        symbol_to_stock = {s["symbol"]: s for s in active_watchlist}
        signals = []

        for symbol, orb in self._ranges.items():
            # Already fired today
            if symbol in self._fired:
                continue
            # Already in an active trade
            if symbol in active_set:
                continue
            # Not in watchlist anymore (pruned by Layer 2)
            if symbol not in symbol_to_stock:
                continue

            stock = symbol_to_stock[symbol]
            instrument_key = stock.get("instrument_key", "")

            or_high = orb["high"]
            or_low  = orb["low"]
            or_range = or_high - or_low
            or_range_pct = ((or_high - or_low) / or_low * 100) if or_low > 0 else 0.0

            ltp = prices_cache.get(symbol, 0.0)
            if ltp <= 0:
                continue

            # Proximity pre-filter to avoid hitting Upstox API rate limits:
            # Skip if LTP is deep inside the opening range. A breakout can only be triggered
            # when the price is near or outside the range boundaries.
            is_near_high = ltp > or_high * 0.9985  # within 0.15% of high or above
            is_near_low  = ltp < or_low * 1.0015   # within 0.15% of low or below
            if not (is_near_high or is_near_low):
                continue

            # Fetch 1-minute historical candles to verify completed candle close breakout
            # (use completed 1-min candle close to avoid wick rejections - Fix 6)
            try:
                df_1min = engine.get_ohlc(instrument_key, interval="1minute")
            except Exception as e:
                logger.warning(f"[ORB] Failed to fetch 1-min candles for {symbol}: {e}")
                continue

            if df_1min is None or df_1min.empty or len(df_1min) < 22:
                continue

            # Index -2 is the last fully completed closed 1-minute candle
            last_1min_close = float(df_1min.iloc[-2]['close'])
                
            breakout_long  = last_1min_close > or_high * 1.001   # close 0.1% above ORB high
            breakout_short = last_1min_close < or_low  * 0.999   # close 0.1% below ORB low
 
            if not (breakout_long or breakout_short):
                continue
 
            direction = "LONG" if breakout_long else "SHORT"

            # Nifty direction filter
            if direction == "LONG" and nifty_change_pct < ORB_NIFTY_DROP_LIMIT:
                logger.info(
                    f"[ORB] {symbol} LONG breakout rejected — "
                    f"Nifty falling {nifty_change_pct:.2f}% (limit {ORB_NIFTY_DROP_LIMIT}%)."
                )
                continue

            if direction == "SHORT" and nifty_change_pct > ORB_NIFTY_RISE_LIMIT:
                logger.info(
                    f"[ORB] {symbol} SHORT breakout rejected — "
                    f"Nifty rising {nifty_change_pct:.2f}% (limit +{ORB_NIFTY_RISE_LIMIT}%)."
                )
                continue

            # Volume confirmation using 20-bar average on 1-minute chart
            # Index -2 is the completed breakout candle on the 1-minute chart
            vol_current = float(df_1min.iloc[-2]['volume'])
            # 20-bar average of completed 1-minute candles prior to the breakout
            vol_avg = df_1min['volume'].iloc[-22:-2].mean()
            vol_ratio = vol_current / vol_avg if vol_avg > 0 else 0.0

            if vol_ratio < ORB_MIN_VOL_RATIO:
                logger.info(
                    f"[ORB] {symbol} {direction}: volume {vol_ratio:.1f}x < "
                    f"{ORB_MIN_VOL_RATIO}x required. Skipping."
                )
                continue

            # Calculate entry, SL, target
            if direction == "LONG":
                entry_price = round(or_high * 1.001, 2)   # or_high + 0.1%
                stop_loss   = round(or_low  * 0.998, 2)   # or_low  - 0.2%
                risk        = abs(entry_price - stop_loss)
                target      = round(entry_price + (risk * 2.0), 2)
            else:
                entry_price = round(or_low  * 0.999, 2)   # or_low  - 0.1%
                stop_loss   = round(or_high * 1.002, 2)   # or_high + 0.2%
                risk        = abs(entry_price - stop_loss)
                target      = round(entry_price - (risk * 2.0), 2)

            reward = abs(target - entry_price)
            rr_ratio = reward / risk if risk > 0 else 0.0

            if rr_ratio < ORB_MIN_RR:
                logger.info(
                    f"[ORB] {symbol} {direction}: R:R {rr_ratio:.1f}x < {ORB_MIN_RR}x minimum. Skipping."
                )
                continue

            # Fetch actual ATR-14 from 5-min candles (Fix 1)
            atr_14 = 0.0
            try:
                from features import compute_features
                df_5min = engine.get_ohlc(instrument_key, interval="5minute")
                if not df_5min.empty and len(df_5min) >= 20:
                    df_5min = compute_features(df_5min)
                    if not df_5min.empty:
                        atr_14 = float(df_5min.iloc[-2].get('atr_14', 0.0))
            except Exception as e:
                logger.warning(f"[ORB] Failed to compute ATR for {symbol}: {e}")
                
            if atr_14 <= 0.0:
                atr_14 = entry_price * 0.005  # fallback to 0.5% default

            self._fired.add(symbol)

            sig = {
                "symbol":          symbol,
                "instrument_key":  instrument_key,
                "direction":       direction,
                "strategy":        "ORB",
                "conditions_met":  5,        # ORB treated as 5/5 quality
                "ltp":             ltp,
                "entry_price":     entry_price,
                "stop_loss":       stop_loss,
                "target":          target,
                "rr_ratio":        round(rr_ratio, 2),
                "orb_high":        round(or_high, 2),
                "orb_low":         round(or_low,  2),
                "or_range_pct":    round(or_range_pct, 2),
                "vol_ratio_6bar":  round(vol_ratio, 2),  # key kept for consistency
                "vwap":            ltp,      # approximation — no VWAP in ORB
                "rsi_14":          0.0,
                "ema_20":          0.0,
                "candle_strength": 1.0,
                "price_vs_vwap":   0.0,
                "atr_14":          round(atr_14, 2),
            }
            signals.append(sig)
            self.signals_today += 1
            logger.info(
                f"[ORB] 🔔 BREAKOUT: {symbol} {direction} @ ₹{ltp:.2f} | "
                f"ORB H={or_high:.2f}/L={or_low:.2f} ({or_range_pct:.1f}%) | "
                f"Vol={vol_ratio:.1f}x | SL=₹{stop_loss:.2f} | Tgt=₹{target:.2f} | R:R={rr_ratio:.1f}x"
            )

        return signals

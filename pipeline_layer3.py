"""
pipeline_layer3.py — Continuous Signal Scanner (Section 3, Layer 3)
────────────────────────────────────────────────────────────────────
Runs every 5 minutes, 9:20 AM to 2:30 PM.
Pure Python calculations — no external API calls (15-min MTF uses cached data).

For each stock in the active watchlist, checks if minimum signal conditions are met.

IMPROVEMENTS (2026-07-05):
  - Time-of-day dynamic threshold: 12:00 PM – 1:30 PM requires 4/5, otherwise 3/5
  - Multi-timeframe alignment: 15-minute EMA + VWAP must agree with direction
  - Sector momentum filter: skips if sector is clearly moving against the trade direction
"""

import logging
import time as _time
import numpy as np
import pandas as pd
from datetime import datetime, time
import pytz

from config import Config
from database import (
    get_ist_now, get_ist_date, get_global_trading_enabled,
    is_symbol_blacklisted, get_trades_count_today
)
import market_feed

IST = pytz.timezone('Asia/Kolkata')
logger = logging.getLogger(__name__)


class SignalScanner:
    """Layer 3: Fast quantitative signal screening on the active watchlist."""

    def __init__(self, user_id: int):
        self.user_id = user_id
        self.scan_count = 0  # Track total scans for pipeline stats

        # 15-minute multi-timeframe cache: {instrument_key: {"aligned": bool, "last_refresh": float}}
        self._mtf_cache: dict = {}
        # Sector performance cache (refreshed externally by Layer 4 or trading cycle)
        self._sector_perf: dict = {}  # {sector_name: change_pct}

    def update_sector_performance(self, sector_perf: dict):
        """Called by the trading cycle after Layer 4 refreshes sector data."""
        self._sector_perf = sector_perf or {}

    def scan(self, active_watchlist: list, active_trade_symbols: list,
             prices_cache: dict, vix_value: float = 0.0,
             sector_perf: dict = None, day_type: str = "NORMAL") -> list:
        """
        Scan active watchlist for signals.

        Args:
            active_watchlist:      List of dicts with {symbol, instrument_key, direction_bias}
            active_trade_symbols:  Symbols currently in active trades for this user
            prices_cache:          Live price cache {symbol: price}
            vix_value:             Current India VIX value
            sector_perf:           Sector performance dict {sector: change_pct_today}

        Returns:
            List of qualifying signal dicts, max 5, sorted by strength.
        """
        self.scan_count += 1
        now = get_ist_now()

        if sector_perf is not None:
            self._sector_perf = sector_perf

        # ── Pre-scan skip checks ────────────────────────────────────────────
        if not get_global_trading_enabled():
            logger.info("[Layer 3] Global trading halted — skipping scan.")
            return []

        max_trades = int(self.config.get('MAX_TRADES_PER_DAY', Config.MAX_TRADES_PER_DAY) or 5)
        trades_today = get_trades_count_today(self.user_id)
        if trades_today >= max_trades:
            logger.info(f"[Layer 3] Daily trade limit reached ({trades_today}/{max_trades}) — skipping scan.")
            return []

        cutoff_hour, cutoff_min = Config.NO_NEW_ENTRIES_AFTER
        if now.time() >= time(cutoff_hour, cutoff_min):
            logger.debug("[Layer 3] Past entry cutoff (2:30 PM) — skipping scan.")
            return []

        if vix_value > Config.MAX_VIX_FOR_TRADING:
            logger.info(f"[Layer 3] VIX too high ({vix_value:.1f} > {Config.MAX_VIX_FOR_TRADING}) — skipping scan.")
            return []

        # ── IMPROVEMENT 1: Time-of-day dynamic threshold ────────────────────
        # Lunch hour (12:00 PM – 1:30 PM): choppy, more false breakouts → require 4/5
        lunch_hour = time(12, 0) <= now.time() < time(13, 30)
        required_conditions = 4 if lunch_hour or day_type == "LOW_PARTICIPATION" else 3
        if lunch_hour:
            logger.debug("[Layer 3] Lunch hour — requiring 4/5 conditions (stricter filter).")
        elif day_type == "LOW_PARTICIPATION":
            logger.debug("[Layer 3] LOW_PARTICIPATION day — requiring 4/5 conditions (stricter filter).")

        # ── Per-stock scanning ──────────────────────────────────────────────
        qualifying = []
        active_set = set(active_trade_symbols)
        blacklist = set()

        for stock in active_watchlist:
            symbol = stock["symbol"]
            instrument_key = stock.get("instrument_key", "")
            direction_bias = stock.get("direction_bias", "LONG_CANDIDATE")

            # Skip if already in active trade (This explicitly blocks Momentum 
            # signals on stocks that have an active ORB trade during the 10:15-11:00 overlap)
            if symbol in active_set:
                logger.debug(f"[Layer 3] Skipping {symbol} — already has an active trade.")
                continue

            # Skip if blacklisted (already traded today)
            if symbol not in blacklist:
                if is_symbol_blacklisted(self.user_id, symbol):
                    blacklist.add(symbol)
                    continue
            else:
                continue

            # Skip if tick data is stale
            if not market_feed.is_tick_fresh(symbol, max_age=30.0):
                logger.debug(f"[Layer 3] Skipping {symbol} — stale tick data.")
                continue

            # Get live price
            ltp = prices_cache.get(symbol, 0.0)
            if ltp <= 0:
                continue

            # Evaluate 5-minute signal conditions
            result = self._evaluate_signal(
                symbol, instrument_key, direction_bias, ltp,
                prices_cache, required_conditions,
                discovery_score=stock.get("discovery_score"),
                discovery_rank=stock.get("discovery_rank")
            )
            if result:
                qualifying.append(result)

        # ── IMPROVEMENT 6: Sector-weighted ranking ────────────────────────────
        # Boost signals from outperforming sectors so they rank higher when
        # conditions_met is tied. Boost is capped at ±2 to avoid overriding
        # strong signals from weaker sectors.
        from nifty500_instruments import get_sector
        for sig in qualifying:
            sym_sector = get_sector(sig["symbol"])
            sector_chg = self._sector_perf.get(sym_sector, 0.0)
            # Normalize: 1% outperformance → +1 boost point (capped ±2)
            sig["sector_boost"] = round(max(-2.0, min(2.0, sector_chg)), 2)

        # Composite rank: conditions (primary) > sector_boost (secondary) > vol_ratio (tertiary)
        qualifying.sort(
            key=lambda x: (
                x["conditions_met"],
                x.get("sector_boost", 0.0),
                x.get("vol_ratio_6bar", 0),
            ),
            reverse=True,
        )
        top_signals = qualifying[:Config.LAYER4_MAX_STOCKS_PER_CYCLE]

        if top_signals:
            logger.info(
                f"[Layer 3] {len(top_signals)} signals passed "
                f"(threshold={required_conditions}/5): "
                f"{[(s['symbol'], s['direction'], s['conditions_met']) for s in top_signals]}"
            )

        return top_signals

    def _evaluate_signal(self, symbol: str, instrument_key: str,
                         direction_bias: str, ltp: float,
                         prices_cache: dict, required_conditions: int,
                         discovery_score: float = None,
                         discovery_rank: int = None) -> dict:
        """
        Calculate features and check conditions threshold for a single stock.
        Returns signal dict if threshold met, None otherwise.
        """
        try:
            from features import compute_features
            from execution import UpstoxExecutionEngine
            from user_bot_registry import get_bot

            # Get the correct user bot execution engine if available
            bot = get_bot(self.user_id)
            if bot and bot.engine:
                engine = bot.engine
            else:
                engine = UpstoxExecutionEngine()

            df = engine.get_ohlc(instrument_key, interval="5minute")
            if df.empty or len(df) < 20:
                return None

            df = compute_features(df)
            if df.empty:
                return None

            latest = df.iloc[-2]  # Most recently COMPLETED candle
            
            # Guard against candle completeness (Fix 8)
            try:
                from datetime import timedelta
                latest_ts = latest['timestamp']
                if hasattr(latest_ts, 'tzinfo'):
                    if latest_ts.tzinfo is None:
                        latest_ts = IST.localize(latest_ts)
                    else:
                        latest_ts = latest_ts.astimezone(IST)
                else:
                    latest_ts = pd.to_datetime(latest_ts)
                    if latest_ts.tzinfo is None:
                        latest_ts = IST.localize(latest_ts)
                    else:
                        latest_ts = latest_ts.astimezone(IST)
                    
                now_ist = datetime.now(IST)
                # If latest candle close time (start + 5m) is in the future, it is not fully completed yet!
                if latest_ts + timedelta(minutes=5) > now_ist:
                    logger.warning(
                        f"[Layer 3] completeness guard triggered for {symbol}: "
                        f"Candle {latest_ts.strftime('%H:%M')} + 5m is in future relative to {now_ist.strftime('%H:%M:%S')}"
                    )
                    return None
            except Exception as tg_err:
                logger.error(f"[Layer 3] Error in candle completeness guard for {symbol}: {tg_err}")

            prev = df.iloc[-3] if len(df) >= 3 else latest

            # ── Extract features ────────────────────────────────────────────
            close = float(latest['close'])
            vwap = float(latest.get('vwap', close))
            rsi_14 = float(latest.get('rsi_14', 50.0))
            ema_9 = float(latest.get('ema_9', close))
            ema_21 = float(latest.get('ema_21', close))
            ema_20 = float(latest.get('ema_20', close))

            # Volume ratio (6-bar)
            vol_ma_6 = df['volume'].iloc[-7:-1].mean() if len(df) >= 7 else df['volume'].mean()
            vol_ratio_6bar = float(latest['volume']) / vol_ma_6 if vol_ma_6 > 0 else 0

            # Candle strength: abs(close - open) / (high - low)
            high_low = float(latest['high']) - float(latest['low'])
            if high_low == 0:
                candle_strength = 0.0
            else:
                candle_strength = abs(float(latest['close']) - float(latest['open'])) / high_low

            # Price vs VWAP percentage
            price_vs_vwap = ((close - vwap) / vwap * 100) if vwap > 0 else 0

            # ── Determine direction ─────────────────────────────────────────
            is_long = "LONG" in direction_bias.upper()
            is_short = "SHORT" in direction_bias.upper()
            direction = "LONG" if is_long else "SHORT"

            # ── Climax volume check ─────────────────────────────────────────
            vol_ma_20 = df['volume'].iloc[-21:-1].mean() if len(df) >= 21 else vol_ma_6
            if vol_ma_20 > 0:
                vol_ratio_20bar = float(latest['volume']) / vol_ma_20
                if vol_ratio_20bar >= 5.0 and direction == "LONG":
                    logger.info(f"[Layer 3] CLIMAX_VOLUME_SKIP: {symbol} volume spike {vol_ratio_20bar:.1f}x > 5.0x. Skipping LONG.")
                    return None

            # ── Check 5 conditions ──────────────────────────────────────────
            conditions_met = 0

            if is_long:
                if price_vs_vwap > 0.15:
                    conditions_met += 1
                if vol_ratio_6bar >= 2.0:
                    conditions_met += 1
                if 58 <= rsi_14 <= 75:
                    conditions_met += 1
                if ema_9 > ema_21:
                    conditions_met += 1
                if candle_strength >= 0.50:
                    conditions_met += 1
            else:  # SHORT
                if price_vs_vwap < -0.15:
                    conditions_met += 1
                if vol_ratio_6bar >= 2.0:
                    conditions_met += 1
                if 25 <= rsi_14 <= 42:
                    conditions_met += 1
                if ema_9 < ema_21:
                    conditions_met += 1
                if candle_strength >= 0.50:
                    conditions_met += 1

            # Must meet the dynamic required threshold
            if conditions_met < required_conditions:
                return None

            # ── IMPROVEMENT 3: Multi-timeframe 15-minute alignment check ────
            if not self._check_15min_alignment(instrument_key, direction, engine):
                logger.debug(
                    f"[Layer 3] {symbol} {direction} passed 5-min ({conditions_met}/5) "
                    f"but FAILED 15-min alignment check — skipped."
                )
                return None

            # ── IMPROVEMENT 6: Sector momentum filter ───────────────────────
            from nifty500_instruments import get_sector
            sector = get_sector(symbol)
            if sector and sector in self._sector_perf:
                sector_chg = self._sector_perf[sector]
                if direction == "LONG" and sector_chg < -0.3:
                    logger.debug(
                        f"[Layer 3] {symbol} LONG skipped — sector '{sector}' "
                        f"is down {sector_chg:.2f}% (against trade)."
                    )
                    return None
                elif direction == "SHORT" and sector_chg > 0.3:
                    logger.debug(
                        f"[Layer 3] {symbol} SHORT skipped — sector '{sector}' "
                        f"is up {sector_chg:.2f}% (against trade)."
                    )
                    return None

            return {
                "symbol": symbol,
                "instrument_key": instrument_key,
                "direction": direction,
                "conditions_met": conditions_met,
                "ltp": ltp,
                "vwap": round(vwap, 2),
                "rsi_14": round(rsi_14, 2),
                "ema_20": round(ema_20, 2),
                "vol_ratio_6bar": round(vol_ratio_6bar, 2),
                "candle_strength": round(candle_strength, 2),
                "price_vs_vwap": round(price_vs_vwap, 4),
                "discovery_score": discovery_score,
                "discovery_rank": discovery_rank,
            }

        except Exception as e:
            logger.error(f"[Layer 3] Error evaluating {symbol}: {e}")
            return None

    def _check_15min_alignment(self, instrument_key: str, direction: str,
                               engine) -> bool:
        """
        IMPROVEMENT 3: Multi-timeframe 15-minute alignment check.

        For LONG:  15-min EMA 20 must be rising AND price must be above 15-min VWAP.
        For SHORT: 15-min EMA 20 must be falling AND price must be below 15-min VWAP.

        Uses a per-symbol 15-minute cache refreshed every 15 minutes to avoid API bursts.
        Returns True if aligned (signal should proceed), False if misaligned (skip).
        """
        now_ts = _time.time()
        cache_entry = self._mtf_cache.get(instrument_key)
        # Refresh every 15 minutes
        if cache_entry and (now_ts - cache_entry["last_refresh"]) < (15 * 60):
            cached_direction = cache_entry.get("direction")
            if cached_direction == direction:
                return cache_entry["aligned"]
            # Direction changed since cache — re-evaluate
        
        try:
            from features import compute_features
            df_15 = engine.get_ohlc(instrument_key, interval="15minute")
            if df_15 is None or df_15.empty or len(df_15) < 5:
                # If no 15-min data available, allow the signal (don't block on data absence)
                logger.debug(f"[MTF] No 15-min data for {instrument_key} — allowing signal.")
                return True

            df_15 = compute_features(df_15)
            if df_15.empty:
                return True

            latest_15 = df_15.iloc[-2]   # Latest completed 15-min candle
            prev_15 = df_15.iloc[-3] if len(df_15) >= 3 else latest_15

            ema20_now  = float(latest_15.get('ema_20', 0))
            ema20_prev = float(prev_15.get('ema_20', ema20_now))
            vwap_15    = float(latest_15.get('vwap', 0))
            close_15   = float(latest_15.get('close', 0))

            if direction == "LONG":
                ema_rising      = ema20_now > ema20_prev
                above_vwap_15   = (close_15 > vwap_15) if vwap_15 > 0 else True
                aligned = ema_rising and above_vwap_15
            else:  # SHORT
                ema_falling     = ema20_now < ema20_prev
                below_vwap_15   = (close_15 < vwap_15) if vwap_15 > 0 else True
                aligned = ema_falling and below_vwap_15

            self._mtf_cache[instrument_key] = {
                "aligned": aligned,
                "direction": direction,
                "last_refresh": now_ts,
            }
            return aligned

        except Exception as e:
            logger.warning(f"[MTF] Error checking 15-min alignment for {instrument_key}: {e}. Allowing signal.")
            return True  # Fail-open: don't block on data error

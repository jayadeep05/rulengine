"""
pipeline_layer4.py — Feature Extraction (Section 3, Layer 4)
─────────────────────────────────────────────────────────────
Runs immediately after Layer 3 for qualifying stocks (max 5).
Extracts a rich set of pre-computed features to send to Claude.

Uses a rolling candle cache refreshed every 10 minutes to prevent API bursts.

IMPROVEMENTS (2026-07-05):
  - R:R pre-filter: any setup with R:R < 2.0 is dropped before reaching Claude
  - Sector momentum: computes live sector performance from existing index cache
"""

import logging
import time as _time
import numpy as np
import pandas as pd
from datetime import datetime
import pytz

from config import Config
from nifty500_instruments import get_sector
from features import compute_features

IST = pytz.timezone('Asia/Kolkata')
logger = logging.getLogger(__name__)


class FeatureExtractor:
    """Layer 4: Extract rich feature payload for Claude API analysis."""

    def __init__(self, execution_engine):
        self.engine = execution_engine
        
        # Rolling candle cache: {instrument_key: {"df": DataFrame, "last_refresh": timestamp}}
        self._candle_cache = {}
        self._daily_cache = {}
        
        # Market context cache
        self._nifty_trend = {"trend": "sideways", "change_pct": 0.0, "last_refresh": 0}
        self._bank_nifty_trend = {"slope": "flat", "change_3bar_pct": 0.0, "last_refresh": 0}
        self._vix_value = {"value": 0.0, "last_refresh": 0}
        self._sector_perf = {}  # {sector: pct_vs_nifty, "last_refresh": 0}
        self._sector_refresh_time = 0

    def extract(self, signals: list, prices_cache: dict, indices_cache: dict, market_regime: str = "UNKNOWN", prev_close_cache: dict = None) -> list:
        """
        Extract features for each qualifying stock from Layer 3.
        
        Args:
            signals: List of signal dicts from Layer 3
            prices_cache: Live price cache
            indices_cache: Live index cache
            prev_close_cache: Cache containing previous day closes for indices
            
        Returns:
            JSON-ready list of feature payloads for Claude.
        """
        if not signals:
            return []

        # Refresh market context caches
        self._refresh_market_context(indices_cache, prev_close_cache)

        payloads = []
        for signal in signals:
            try:
                payload = self._extract_single(signal, prices_cache, market_regime)
                if payload:
                    payloads.append(payload)
            except Exception as e:
                logger.error(f"[Layer 4] Error extracting features for {signal['symbol']}: {e}")
                continue

        logger.info(f"[Layer 4] Extracted features for {len(payloads)} stocks.")
        return payloads

    def _extract_single(self, signal: dict, prices_cache: dict, market_regime: str = "UNKNOWN") -> dict:
        """Extract full feature payload for a single stock."""
        symbol = signal["symbol"]
        instrument_key = signal["instrument_key"]
        direction = signal["direction"]
        ltp = prices_cache.get(symbol, signal.get("ltp", 0))

        if ltp <= 0:
            return None

        # Get 5-min candle data (from cache or fresh)
        df = self._get_cached_candles(instrument_key)
        if df is None or df.empty or len(df) < 6:
            return None

        df = compute_features(df)
        latest = df.iloc[-2]  # Most recently completed candle

        # ── Price Context ──────────────────────────────────────────────────
        vwap = float(latest.get('vwap', ltp))
        ema_20 = float(latest.get('ema_20', ltp))
        atr_14 = float(latest.get('atr_14', 0))
        atr_pct = (atr_14 / ltp * 100) if ltp > 0 else 0

        # Get daily EMA20 for price_vs_ema20_daily
        daily_df = self._get_cached_daily_candles(instrument_key, days=45)
        price_vs_ema20_daily = 0.0
        rsi_14_daily = 50.0
        if not daily_df.empty and len(daily_df) >= 20:
            daily_ema20 = daily_df['close'].ewm(span=20, adjust=False).mean().iloc[-1]
            price_vs_ema20_daily = ((ltp - daily_ema20) / daily_ema20) * 100
            # Daily RSI
            delta = daily_df['close'].diff()
            gain = delta.where(delta > 0, 0).ewm(alpha=1/14, adjust=False).mean()
            loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
            rs = gain / loss
            rsi_daily = 100 - (100 / (1 + rs))
            rsi_14_daily = float(rsi_daily.iloc[-1])

        # 20-day high from daily data
        price_vs_20day_high = 0.0
        if not daily_df.empty and len(daily_df) >= 20:
            high_20d = daily_df['high'].iloc[-20:].max()
            price_vs_20day_high = ((ltp - high_20d) / high_20d) * 100

        # Yesterday's High/Low Context (Gap 2)
        prev_day_high = 0.0
        prev_day_low = 0.0
        if not daily_df.empty:
            last_date = daily_df.iloc[-1]['timestamp'].date()
            today_date = datetime.now(IST).date()
            if last_date == today_date and len(daily_df) >= 2:
                yesterday_row = daily_df.iloc[-2]
            else:
                yesterday_row = daily_df.iloc[-1]
            prev_day_high = float(yesterday_row['high'])
            prev_day_low = float(yesterday_row['low'])

        # ── Momentum ──────────────────────────────────────────────────────
        rsi_14_5min = float(latest.get('rsi_14', 50.0))
        
        # EMA slope
        prev = df.iloc[-3] if len(df) >= 3 else latest
        ema_now = float(latest.get('ema_20', 0))
        ema_prev = float(prev.get('ema_20', 0))
        if ema_now > ema_prev * 1.001:
            ema_slope = "rising"
        elif ema_now < ema_prev * 0.999:
            ema_slope = "falling"
        else:
            ema_slope = "flat"

        candle_strength = float(latest.get('candle_strength', 0))
        price_slope = float(latest.get('price_slope', 0)) if 'price_slope' in latest.index else 0

        # ── Volume ────────────────────────────────────────────────────────
        vol_ratio_6bar = signal.get("vol_ratio_6bar", 0)
        
        # Daily volume ratio
        vol_ratio_daily = 0.0
        if not daily_df.empty and len(daily_df) >= 21:
            avg_vol_20d = daily_df['volume'].iloc[-21:-1].mean()
            if avg_vol_20d > 0:
                vol_ratio_daily = float(daily_df.iloc[-1]['volume']) / avg_vol_20d

        # Volume trend (expanding/contracting/flat)
        if len(df) >= 3:
            last_vols = df['volume'].iloc[-3:].values
            if last_vols[-1] > last_vols[-2] > last_vols[-3]:
                volume_trend = "expanding"
            elif last_vols[-1] < last_vols[-2] < last_vols[-3]:
                volume_trend = "contracting"
            else:
                volume_trend = "flat"
        else:
            volume_trend = "flat"

        # Volume divergence check
        volume_divergence = False
        if len(df) >= 5:
            last_4_volumes = df['volume'].iloc[-5:-1].values
            prices = df['close'].iloc[-5:-1].values
            if len(last_4_volumes) == 4 and len(prices) == 4:
                price_trending_up = prices[-1] > prices[0]
                price_trending_down = prices[-1] < prices[0]
                volume_trending_down = last_4_volumes[-1] < last_4_volumes[0]
                volume_trending_up = last_4_volumes[-1] > last_4_volumes[0]
                direction = signal["direction"]
                if direction == "LONG" and price_trending_up and volume_trending_down:
                    volume_divergence = True
                elif direction == "SHORT" and price_trending_down and volume_trending_down:
                    volume_divergence = True

        # ── Levels ────────────────────────────────────────────────────────
        support_20bar = float(df['low'].iloc[-20:].min()) if len(df) >= 20 else float(latest['low'])
        resistance_20bar = float(df['high'].iloc[-20:].max()) if len(df) >= 20 else float(latest['high'])
        
        dist_to_resistance = ((resistance_20bar - ltp) / ltp * 100) if ltp > 0 else 0
        dist_to_support = ((ltp - support_20bar) / ltp * 100) if ltp > 0 else 0

        # ── Target Multiplier (Adaptive) ──────────────────────────────────
        target_multiplier = 2.0
        if "TRENDING_UP" in market_regime or "TRENDING_DOWN" in market_regime:
            target_multiplier = 2.5
        elif "RANGING" in market_regime or "HIGH_VOLATILITY" in market_regime:
            target_multiplier = 1.5

        # ── Target Generation & Pre-filter ────────────────────────────────
        direction = signal["direction"]
        calculated_entry = ltp + (ltp * 0.0005) if direction == "LONG" else ltp - (ltp * 0.0005)
        
        # Calculate SL (constrained by min 0.3% and max 1.5 ATR)
        max_sl_dist = 1.5 * atr_14
        min_sl_dist = calculated_entry * 0.003
        
        if direction == "LONG":
            base_sl = support_20bar - (calculated_entry * 0.001)
            dist = calculated_entry - base_sl
            if dist < min_sl_dist:
                calculated_sl = calculated_entry - atr_14
            elif dist > max_sl_dist:
                logger.info(f"[Layer 4] {signal['symbol']} skipped — SL distance ({dist/calculated_entry*100:.2f}%) exceeds 1.5 ATR limit.")
                return None
            else:
                calculated_sl = base_sl
                
            risk = calculated_entry - calculated_sl
            calculated_target = calculated_entry + (target_multiplier * risk)
            
            # Target resistance violation check
            if calculated_target > resistance_20bar:
                if vol_ratio_6bar > 3.0 and volume_trend == "expanding":
                    calculated_target = min(calculated_target, resistance_20bar * 1.005)
                else:
                    logger.info(f"[Layer 4] {signal['symbol']} skipped — target ({calculated_target:.2f}) exceeds resistance ({resistance_20bar:.2f}) without volume breakout.")
                    return None
                    
        else:  # SHORT
            base_sl = resistance_20bar + (calculated_entry * 0.001)
            dist = base_sl - calculated_entry
            if dist < min_sl_dist:
                calculated_sl = calculated_entry + atr_14
            elif dist > max_sl_dist:
                logger.info(f"[Layer 4] {signal['symbol']} skipped — SL distance ({dist/calculated_entry*100:.2f}%) exceeds 1.5 ATR limit.")
                return None
            else:
                calculated_sl = base_sl
                
            risk = calculated_sl - calculated_entry
            calculated_target = calculated_entry - (target_multiplier * risk)
            
            if calculated_target < support_20bar:
                if vol_ratio_6bar > 3.0 and volume_trend == "expanding":
                    calculated_target = max(calculated_target, support_20bar * 0.995)
                else:
                    logger.info(f"[Layer 4] {signal['symbol']} skipped — target ({calculated_target:.2f}) exceeds support ({support_20bar:.2f}) without volume breakdown.")
                    return None

        # ── Market Context ────────────────────────────────────────────────
        sector = get_sector(symbol)
        nifty_change = self.get_nifty_change_pct()
        sector_change = self._sector_perf.get(sector, nifty_change)
        sector_vs_nifty = sector_change - nifty_change
        
        # Stock vs Nifty 5-day relative strength
        stock_vs_nifty_5day = 0.0
        if not daily_df.empty and len(daily_df) >= 6:
            stock_5day = ((float(daily_df.iloc[-1]['close']) - float(daily_df.iloc[-6]['close'])) 
                          / float(daily_df.iloc[-6]['close'])) * 100
            nifty_5day = self._nifty_trend.get("change_5day_pct", 0.0)
            stock_vs_nifty_5day = stock_5day - nifty_5day

        # ── Recent Completed Candles (Anchor to session, max 20 candles) ──
        completed_df = df.iloc[:-1]
        max_candles = 20
        if len(completed_df) > max_candles:
            completed_df = completed_df.iloc[-max_candles:]

        recent_candles = []
        for _, c in completed_df.iterrows():
            recent_candles.append({
                "o": round(float(c['open']), 2),
                "h": round(float(c['high']), 2),
                "l": round(float(c['low']), 2),
                "c": round(float(c['close']), 2),
                "v": int(c['volume']),
            })

        # ── Time Context ──────────────────────────────────────────────────
        now_ist = datetime.now(IST)
        signal_time = now_ist.strftime("%H:%M")
        tod_h = now_ist.hour
        if tod_h < 12:
            time_bucket = "MORNING"
        elif tod_h < 13 or (tod_h == 13 and now_ist.minute < 30):
            time_bucket = "MIDDAY"
        else:
            time_bucket = "AFTERNOON"

        # ── Build payload ─────────────────────────────────────────────────
        return {
            "symbol": symbol,
            "target_multiplier": target_multiplier,
            "calculated_entry": round(calculated_entry, 2),
            "calculated_sl": round(calculated_sl, 2),
            "calculated_target": round(calculated_target, 2),
            "signal_direction": direction,
            "signal_time": signal_time,
            "time_bucket": time_bucket,
            "layer3_conditions_met": signal.get("conditions_met", 0),
            "discovery_score": signal.get("discovery_score"),
            "discovery_rank": signal.get("discovery_rank"),

            "price_context": {
                "ltp": round(ltp, 2),
                "price_vs_vwap_pct": round(((ltp - vwap) / vwap * 100) if vwap > 0 else 0, 2),
                "price_vs_ema20_daily_pct": round(price_vs_ema20_daily, 2),
                "price_vs_20day_high_pct": round(price_vs_20day_high, 2),
                "atr_14_5min": round(atr_14, 2),
                "atr_pct": round(atr_pct, 2),
            },

            "momentum": {
                "rsi_14_5min": round(rsi_14_5min, 1),
                "rsi_14_daily": round(rsi_14_daily, 1),
                "ema20_5min_slope": ema_slope,
                "candle_strength_last": round(abs(candle_strength), 2),
                "price_slope_10bar": round(price_slope, 2),
                "volume_divergence": volume_divergence,
            },

            "volume": {
                "vol_ratio_20day_daily": round(vol_ratio_daily, 2),
                "vol_ratio_6bar_5min": round(vol_ratio_6bar, 2),
                "volume_trend": volume_trend,
            },

            "levels": {
                "support_20bar_5min": round(support_20bar, 2),
                "resistance_20bar_5min": round(resistance_20bar, 2),
                "distance_to_resistance_pct": round(dist_to_resistance, 2),
                "distance_to_support_pct": round(dist_to_support, 2),
                "previous_day_high": round(prev_day_high, 2) if prev_day_high > 0 else 0.0,
                "previous_day_low": round(prev_day_low, 2) if prev_day_low > 0 else 0.0,
                "cleared_previous_day_high": bool(ltp > (prev_day_high * 1.001)) if prev_day_high > 0 else False,
                "cleared_previous_day_low": bool(ltp < (prev_day_low * 0.999)) if prev_day_low > 0 else False,
            },

            "market_context": {
                "market_regime": market_regime,
                "nifty_trend_today": self._nifty_trend.get("trend", "sideways"),
                "bank_nifty_slope": self._bank_nifty_trend.get("slope", "flat"),
                "bank_nifty_change_3bar_pct": self._bank_nifty_trend.get("change_3bar_pct", 0.0),
                "india_vix": round(self._vix_value.get("value", 0), 1),
                "sector": sector,
                "sector_vs_nifty_today_pct": round(sector_vs_nifty, 2),
                "stock_vs_nifty_5day_pct": round(stock_vs_nifty_5day, 2),
            },

            "recent_candles_5min": recent_candles,
        }

    def _get_cached_candles(self, instrument_key: str) -> pd.DataFrame:
        """Get candles from rolling cache, refreshing every CANDLE_CACHE_REFRESH_MIN minutes.
        
        TTL guard: if the cache entry was written on a different calendar date (e.g. bot ran
        overnight), it is invalidated immediately to prevent yesterday's candles being used.
        """
        now = _time.time()
        today_date = datetime.now(IST).date()
        cache_entry = self._candle_cache.get(instrument_key)
        refresh_interval = Config.CANDLE_CACHE_REFRESH_MIN * 60

        if cache_entry:
            # Cross-day staleness check
            cache_date = cache_entry.get("trade_date")
            if cache_date and cache_date != today_date:
                logger.debug(f"[Layer 4] Cache for {instrument_key} is from {cache_date} — invalidating (cross-day TTL).")
                del self._candle_cache[instrument_key]
                cache_entry = None
            elif (now - cache_entry["last_refresh"]) < refresh_interval:
                return cache_entry["df"]

        # Refresh
        df = self.engine.get_ohlc(instrument_key, interval="5minute")
        if not df.empty:
            self._candle_cache[instrument_key] = {"df": df, "last_refresh": now, "trade_date": today_date}
        return df

    def _get_cached_daily_candles(self, instrument_key: str, days: int = 45) -> pd.DataFrame:
        """Get daily candles from cache, refreshing every 4 hours.
        
        TTL guard: cross-day check ensures overnight restarts don't reuse the
        previous session's daily data.
        """
        now = _time.time()
        today_date = datetime.now(IST).date()
        cache_entry = self._daily_cache.get(instrument_key)
        refresh_interval = 4 * 3600  # 4 hours

        if cache_entry:
            cache_date = cache_entry.get("trade_date")
            if cache_date and cache_date != today_date:
                logger.debug(f"[Layer 4] Daily cache for {instrument_key} is from {cache_date} — invalidating (cross-day TTL).")
                del self._daily_cache[instrument_key]
                cache_entry = None
            elif (now - cache_entry["last_refresh"]) < refresh_interval:
                return cache_entry["df"]
            
        df = self.engine.get_historical_candles_daily(instrument_key, days=days)
        if not df.empty:
            self._daily_cache[instrument_key] = {"df": df, "last_refresh": now, "trade_date": today_date}
        return df

    def _refresh_market_context(self, indices_cache: dict, prev_close_cache: dict = None):
        """Refresh Nifty trend, VIX, and sector performance at configured intervals."""
        now = _time.time()
        prev_close_cache = prev_close_cache or {}

        # Nifty trend — refresh every 15 minutes
        if (now - self._nifty_trend.get("last_refresh", 0)) > Config.MARKET_DATA_REFRESH_MIN * 60:
            nifty_ltp = indices_cache.get("NSE_INDEX|Nifty 50", 0)
            
            # Use prev_close_cache first, fallback to indices_cache
            nifty_prev = prev_close_cache.get("NSE_INDEX|Nifty 50", indices_cache.get("NSE_INDEX|Nifty 50_prev", 0))
            
            # Try to get previous close from a separate cache if available
            from config import SystemState
            # Simple approach: use the change percentage
            if nifty_ltp > 0 and nifty_prev > 0:
                change_pct = ((nifty_ltp - nifty_prev) / nifty_prev) * 100
            else:
                change_pct = 0.0

            if change_pct > 0.3:
                trend = "bullish"
            elif change_pct < -0.3:
                trend = "bearish"
            else:
                trend = "sideways"

            nifty_5day_pct = 0.0
            try:
                nifty_df = self._get_cached_daily_candles("NSE_INDEX|Nifty 50", days=10)
                if not nifty_df.empty and len(nifty_df) >= 6:
                    nifty_5day_pct = ((float(nifty_df.iloc[-1]['close']) - float(nifty_df.iloc[-6]['close'])) 
                                      / float(nifty_df.iloc[-6]['close'])) * 100
            except Exception as ne:
                logger.error(f"Failed to fetch Nifty 5-day daily candles: {ne}")

            self._nifty_trend = {
                "trend": trend,
                "change_pct": round(change_pct, 2),
                "change_5day_pct": round(nifty_5day_pct, 2),
                "last_refresh": now,
            }

        # VIX — refresh every 15 minutes
        if (now - self._vix_value.get("last_refresh", 0)) > Config.MARKET_DATA_REFRESH_MIN * 60:
            vix = indices_cache.get("NSE_INDEX|India VIX", 0)
            self._vix_value = {"value": vix, "last_refresh": now}

        # Bank Nifty slope — refresh every 5 minutes
        if (now - self._bank_nifty_trend.get("last_refresh", 0)) > 5 * 60:
            try:
                df_bn = self.engine.get_ohlc("NSE_INDEX|Nifty Bank", interval="5minute")
                if not df_bn.empty and len(df_bn) >= 5:
                    df_bn['ema_20'] = df_bn['close'].ewm(span=20, adjust=False).mean()
                    latest_bn = df_bn.iloc[-2]  # Completed candle
                    prev_bn = df_bn.iloc[-3] if len(df_bn) >= 3 else latest_bn
                    
                    ema_bn_now = float(latest_bn.get("ema_20", 0))
                    ema_bn_prev = float(prev_bn.get("ema_20", 0))
                    
                    close_bn_now = float(latest_bn["close"])
                    close_bn_prev = float(prev_bn["close"])
                    
                    roc_3bar = 0.0
                    if len(df_bn) >= 5:
                        old_close = float(df_bn.iloc[-4]["close"])
                        if old_close > 0:
                            roc_3bar = ((close_bn_now - old_close) / old_close) * 100
                            
                    if ema_bn_now > ema_bn_prev * 1.0005:
                        slope = "rising"
                    elif ema_bn_now < ema_bn_prev * 0.9995:
                        slope = "falling"
                    else:
                        slope = "flat"
                        
                    self._bank_nifty_trend = {
                        "slope": slope,
                        "change_3bar_pct": round(roc_3bar, 3),
                        "last_refresh": now
                    }
            except Exception as bne:
                logger.error(f"Failed to fetch Bank Nifty candles for slope: {bne}")

        # ── IMPROVEMENT 6: Sector performance — refresh every 30 minutes ─
        # Maps available Upstox index keys to sector names used in SECTOR_MAP.
        # Only sectors covered by already-subscribed index keys are tracked.
        if (now - self._sector_refresh_time) > Config.SECTOR_DATA_REFRESH_MIN * 60:
            SECTOR_INDEX_MAP = {
                "Banking":          ("NSE_INDEX|Nifty Bank",           "NSE_INDEX|Nifty Bank_prev"),
                "Financial Services": ("NSE_INDEX|Nifty Fin Service",  "NSE_INDEX|Nifty Fin Service_prev"),
                "IT":               ("NSE_INDEX|Nifty IT",             "NSE_INDEX|Nifty IT_prev"),
                "Auto":             ("NSE_INDEX|Nifty Auto",           "NSE_INDEX|Nifty Auto_prev"),
                "Pharma":           ("NSE_INDEX|Nifty Pharma",         "NSE_INDEX|Nifty Pharma_prev"),
                "Metals":           ("NSE_INDEX|Nifty Metal",          "NSE_INDEX|Nifty Metal_prev"),
                "FMCG":             ("NSE_INDEX|Nifty FMCG",           "NSE_INDEX|Nifty FMCG_prev"),
                "Oil & Gas":        ("NSE_INDEX|Nifty Oil & Gas",      "NSE_INDEX|Nifty Oil & Gas_prev"),
                "Healthcare":       ("NSE_INDEX|Nifty Healthcare",     "NSE_INDEX|Nifty Healthcare_prev"),
                "Realty":           ("NSE_INDEX|Nifty Realty",         "NSE_INDEX|Nifty Realty_prev"),
                "Infrastructure":   ("NSE_INDEX|Nifty Infra",          "NSE_INDEX|Nifty Infra_prev"),
                "Power":            ("NSE_INDEX|Nifty Energy",         "NSE_INDEX|Nifty Energy_prev"),
            }
            new_sector_perf = {}
            for sector_name, (ltp_key, prev_key) in SECTOR_INDEX_MAP.items():
                sec_ltp  = indices_cache.get(ltp_key, 0)
                sec_prev = indices_cache.get(prev_key, 0)
                if sec_ltp > 0 and sec_prev > 0:
                    new_sector_perf[sector_name] = round(
                        ((sec_ltp - sec_prev) / sec_prev) * 100, 2
                    )
            if new_sector_perf:
                self._sector_perf = new_sector_perf
                logger.debug(f"[Layer 4] Sector performance updated: {new_sector_perf}")
            self._sector_refresh_time = now

    def get_nifty_trend(self) -> str:
        return self._nifty_trend.get("trend", "sideways")

    def get_nifty_change_pct(self) -> float:
        return self._nifty_trend.get("change_pct", 0.0)

    def get_vix_value(self) -> float:
        return self._vix_value.get("value", 0.0)

    def get_sector_perf(self) -> dict:
        """Returns latest sector performance dict for Layer 3 sector filter."""
        return dict(self._sector_perf)

"""
pipeline_layer1.py — Morning Pre-Filter (Section 3, Layer 1)
─────────────────────────────────────────────────────────────
Runs once at 9:00 AM. Reduces the full universe (~500 stocks) to a focused
watchlist of 80-120 stocks before the market opens.

Data source: Previous day's daily OHLCV candles for all stocks.
Output: morning_watchlist table in the database.
"""

import logging
import numpy as np
import pandas as pd
from datetime import datetime
import pytz

from config import Config
from nifty500_instruments import get_universe, get_sector
from database import (
    SessionLocal, MorningWatchlist, ActiveWatchlist,
    get_ist_now, get_ist_date
)

IST = pytz.timezone('Asia/Kolkata')
logger = logging.getLogger(__name__)


class MorningPreFilter:
    """Layer 1: Reduce full universe to 80-120 stocks at 9:00 AM."""

    def __init__(self, execution_engine):
        self.engine = execution_engine
        self.watchlist = []  # List of dicts: {symbol, instrument_key, direction_bias, ...}

    def run(self) -> list:
        """Execute Layer 1 morning pre-filter. Returns list of qualifying stocks."""
        logger.info("[Layer 1] Starting morning pre-filter...")
        universe = get_universe()
        qualifying = []

        # Fetch Nifty 50 5-day return for relative strength comparison
        nifty_5day_return = self._get_nifty_5day_return()
        logger.info(f"[Layer 1] Nifty 5-day return: {nifty_5day_return:.2f}% | Evaluating {len(universe)} Nifty 500 stocks...")

        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _eval_single(item):
            sym, ik = item
            try:
                df = self.engine.get_historical_candles_daily(ik, days=45)
                if df.empty or len(df) < 21:
                    return None
                return self._evaluate_stock(sym, ik, df, nifty_5day_return)
            except Exception as e:
                logger.debug(f"[Layer 1] Error processing {sym}: {e}")
                return None

        items = list(universe.items())
        processed = 0
        with ThreadPoolExecutor(max_workers=12) as executor:
            future_to_sym = {executor.submit(_eval_single, it): it[0] for it in items}
            for future in as_completed(future_to_sym):
                processed += 1
                res = future.result()
                if res:
                    qualifying.append(res)
                if processed % 100 == 0 or processed == len(items):
                    logger.info(f"[Layer 1] Processed {processed}/{len(items)} Nifty 500 stocks ({len(qualifying)} qualified so far)...")

        # Rank and sort qualifying stocks by composite momentum score
        qualifying.sort(
            key=lambda x: (
                float(x.get("relative_strength_5day") or 0.0) * 1.5 + 
                float(x.get("vol_ratio_20day") or 1.0) * 10.0
            ),
            reverse=True
        )

        logger.info(f"[Layer 1] Pre-filter complete. {len(qualifying)} stocks qualified out of {processed} Nifty 500 stocks processed.")

        # ── Gracefully append scraped breakout stocks (fail-safe) ─────────────
        try:
            from watchlist_scraper import fetch_breakout_stocks
            scraped_stocks = fetch_breakout_stocks()
            added_count = 0
            existing_symbols = {item["symbol"] for item in qualifying}
            
            for item in scraped_stocks:
                symbol = item["symbol"]
                if symbol in universe and symbol not in existing_symbols:
                        qualifying.append({
                            "symbol": symbol,
                        "instrument_key": universe[symbol],
                            "direction_bias": item["direction_bias"],
                            "vol_ratio_20day": None,
                            "relative_strength_5day": None,
                            "price_vs_ema20_daily": None,
                            "price_vs_20day_high_pct": None
                        })
                        added_count += 1
            logger.info(f"[Layer 1] Scraper successfully added {added_count} breakout stocks to the watchlist.")
        except Exception as scraper_err:
            logger.error(f"[Layer 1] External breakout scraping failed: {scraper_err}. Continuing with standard filters.")

        self.watchlist = qualifying
        self._save_to_db(qualifying)
        self._initialize_active_watchlist(qualifying)
        return qualifying

    def _evaluate_stock(self, symbol: str, instrument_key: str, df: pd.DataFrame, nifty_5day_return: float) -> dict:
        """Apply the 4 pre-filter rules. Returns dict if all pass, None otherwise."""
        yesterday = df.iloc[-1]  # Most recent completed day
        close = float(yesterday['close'])
        volume = float(yesterday['volume'])

        # ── Rule 1: Volume Activity ─────────────────────────────────────────
        # Yesterday's volume > 1.3x the 20-day average daily volume
        if len(df) >= 21:
            avg_vol_20 = df['volume'].iloc[-21:-1].mean()
        else:
            avg_vol_20 = df['volume'].iloc[:-1].mean()
        
        if avg_vol_20 <= 0 or volume <= 0:
            return None
        vol_ratio = volume / avg_vol_20
        if vol_ratio < 1.3:
            return None

        # ── Rule 2: Trend Filter ────────────────────────────────────────────
        # EMA 20 on daily chart
        ema_20 = df['close'].ewm(span=20, adjust=False).mean().iloc[-1]
        price_vs_ema20 = ((close - ema_20) / ema_20) * 100

        is_long_candidate = close > ema_20
        is_short_candidate = close < ema_20

        if not is_long_candidate and not is_short_candidate:
            return None

        # ── Rule 3: Not Extended (Breakout Zone Only) ───────────────────────
        rolling_high_20 = df['high'].iloc[-20:].max()
        rolling_low_20 = df['low'].iloc[-20:].min()

        if is_long_candidate:
            # Closing price is no more than 8% below the 20-day rolling high
            pct_below_high = ((rolling_high_20 - close) / rolling_high_20) * 100
            if pct_below_high > 8.0:
                return None
            price_vs_20day_high = -pct_below_high  # negative = below high
        else:
            # SHORT: closing price is no more than 8% above the 20-day rolling low
            pct_above_low = ((close - rolling_low_20) / rolling_low_20) * 100
            if pct_above_low > 8.0:
                return None
            price_vs_20day_high = pct_above_low

        # ── Rule 4: Relative Strength ──────────────────────────────────────
        if len(df) >= 6:
            stock_5day_return = ((close - float(df.iloc[-6]['close'])) / float(df.iloc[-6]['close'])) * 100
        else:
            stock_5day_return = 0.0

        if is_long_candidate:
            if stock_5day_return < nifty_5day_return:
                return None
        else:
            if stock_5day_return > nifty_5day_return:
                return None

        relative_strength = stock_5day_return - nifty_5day_return

        direction = "LONG_CANDIDATE" if is_long_candidate else "SHORT_CANDIDATE"
        return {
            "symbol": symbol,
            "instrument_key": instrument_key,
            "direction_bias": direction,
            "avg_vol_20": float(avg_vol_20),
            "vol_ratio_20day": round(vol_ratio, 2),
            "relative_strength_5day": round(relative_strength, 2),
            "price_vs_ema20_daily": round(price_vs_ema20, 2),
            "price_vs_20day_high_pct": round(price_vs_20day_high, 2),
        }

    def _get_nifty_5day_return(self) -> float:
        """Fetch Nifty 50 5-day return for relative strength comparison."""
        try:
            nifty_key = "NSE_INDEX|Nifty 50"
            df = self.engine.get_historical_candles_daily(nifty_key, days=20)
            if not df.empty and len(df) >= 6:
                current = float(df.iloc[-1]['close'])
                five_days_ago = float(df.iloc[-6]['close'])
                return ((current - five_days_ago) / five_days_ago) * 100
        except Exception as e:
            logger.error(f"[Layer 1] Error fetching Nifty 5-day return: {e}")
        return 0.0

    def _save_to_db(self, stocks: list):
        """Save morning watchlist to database."""
        db = SessionLocal()
        try:
            today = get_ist_date()
            # Clear any existing entries for today (idempotent)
            db.query(MorningWatchlist).filter(MorningWatchlist.date == today).delete()
            db.commit()

            for stock in stocks:
                entry = MorningWatchlist(
                    date=today,
                    symbol=stock["symbol"],
                    instrument_key=stock["instrument_key"],
                    direction_bias=stock["direction_bias"],
                    vol_ratio_20day=stock.get("vol_ratio_20day"),
                    relative_strength_5day=stock.get("relative_strength_5day"),
                    price_vs_ema20_daily=stock.get("price_vs_ema20_daily"),
                    price_vs_20day_high_pct=stock.get("price_vs_20day_high_pct"),
                )
                db.add(entry)
            db.commit()
            logger.info(f"[Layer 1] Saved {len(stocks)} stocks to morning_watchlist.")
        except Exception as e:
            db.rollback()
            logger.error(f"[Layer 1] Error saving morning watchlist: {e}")
        finally:
            db.close()

    def _initialize_active_watchlist(self, stocks: list):
        """Initialize active_watchlist from morning_watchlist (full list starts active)."""
        db = SessionLocal()
        try:
            today = get_ist_date()
            now = get_ist_now()
            # Clear any existing entries for today
            db.query(ActiveWatchlist).filter(ActiveWatchlist.date == today).delete()
            db.commit()

            for stock in stocks:
                entry = ActiveWatchlist(
                    date=today,
                    symbol=stock["symbol"],
                    instrument_key=stock["instrument_key"],
                    direction_bias=stock["direction_bias"],
                    added_at=now,
                )
                db.add(entry)
            db.commit()
            logger.info(f"[Layer 1] Initialized active_watchlist with {len(stocks)} stocks.")
        except Exception as e:
            db.rollback()
            logger.error(f"[Layer 1] Error initializing active watchlist: {e}")
        finally:
            db.close()

    @staticmethod
    def load_from_db() -> list:
        """Load today's morning watchlist from DB (for startup recovery)."""
        db = SessionLocal()
        try:
            today = get_ist_date()
            rows = db.query(MorningWatchlist).filter(MorningWatchlist.date == today).all()
            return [{
                "symbol": r.symbol,
                "instrument_key": r.instrument_key,
                "direction_bias": r.direction_bias,
                "vol_ratio_20day": r.vol_ratio_20day,
                "relative_strength_5day": r.relative_strength_5day,
                "price_vs_ema20_daily": r.price_vs_ema20_daily,
                "price_vs_20day_high_pct": r.price_vs_20day_high_pct,
            } for r in rows]
        finally:
            db.close()

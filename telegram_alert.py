"""
telegram_alert.py — Telegram Bot Notification Service (Section 8)
─────────────────────────────────────────────────────────────────
Sends alerts via Telegram Bot API for all critical events.
Uses direct HTTP calls — no external Telegram SDK needed.
"""

import os
import logging
import requests
import asyncio
from functools import partial
from config import Config

logger = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


import time as _time
import threading

def _get_telegram_creds():
    token = Config.TELEGRAM_BOT_TOKEN or os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = Config.TELEGRAM_CHAT_ID or os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        try:
            from database import SessionLocal, SystemConfigDB
            with SessionLocal() as db:
                if not token:
                    row = db.query(SystemConfigDB).filter(SystemConfigDB.config_key == "TELEGRAM_BOT_TOKEN").first()
                    if row and row.config_value:
                        token = row.config_value.strip()
                if not chat_id:
                    row = db.query(SystemConfigDB).filter(SystemConfigDB.config_key == "TELEGRAM_CHAT_ID").first()
                    if row and row.config_value:
                        chat_id = row.config_value.strip()
        except Exception:
            pass
    return token, chat_id

def _send_telegram_blocking(message: str, parse_mode: str = "HTML", retries: int = 1) -> bool:
    """Send a Telegram message synchronously. Supports retry logic on failures."""
    token, chat_id = _get_telegram_creds()
    
    if not token or not chat_id:
        logger.warning("Telegram not configured (missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in .env or database system_config). Alert not sent.")
        return False
    
    url = TELEGRAM_API_URL.format(token=token)
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True
    }
    
    for attempt in range(retries):
        try:
            response = requests.post(url, json=payload, timeout=10)
            if response.status_code == 200:
                return True
            else:
                logger.warning(f"Telegram send attempt {attempt + 1} failed ({response.status_code}): {response.text}")
        except Exception as e:
            logger.warning(f"Telegram send attempt {attempt + 1} error: {e}")
            
        if attempt < retries - 1:
            _time.sleep(2)
            
    logger.error(f"Telegram send failed after {retries} attempts.")
    return False


def send_telegram(message: str, parse_mode: str = "HTML", retries: int = 1) -> bool:
    """Dispatches a Telegram message asynchronously in a background daemon thread to protect the main process."""
    t = threading.Thread(
        target=_send_telegram_blocking,
        args=(message, parse_mode, retries),
        daemon=True
    )
    t.start()
    return True


def send_telegram_critical(message: str, parse_mode: str = "HTML") -> bool:
    """Send a Telegram message in the background with 3 retries for critical events."""
    return send_telegram(message, parse_mode, retries=3)


async def send_telegram_async(message: str, parse_mode: str = "HTML") -> bool:
    """Async wrapper — runs send_telegram in a thread to avoid blocking the event loop."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(send_telegram, message, parse_mode))


# ─── Alert Templates (Section 8) ─────────────────────────────────────────────

def alert_bot_startup(date_str: str, symbols: list):
    count = len(symbols)
    sym_str = ", ".join(symbols)
    send_telegram(f"✅ intradayBot started. Date: {date_str}.\n\n📋 Morning Watchlist ({count} stocks):\n{sym_str}")

def alert_token_refresh_failed():
    send_telegram("🚨 Upstox token refresh FAILED. Bot did not start. Manual login required.")

def alert_watchlist_refreshed(time_str: str, count: int):
    pass  # User requested to disable this notification

def alert_trade_entry(direction: str, symbol: str, fill_price: float, sl: float, target: float, qty: int, confidence: int, reasoning: str):
    emoji = "📈" if direction == "LONG" else "📉"
    send_telegram(
        f"{emoji} {direction} ENTERED: {symbol} @ ₹{fill_price:.2f}\n"
        f"Qty: {qty} | SL: ₹{sl:.2f} | Target: ₹{target:.2f}\n"
        f"Confidence: {confidence}/5 | Reason: {reasoning}"
    )

def alert_trade_exit(symbol: str, reason: str, entry: float, exit_price: float, pnl: float):
    send_telegram(
        f"✅ TRADE CLOSED: {symbol} [{reason}]\n"
        f"Entry: ₹{entry:.2f} | Exit: ₹{exit_price:.2f} | P&L: ₹{pnl:+.2f}"
    )

def alert_consecutive_losses(user_id: int):
    pass  # User requested to disable this notification

# VIX alert deduplication state
_vix_cautious_alerted = False
_vix_halt_alerted = False

def reset_vix_alerts():
    """Reset daily VIX alert flags (called at start of day reset loop)."""
    global _vix_cautious_alerted, _vix_halt_alerted
    _vix_cautious_alerted = False
    _vix_halt_alerted = False

def alert_daily_limit_reached(user_id: int, max_trades: int):
    pass  # User requested to disable this notification

def alert_vix_cautious(vix_value: float):
    global _vix_cautious_alerted
    if not _vix_cautious_alerted:
        _vix_cautious_alerted = True

def alert_vix_too_high(vix_value: float):
    global _vix_halt_alerted
    if not _vix_halt_alerted:
        _vix_halt_alerted = True

def alert_global_halt_activated():
    send_telegram_critical("🚨 GLOBAL HALT ACTIVATED by admin. All new entries stopped. Existing positions still monitored.")

def alert_global_halt_lifted():
    send_telegram("✅ GLOBAL HALT LIFTED. Trading resumed for all users.")

def alert_order_status_unknown(symbol: str, order_id: str):
    send_telegram_critical(
        f"🚨 ORDER STATUS UNKNOWN: {symbol}. Manual check required. Order ID: {order_id}\n"
        f"No further orders placed for {symbol} today."
    )

def alert_stale_feed(minutes: int, num_symbols: int):
    send_telegram_critical(f"⚠️ Market feed stale for {minutes} minutes for {num_symbols} symbols. Check WebSocket.")

def alert_claude_invalid_json(time_str: str):
    send_telegram(f"⚠️ Claude API returned invalid JSON at {time_str}. No trades placed this cycle.")

def alert_claude_api_failed(time_str: str, error_type: str):
    send_telegram(f"⚠️ Claude API call failed at {time_str} ({error_type}). Skipping this scan cycle.")

def alert_daily_summary(summary_text: str):
    send_telegram(summary_text)

def alert_reconciliation(mismatches: int, ghost: int, untracked: int, details: str = ""):
    msg = f"📋 Reconciliation complete. Mismatches: {mismatches} | Ghost: {ghost} | Untracked: {untracked}"
    send_telegram(msg)
    if mismatches > 0 or ghost > 0 or untracked > 0:
        if details:
            send_telegram(f"📋 Reconciliation Details:\n{details[:3500]}")

def alert_no_trades_today(date_str: str, signals_generated: int, top_skip_reason: str):
    pass  # User requested to disable this notification

def alert_trailing_sl_updated(symbol: str, stage: str, old_sl: float, new_sl: float, current_price: float):
    send_telegram(
        f"🔒 TRAILING SL UPDATED: {symbol}\n"
        f"Stage: {stage}\n"  # "BREAKEVEN" or "TRAILING"
        f"SL moved: ₹{old_sl:.2f} → ₹{new_sl:.2f}\n"
        f"Current price: ₹{current_price:.2f}"
    )

def alert_orb_range_set(symbol: str, or_high: float, or_low: float, range_pct: float, status: str):
    pass  # User requested to disable this notification

def alert_circuit_limit_hit(symbol: str, side: str, qty: int, sl_price: float, context: str):
    send_telegram_critical(
        f"🚨 <b>CIRCUIT LIMIT HIT</b>: {symbol}\n"
        f"Upstox rejected Stop Loss order during {context}.\n"
        f"Side: {side} | Qty: {qty} | Attempted SL: ₹{sl_price:.2f}\n"
        f"⚠️ <b>Position is UNMANAGED. Manual intervention required immediately!</b>"
    )

def alert_heartbeat(time_str: str, stocks_scanned: int, active_trades: int, daily_pnl: float):
    send_telegram(
        f"ℹ️ <b>Heartbeat</b> | {time_str} IST\n"
        f"• Loop Alive\n"
        f"• Stocks Scanned: {stocks_scanned}\n"
        f"• Active Trades: {active_trades}\n"
        f"• Daily P&L: ₹{daily_pnl:+.2f}"
    )

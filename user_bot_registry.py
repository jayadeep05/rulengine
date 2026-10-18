"""
user_bot_registry.py — Per-user trading bot state manager
-----------------------------------------------------------
Provides isolated SystemState + TradeManager + ExecutionEngine per user.
Each user's bot runs independently without sharing state with others.
"""

import logging
from typing import Dict, Optional
from config import SystemState
from trade_manager import TradeManager
from execution import UpstoxExecutionEngine
from dual_strategy_engine import DualStrategyEngine

logger = logging.getLogger(__name__)


class UserBot:
    """Container for a single user's bot components."""

    def __init__(self, user_id: int, credentials: dict, user_config: dict = None):
        self.user_id   = user_id
        self.state     = SystemState()
        self.config    = user_config or {}
        self.state.usable_balance = float(self.config.get("MANUAL_CAPITAL", 100000.0))
        self.state.active_capital = self.state.usable_balance
        self.tm        = TradeManager(user_id=user_id)
        self.engine    = UpstoxExecutionEngine(credentials=credentials, mode=self.config.get("MODE"))
        self.dual      = DualStrategyEngine(self.engine)
        self.creds     = credentials  # keep reference for refresh
        logger.info(f"[BOT] Initialized bot for user_id={user_id}")

    def refresh_credentials(self, credentials: dict):
        """Update credentials without restarting the bot."""
        self.creds = credentials
        self.engine._creds = credentials
        logger.info(f"[BOT] Credentials refreshed for user_id={self.user_id}")


# ── Global registry: user_id -> UserBot ────────────────────────────────────
_registry: Dict[int, UserBot] = {}


def get_or_create_bot(user_id: int, db) -> UserBot:
    """
    Return existing bot for user_id, or create one.
    Loads credentials from DB if not already in registry.
    """
    if user_id in _registry:
        return _registry[user_id]

    from credential_service import get_decrypted_credentials
    from main import get_user_config_dict  # Import here to avoid circular dependency
    
    creds = get_decrypted_credentials(db, user_id)
    u_cfg = get_user_config_dict(db, user_id)
    bot = UserBot(user_id=user_id, credentials=creds, user_config=u_cfg)
    _registry[user_id] = bot
    return bot


def get_bot(user_id: int) -> Optional[UserBot]:
    """Return bot if it exists, else None (user has never logged in this session)."""
    return _registry.get(user_id)


def reload_bot_credentials(user_id: int, db=None):
    """Called after credential update to refresh engine without stopping bot."""
    from credential_service import get_decrypted_credentials
    from database import SessionLocal
    standalone = False
    if db is None:
        db = SessionLocal()
        standalone = True
    try:
        try:
            db.expire_all()
        except Exception:
            pass
        creds = get_decrypted_credentials(db, user_id)
        if user_id in _registry:
            _registry[user_id].refresh_credentials(creds)
        else:
            _registry[user_id] = UserBot(user_id=user_id, credentials=creds)
        try:
            import market_feed
            market_feed.update_credentials(creds)
        except Exception:
            pass
    finally:
        if standalone:
            db.close()


def all_bots() -> Dict[int, UserBot]:
    return _registry


def reload_user_config(user_id: int, db):
    """Refreshes the bot's config from the DB."""
    from main import get_user_config_dict
    if user_id in _registry:
        new_cfg = get_user_config_dict(db, user_id)
        _registry[user_id].config = new_cfg
        _registry[user_id].state.usable_balance = float(new_cfg.get("MANUAL_CAPITAL", 100000.0))
        _registry[user_id].state.active_capital = _registry[user_id].state.usable_balance
        # Update engine mode if changed
        if hasattr(_registry[user_id], 'engine'):
            _registry[user_id].engine.mode = new_cfg.get("MODE", "TEST")
        logger.info(f"[BOT] Configuration reloaded for user_id={user_id} (Mode: {new_cfg.get('MODE')})")

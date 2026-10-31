"""
super_admin_service.py — Super Admin Management Engine
───────────────────────────────────────────────────────────────────────────────
Centralized service layer for Super Admin operations:
- User CRUD, activation/deactivation, password resets
- Per-user trading configuration and risk parameters
- Granular role and permission management
- Comprehensive user activity and trade audit history
- Complete isolation between users adhering to SOLID principles
"""

import json
from datetime import datetime
from typing import Optional, List, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import text, func

from database import (
    User, UserCredential, UserConfig, Trade, SystemLog, MasterLinkedAccount,
    get_ist_now, get_ist_date, log_system_event
)
from auth import hash_password
from credential_service import credential_field_status

# Default permission catalog
DEFAULT_ROLE_PERMISSIONS: Dict[str, Dict[str, Any]] = {
    "super_admin": {
        "allow_super_admin_panel": True,
        "allow_multi_account": True,
        "allow_automatic_engine": True,
        "allow_primeradar_intra": True,
        "allow_primeradar_options": True,
        "allow_dual_strategy": True,
        "allow_live_trading": True,
        "allow_risk_override": True,
        "max_capital_limit": 5000000.0,
    },
    "master_admin": {
        "allow_super_admin_panel": False,
        "allow_multi_account": True,
        "allow_automatic_engine": True,
        "allow_primeradar_intra": True,
        "allow_primeradar_options": True,
        "allow_dual_strategy": True,
        "allow_live_trading": True,
        "allow_risk_override": False,
        "max_capital_limit": 1000000.0,
    },
    "user": {
        "allow_super_admin_panel": False,
        "allow_multi_account": False,
        "allow_automatic_engine": True,
        "allow_primeradar_intra": True,
        "allow_primeradar_options": True,
        "allow_dual_strategy": True,
        "allow_live_trading": True,
        "allow_risk_override": False,
        "max_capital_limit": 200000.0,
    }
}


def get_user_effective_permissions(user: User) -> Dict[str, Any]:
    """Resolves base role permissions combined with custom per-user overrides."""
    role = (user.role or "user").lower()
    base = dict(DEFAULT_ROLE_PERMISSIONS.get(role, DEFAULT_ROLE_PERMISSIONS["user"]))
    if user.permissions_json:
        try:
            custom = json.loads(user.permissions_json)
            if isinstance(custom, dict):
                base.update(custom)
        except Exception:
            pass
    return base


def get_all_users_summary(db: Session) -> List[Dict[str, Any]]:
    """Fetches high-level summary of all registered users for administration table."""
    users = db.query(User).order_by(User.id.asc()).all()
    today = get_ist_date()

    results = []
    for u in users:
        # Credential status
        cred = credential_field_status(db, u.id)
        has_creds = bool(cred.get("upstox_client_id") and cred.get("upstox_access_token"))

        # Config status
        cfg = u.config
        mode = cfg.trading_mode if cfg else "TEST"
        is_running = bool(cfg.is_running) if cfg else False
        capital = float(cfg.manual_capital or 0.0) if cfg else 10000.0

        # Trade stats for today
        trades_today = db.query(Trade).filter(Trade.user_id == u.id, Trade.trade_date == today).all()
        total_pnl = sum(t.realized_pnl for t in trades_today if t.realized_pnl is not None)
        open_trades = sum(1 for t in trades_today if t.status == "OPEN")

        # Total lifetime trades
        lifetime_trades = db.query(func.count(Trade.id)).filter(Trade.user_id == u.id).scalar() or 0

        # Linked accounts
        linked_count = db.query(func.count(MasterLinkedAccount.id)).filter(
            MasterLinkedAccount.master_user_id == u.id,
            MasterLinkedAccount.is_active == 1
        ).scalar() or 0

        results.append({
            "id": u.id,
            "username": u.username,
            "email": u.email,
            "role": u.role,
            "is_active": getattr(u, "is_active", True),
            "created_at": u.created_at.strftime("%Y-%m-%d %H:%M") if u.created_at else "--",
            "last_login": u.last_login.strftime("%Y-%m-%d %H:%M") if getattr(u, "last_login", None) else "--",
            "upstox_connected": has_creds,
            "trading_mode": mode,
            "is_running": is_running,
            "capital": capital,
            "trades_today_count": len(trades_today),
            "open_trades_count": open_trades,
            "today_pnl": round(total_pnl, 2),
            "lifetime_trades": lifetime_trades,
            "linked_accounts_count": linked_count,
            "permissions": get_user_effective_permissions(u)
        })

    return results


def get_user_full_details(db: Session, target_user_id: int) -> Optional[Dict[str, Any]]:
    """Returns comprehensive details, trading config, linked accounts, and activity for a user."""
    u = db.query(User).filter(User.id == target_user_id).first()
    if not u:
        return None

    today = get_ist_date()

    # Upstox credentials status
    creds_status = credential_field_status(db, u.id)

    # Trading config
    cfg = u.config
    if not cfg:
        # Pre-seed default config if missing
        cfg = UserConfig(user_id=u.id)
        db.add(cfg)
        db.commit()
        db.refresh(cfg)

    config_dict = {
        "max_trades_per_day": cfg.max_trades_per_day or 5,
        "max_open_trades": cfg.max_open_trades or 2,
        "daily_loss_limit": float(cfg.daily_loss_limit or 2000.0),
        "trailing_sl_activation": float(cfg.trailing_sl_activation or 1.0),
        "volume_spike_ratio": float(cfg.volume_spike_ratio or 1.8),
        "min_candle_strength": float(cfg.min_candle_strength or 0.6),
        "min_stock_price": float(cfg.min_stock_price or 50.0),
        "max_stock_price": float(cfg.max_stock_price or 5000.0),
        "manual_capital": float(cfg.manual_capital or 10000.0),
        "hybrid_virtual_mode": bool(cfg.hybrid_virtual_mode),
        "trading_mode": cfg.trading_mode or "TEST",
        "is_running": bool(cfg.is_running),
        "updated_at": cfg.updated_at.strftime("%Y-%m-%d %H:%M:%S") if cfg.updated_at else "--"
    }

    # Linked accounts (as master)
    master_links = db.query(MasterLinkedAccount).filter(MasterLinkedAccount.master_user_id == u.id).all()
    linked_as_master = []
    for ml in master_links:
        luser = db.query(User).filter(User.id == ml.linked_user_id).first()
        if luser:
            linked_as_master.append({
                "link_id": ml.id,
                "user_id": luser.id,
                "username": luser.username,
                "email": luser.email,
                "is_active": bool(ml.is_active),
                "allocation_pct": float(ml.allocation_pct or 1.0),
                "linked_since": ml.created_at.strftime("%Y-%m-%d") if ml.created_at else "--"
            })

    # Linked under another master account
    slave_links = db.query(MasterLinkedAccount).filter(MasterLinkedAccount.linked_user_id == u.id).all()
    linked_under_masters = []
    for sl in slave_links:
        muser = db.query(User).filter(User.id == sl.master_user_id).first()
        if muser:
            linked_under_masters.append({
                "link_id": sl.id,
                "master_user_id": muser.id,
                "master_username": muser.username,
                "is_active": bool(sl.is_active),
                "allocation_pct": float(sl.allocation_pct or 1.0)
            })

    # Recent trades (latest 30)
    recent_trades = db.query(Trade).filter(Trade.user_id == u.id).order_by(Trade.entry_time.desc()).limit(30).all()
    trades_list = []
    for t in recent_trades:
        trades_list.append({
            "id": t.id,
            "trade_date": str(t.trade_date),
            "symbol": t.symbol,
            "side": t.side,
            "quantity": t.quantity,
            "entry_price": round(t.entry_price, 2) if t.entry_price else 0.0,
            "exit_price": round(t.exit_price, 2) if t.exit_price else None,
            "realized_pnl": round(t.realized_pnl, 2) if t.realized_pnl is not None else 0.0,
            "status": t.status,
            "strategy": t.strategy_type or "MOMENTUM",
            "entry_time": t.entry_time.strftime("%H:%M:%S") if t.entry_time else "--",
            "exit_time": t.exit_time.strftime("%H:%M:%S") if t.exit_time else "--",
            "exit_reason": t.exit_reason or "--"
        })

    # Recent activity logs (latest 30)
    recent_logs = db.query(SystemLog).filter(SystemLog.user_id == u.id).order_by(SystemLog.timestamp.desc()).limit(30).all()
    logs_list = []
    for l in recent_logs:
        logs_list.append({
            "id": l.id,
            "timestamp": l.timestamp.strftime("%Y-%m-%d %H:%M:%S") if l.timestamp else "--",
            "level": l.log_level,
            "event": l.event_type,
            "message": l.message
        })

    profile_dict = {
        "id": u.id,
        "username": u.username,
        "email": u.email,
        "role": u.role,
        "is_active": getattr(u, "is_active", True),
        "created_at": u.created_at.strftime("%Y-%m-%d %H:%M:%S") if u.created_at else "--",
        "last_login": u.last_login.strftime("%Y-%m-%d %H:%M:%S") if getattr(u, "last_login", None) else "--",
    }
    effective_perms = get_user_effective_permissions(u)

    return {
        "id": u.id,
        "username": u.username,
        "email": u.email,
        "role": u.role,
        "is_active": getattr(u, "is_active", True),
        "created_at": profile_dict["created_at"],
        "last_login": profile_dict["last_login"],
        "profile": profile_dict,
        "credentials": creds_status,
        "config": config_dict,
        "trading_config": config_dict,
        "permissions": effective_perms,
        "effective_permissions": effective_perms,
        "linked_accounts": linked_as_master,
        "linked_accounts_as_master": linked_as_master,
        "linked_under_masters": linked_under_masters,
        "recent_trades": trades_list,
        "recent_logs": logs_list
    }


def create_user_admin(
    db: Session,
    email: str,
    username: str,
    password: str,
    role: str = "user",
    is_active: bool = True,
    permissions: Optional[Dict[str, Any]] = None,
    admin_user_id: Optional[int] = None
) -> Dict[str, Any]:
    """Creates a new user account with default configuration."""
    email_clean = email.strip().lower()
    username_clean = username.strip()

    if db.query(User).filter(User.email == email_clean).first():
        raise ValueError(f"Email '{email_clean}' is already registered.")
    if db.query(User).filter(User.username == username_clean).first():
        raise ValueError(f"Username '{username_clean}' is already taken.")

    role_clean = role.lower() if role in ("super_admin", "master_admin", "admin", "user") else "user"
    if role_clean == "admin":
        role_clean = "master_admin"

    perms_json = json.dumps(permissions) if permissions else None

    user = User(
        email=email_clean,
        username=username_clean,
        password_hash=hash_password(password),
        role=role_clean,
        is_active=is_active,
        permissions_json=perms_json
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    # Automatically create default trading config
    cfg = UserConfig(user_id=user.id)
    db.add(cfg)
    db.commit()

    log_system_event(
        db, admin_user_id, "INFO", "USER_CREATED",
        f"Super Admin created user '{username_clean}' ({email_clean}) with role '{role_clean}'"
    )

    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "role": user.role,
        "is_active": user.is_active
    }


def update_user_admin(
    db: Session,
    target_user_id: int,
    email: str,
    username: str,
    role: str,
    is_active: bool,
    password: Optional[str] = None,
    permissions: Optional[Dict[str, Any]] = None,
    admin_user_id: Optional[int] = None
) -> Dict[str, Any]:
    """Updates user core details, credentials, status, or role."""
    user = db.query(User).filter(User.id == target_user_id).first()
    if not user:
        raise ValueError("User not found.")

    email_clean = email.strip().lower()
    username_clean = username.strip()

    # Check for email collision
    existing_email = db.query(User).filter(User.email == email_clean, User.id != target_user_id).first()
    if existing_email:
        raise ValueError(f"Email '{email_clean}' is already in use by another user.")

    # Check for username collision
    existing_uname = db.query(User).filter(User.username == username_clean, User.id != target_user_id).first()
    if existing_uname:
        raise ValueError(f"Username '{username_clean}' is already in use by another user.")

    role_clean = role.lower() if role in ("super_admin", "master_admin", "admin", "user") else user.role
    if role_clean == "admin":
        role_clean = "master_admin"

    # Prevent demoting the last super admin
    if user.role == "super_admin" and role_clean != "super_admin":
        super_count = db.query(func.count(User.id)).filter(User.role == "super_admin").scalar() or 0
        if super_count <= 1:
            raise ValueError("Cannot demote the only remaining Super Admin.")

    user.email = email_clean
    user.username = username_clean
    user.role = role_clean
    user.is_active = is_active

    if password and password.strip():
        user.password_hash = hash_password(password.strip())

    if permissions is not None:
        user.permissions_json = json.dumps(permissions)

    db.commit()
    db.refresh(user)

    log_system_event(
        db, admin_user_id, "INFO", "USER_UPDATED",
        f"Super Admin updated user #{user.id} ({user.username}): role={user.role}, active={user.is_active}"
    )

    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "role": user.role,
        "is_active": user.is_active
    }


def toggle_user_active_status(db: Session, target_user_id: int, admin_user_id: Optional[int] = None) -> bool:
    """Toggles active/inactive state of a user."""
    user = db.query(User).filter(User.id == target_user_id).first()
    if not user:
        raise ValueError("User not found.")

    if target_user_id == admin_user_id and user.is_active:
        raise ValueError("Super Admin cannot deactivate their own account.")

    user.is_active = not getattr(user, "is_active", True)
    db.commit()

    log_system_event(
        db, admin_user_id, "WARNING", "USER_STATUS_CHANGE",
        f"Super Admin set active={user.is_active} for user #{user.id} ({user.username})"
    )
    return user.is_active


def delete_user_admin(db: Session, target_user_id: int, admin_user_id: Optional[int] = None) -> bool:
    """Safely cascades and removes a user and their associated data."""
    if target_user_id == admin_user_id:
        raise ValueError("Super Admin cannot delete their own account.")

    user = db.query(User).filter(User.id == target_user_id).first()
    if not user:
        raise ValueError("User not found.")

    if user.role == "super_admin":
        super_count = db.query(func.count(User.id)).filter(User.role == "super_admin").scalar() or 0
        if super_count <= 1:
            raise ValueError("Cannot delete the only remaining Super Admin.")

    uname = user.username

    # Delete related child records
    db.query(MasterLinkedAccount).filter(
        (MasterLinkedAccount.master_user_id == target_user_id) |
        (MasterLinkedAccount.linked_user_id == target_user_id)
    ).delete(synchronize_session=False)

    db.query(UserCredential).filter(UserCredential.user_id == target_user_id).delete(synchronize_session=False)
    db.query(UserConfig).filter(UserConfig.user_id == target_user_id).delete(synchronize_session=False)
    db.query(SystemLog).filter(SystemLog.user_id == target_user_id).delete(synchronize_session=False)

    # Note: Trade records retain user_id set to NULL or cascade delete per regulatory preference
    db.query(Trade).filter(Trade.user_id == target_user_id).update({"user_id": None}, synchronize_session=False)

    db.delete(user)
    db.commit()

    log_system_event(
        db, admin_user_id, "WARNING", "USER_DELETED",
        f"Super Admin deleted user #{target_user_id} ({uname})"
    )
    return True


def update_user_trading_config_and_permissions(
    db: Session,
    target_user_id: int,
    config_data: Dict[str, Any],
    permissions_data: Optional[Dict[str, Any]] = None,
    admin_user_id: Optional[int] = None
) -> Dict[str, Any]:
    """Updates user trading risk parameters and feature access permissions."""
    user = db.query(User).filter(User.id == target_user_id).first()
    if not user:
        raise ValueError("User not found.")

    cfg = user.config
    if not cfg:
        cfg = UserConfig(user_id=user.id)
        db.add(cfg)

    # Update trading parameters
    if "max_trades_per_day" in config_data:
        cfg.max_trades_per_day = int(config_data["max_trades_per_day"])
    elif "max_trades" in config_data and config_data["max_trades"] is not None:
        cfg.max_trades_per_day = int(config_data["max_trades"])

    if "max_open_trades" in config_data:
        cfg.max_open_trades = int(config_data["max_open_trades"])

    if "daily_loss_limit" in config_data:
        cfg.daily_loss_limit = float(config_data["daily_loss_limit"])
    elif "daily_loss_cap" in config_data and config_data["daily_loss_cap"] is not None:
        cfg.daily_loss_limit = float(config_data["daily_loss_cap"])

    if "trailing_sl_activation" in config_data:
        cfg.trailing_sl_activation = float(config_data["trailing_sl_activation"])
    if "volume_spike_ratio" in config_data:
        cfg.volume_spike_ratio = float(config_data["volume_spike_ratio"])
    if "min_candle_strength" in config_data:
        cfg.min_candle_strength = float(config_data["min_candle_strength"])
    if "min_stock_price" in config_data:
        cfg.min_stock_price = float(config_data["min_stock_price"])
    if "max_stock_price" in config_data:
        cfg.max_stock_price = float(config_data["max_stock_price"])

    if "manual_capital" in config_data:
        cfg.manual_capital = float(config_data["manual_capital"])
    elif "capital" in config_data and config_data["capital"] is not None:
        cfg.manual_capital = float(config_data["capital"])

    if "trading_mode" in config_data and config_data["trading_mode"]:
        mode = str(config_data["trading_mode"]).upper()
        if mode in ("TEST", "LIVE"):
            cfg.trading_mode = mode
    if "is_running" in config_data:
        cfg.is_running = 1 if config_data["is_running"] else 0
    if "hybrid_virtual_mode" in config_data:
        cfg.hybrid_virtual_mode = 1 if config_data["hybrid_virtual_mode"] else 0

    cfg.updated_at = get_ist_now()

    # Update granular permissions
    if permissions_data is not None:
        user.permissions_json = json.dumps(permissions_data)

    db.commit()

    log_system_event(
        db, admin_user_id, "INFO", "CONFIG_UPDATED",
        f"Super Admin updated trading configuration & permissions for user #{user.id} ({user.username})"
    )

    return {
        "status": "success",
        "user_id": user.id,
        "config": {
            "trading_mode": cfg.trading_mode,
            "is_running": bool(cfg.is_running),
            "manual_capital": float(cfg.manual_capital or 0.0),
            "daily_loss_limit": float(cfg.daily_loss_limit or 0.0),
            "max_trades_per_day": cfg.max_trades_per_day,
            "max_open_trades": cfg.max_open_trades
        },
        "permissions": get_user_effective_permissions(user)
    }


def get_system_audit_logs(db: Session, limit: int = 100) -> List[Dict[str, Any]]:
    """Retrieves system and user audit log stream."""
    logs = (
        db.query(SystemLog, User.username)
        .outerjoin(User, SystemLog.user_id == User.id)
        .order_by(SystemLog.timestamp.desc())
        .limit(min(limit, 300))
        .all()
    )

    out = []
    for l, uname in logs:
        out.append({
            "id": l.id,
            "user_id": l.user_id,
            "username": uname or "SYSTEM",
            "timestamp": l.timestamp.strftime("%Y-%m-%d %H:%M:%S") if l.timestamp else "--",
            "level": l.log_level,
            "event": l.event_type,
            "message": l.message
        })
    return out


def get_system_overview_stats(db: Session) -> Dict[str, Any]:
    """Generates real-time statistics for the Super Admin overview banner."""
    total_users = db.query(func.count(User.id)).scalar() or 0
    active_users = db.query(func.count(User.id)).filter(User.is_active == True).scalar() or 0
    super_admins = db.query(func.count(User.id)).filter(User.role == "super_admin").scalar() or 0
    master_admins = db.query(func.count(User.id)).filter(User.role.in_(["master_admin", "admin"])).scalar() or 0
    regular_users = db.query(func.count(User.id)).filter(User.role == "user").scalar() or 0

    running_bots = db.query(func.count(UserConfig.id)).filter(UserConfig.is_running == 1).scalar() or 0
    live_bots = db.query(func.count(UserConfig.id)).filter(UserConfig.is_running == 1, UserConfig.trading_mode == "LIVE").scalar() or 0

    today = get_ist_date()
    today_trades = db.query(Trade).filter(Trade.trade_date == today).all()
    today_trades_count = len(today_trades)
    today_net_pnl = sum(t.realized_pnl for t in today_trades if t.realized_pnl is not None)

    return {
        "total_users": total_users,
        "active_users": active_users,
        "inactive_users": total_users - active_users,
        "super_admins": super_admins,
        "master_admins": master_admins,
        "regular_users": regular_users,
        "running_bots": running_bots,
        "live_trading_bots": live_bots,
        "trades_today_count": today_trades_count,
        "trades_today_pnl": round(today_net_pnl, 2),
        "db_status": "Connected (Healthy)"
    }


class SuperAdminService:
    """Service wrapper for Super Admin centralized controllers."""
    get_system_overview_stats = staticmethod(get_system_overview_stats)
    get_all_users_summary = staticmethod(get_all_users_summary)
    get_user_full_details = staticmethod(get_user_full_details)
    create_user_admin = staticmethod(create_user_admin)
    update_user_admin = staticmethod(update_user_admin)
    delete_user_admin = staticmethod(delete_user_admin)
    toggle_user_active_status = staticmethod(toggle_user_active_status)
    update_user_trading_config_and_permissions = staticmethod(update_user_trading_config_and_permissions)
    get_system_audit_logs = staticmethod(get_system_audit_logs)


import os
import pymysql
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Boolean,
    DateTime, Date, Text, ForeignKey, UniqueConstraint, Enum as SAEnum
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from datetime import datetime
import pytz
from config import Config

IST = pytz.timezone('Asia/Kolkata')

def get_ist_now():
    return datetime.now(IST)

def get_ist_date():
    return datetime.now(IST).date()

def get_database_engine():
    """Connects to Supabase / PostgreSQL, MySQL, or falls back gracefully to SQLite."""
    # 1. Supabase / PostgreSQL Cloud Database
    supabase_url = os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL") or getattr(Config, "SUPABASE_DB_URL", "")
    if supabase_url:
        try:
            pg_url = supabase_url
            if pg_url.startswith("postgres://"):
                pg_url = pg_url.replace("postgres://", "postgresql+psycopg2://", 1)
            elif pg_url.startswith("postgresql://") and not pg_url.startswith("postgresql+psycopg2://"):
                pg_url = pg_url.replace("postgresql://", "postgresql+psycopg2://", 1)
            eng = create_engine(pg_url, echo=False, pool_size=10, max_overflow=5, pool_recycle=1800, pool_pre_ping=True)
            with eng.connect() as test_conn:
                pass
            print("SUCCESS: Connected to Supabase / PostgreSQL Cloud Database.")
            return eng
        except Exception as e:
            print(f"WARNING: Supabase connection failed ({e}). Checking alternatives...")

    # 2. MySQL fallback
    use_sqlite = getattr(Config, 'DB_DIALECT', 'auto') == 'sqlite'
    if not use_sqlite and Config.DB_HOST:
        try:
            connection = pymysql.connect(
                host=Config.DB_HOST,
                user=Config.DB_USER,
                password=Config.DB_PASSWORD,
                port=int(Config.DB_PORT),
                connect_timeout=2
            )
            cursor = connection.cursor()
            cursor.execute(f"CREATE DATABASE IF NOT EXISTS {Config.DB_NAME}")
            connection.commit()
            cursor.close()
            connection.close()
            mysql_url = f"mysql+pymysql://{Config.DB_USER}:{Config.DB_PASSWORD}@{Config.DB_HOST}:{Config.DB_PORT}/{Config.DB_NAME}"
            eng = create_engine(mysql_url, echo=False, pool_recycle=3600)
            with eng.connect() as test_conn:
                pass
            print(f"SUCCESS: MySQL Database '{Config.DB_NAME}' connected.")
            return eng
        except Exception as e:
            print(f"INFO: MySQL not reachable ({e}). Falling back to SQLite local database.")
    
    # 3. SQLite fallback
    sqlite_url = "sqlite:///trade_history.db"
    return create_engine(sqlite_url, echo=False, connect_args={"check_same_thread": False})

engine = get_database_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

# ─────────────────────────────────────────────────────────────────────────────
# AUTH / USER TABLES (new)
# ─────────────────────────────────────────────────────────────────────────────

class User(Base):
    """Stores user accounts. Passwords are bcrypt-hashed. Never store plain text."""
    __tablename__ = "users"

    id            = Column(Integer, primary_key=True, index=True, autoincrement=True)
    email         = Column(String(255), unique=True, index=True, nullable=False)
    username      = Column(String(100), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role          = Column(String(20), default="user")  # "super_admin", "master_admin", "user"
    is_active     = Column(Boolean, default=True)
    last_login    = Column(DateTime, nullable=True)
    permissions_json = Column(Text, nullable=True)     # JSON string of granular feature permissions
    created_at    = Column(DateTime, default=get_ist_now)

    # Relationships
    credentials   = relationship("UserCredential", back_populates="user", uselist=False)
    config        = relationship("UserConfig", back_populates="user", uselist=False)
    trades        = relationship("Trade", back_populates="user")


class UserCredential(Base):
    """
    Stores AES-256-GCM encrypted Upstox credentials per user.
    Fields are WRITE-ONLY — they are NEVER returned to the frontend.
    """
    __tablename__ = "user_credentials"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_user_credentials_user_id"),
    )

    id                     = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id                = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True)
    upstox_client_id       = Column(Text, nullable=True)   # AES-GCM encrypted
    upstox_client_secret   = Column(Text, nullable=True)   # AES-GCM encrypted
    upstox_access_token    = Column(Text, nullable=True)   # AES-GCM encrypted
    analytics_token        = Column(Text, nullable=True)   # AES-GCM encrypted
    created_at             = Column(DateTime, default=get_ist_now)
    updated_at             = Column(DateTime, default=get_ist_now, onupdate=get_ist_now)

    user = relationship("User", back_populates="credentials")


class UserConfig(Base):
    """Stores per-user trading strategy settings."""
    __tablename__ = "user_config"

    id                     = Column(Integer, primary_key=True, autoincrement=True)
    user_id                = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True)
    max_trades_per_day     = Column(Integer, default=5)
    max_open_trades        = Column(Integer, default=2)
    daily_loss_limit       = Column(Float, default=2000.0)
    trailing_sl_activation = Column(Float, default=1.0)
    volume_spike_ratio     = Column(Float, default=1.8)
    min_candle_strength    = Column(Float, default=0.6)
    min_stock_price        = Column(Float, default=50.0)
    max_stock_price        = Column(Float, default=5000.0)
    use_claude_filter      = Column("use_groq_filter", Integer, default=1)  # 1=True, 0=False
    use_live_capital       = Column(Integer, default=0)
    manual_capital         = Column(Float, default=10000.0)
    hybrid_virtual_mode    = Column(Integer, default=1)  # 1=True, 0=False
    trading_mode           = Column(String(20), default="TEST")  # "TEST" or "LIVE"
    is_running             = Column(Integer, default=0)          # 1=Running, 0=Stopped
    updated_at             = Column(DateTime, default=get_ist_now, onupdate=get_ist_now)

    user = relationship("User", back_populates="config")


# ─────────────────────────────────────────────────────────────────────────────
# TRADING TABLES (extended with user_id and v2 columns)
# ─────────────────────────────────────────────────────────────────────────────

class Trade(Base):
    __tablename__ = "trades"

    id           = Column(String(50), primary_key=True, index=True)
    user_id      = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)  # nullable for legacy rows
    trade_date   = Column(Date, default=get_ist_date, index=True)
    symbol       = Column(String(50), index=True)
    side         = Column(String(10))
    quantity     = Column(Integer)

    entry_price  = Column(Float)
    sl_price     = Column(Float)
    target_price = Column(Float)
    entry_time   = Column(DateTime, default=get_ist_now)

    exit_price   = Column(Float, nullable=True)
    exit_time    = Column(DateTime, nullable=True)
    exit_reason  = Column(String(50), nullable=True)

    realized_pnl = Column(Float, default=0.0)
    status       = Column(String(20), default="OPEN", index=True)
    ai_score     = Column(Float, nullable=True)
    sl_order_id  = Column(String(100), nullable=True)

    # v2 columns (Section 11)
    confidence           = Column(Integer, nullable=True)
    reasoning            = Column(Text, nullable=True)
    layer3_conditions_met = Column(Integer, nullable=True)
    paper_trade          = Column(Boolean, default=False)

    # Strategy analytics columns (added 2026-07-05)
    strategy_type     = Column(String(20), nullable=True)   # "MOMENTUM" or "ORB"
    rr_ratio          = Column(Float, nullable=True)         # reward-to-risk at entry
    timeframe_aligned = Column(Boolean, nullable=True)       # 15-min MTF aligned?
    sector_aligned    = Column(Boolean, nullable=True)       # sector moving with trade?
    vix_at_entry      = Column(Float, nullable=True)         # India VIX at time of entry
    time_of_day_bucket = Column(String(20), nullable=True)  # MORNING / MIDDAY / AFTERNOON
    market_regime     = Column(String(20), nullable=True)   # TRENDING_UP / RANGING etc
    would_have_entered_without_claude = Column(Boolean, default=False)
    
    # Execution Tracking (v4)
    mfe = Column(Float, nullable=True) # Maximum Favorable Excursion
    mae = Column(Float, nullable=True) # Maximum Adverse Excursion
    
    # Discovery Tracking (v4.1)
    discovery_score = Column(Float, nullable=True)
    discovery_rank  = Column(Integer, nullable=True)

    user = relationship("User", back_populates="trades")


class DailyStats(Base):
    __tablename__ = "daily_sessions"

    id                = Column(Integer, primary_key=True, autoincrement=True)
    user_id           = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)  # nullable for legacy rows
    session_date      = Column(Date, default=get_ist_date, index=True)
    start_capital     = Column(Float)
    end_capital       = Column(Float)
    total_trades_taken= Column(Integer, default=0)
    winning_trades    = Column(Integer, default=0)
    net_realized_pnl  = Column(Float, default=0.0)
    max_drawdown      = Column(Float, default=0.0)

    __table_args__ = (
        UniqueConstraint("user_id", "session_date", name="uq_user_session_date"),
    )


class SystemLog(Base):
    __tablename__ = "system_logs"

    id            = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id       = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)  # nullable for global system logs
    timestamp     = Column(DateTime, default=get_ist_now, index=True)
    log_level     = Column(String(20))
    event_type    = Column(String(50))
    message       = Column(String(255))
    metadata_json = Column(String(1000), nullable=True)


# ─────────────────────────────────────────────────────────────────────────────
# v2 PIPELINE TABLES (Section 11)
# ─────────────────────────────────────────────────────────────────────────────

class MorningWatchlist(Base):
    """Layer 1 output — stocks passing the morning pre-filter."""
    __tablename__ = "morning_watchlist"
    __table_args__ = (
        UniqueConstraint("date", "symbol", name="uq_morning_watchlist"),
    )

    id                     = Column(Integer, primary_key=True, autoincrement=True)
    date                   = Column(Date, nullable=False, index=True)
    symbol                 = Column(String(20), nullable=False)
    instrument_key         = Column(String(50), nullable=True)
    direction_bias         = Column(String(20), nullable=False)  # LONG_CANDIDATE / SHORT_CANDIDATE
    vol_ratio_20day        = Column(Float, nullable=True)
    relative_strength_5day = Column(Float, nullable=True)
    price_vs_ema20_daily   = Column(Float, nullable=True)
    price_vs_20day_high_pct = Column(Float, nullable=True)
    discovery_score        = Column(Float, nullable=True)
    discovery_rank         = Column(Integer, nullable=True)
    created_at             = Column(DateTime, default=get_ist_now)


class ActiveWatchlist(Base):
    """Layer 2 output — currently active stocks for scanning."""
    __tablename__ = "active_watchlist"
    __table_args__ = (
        UniqueConstraint("date", "symbol", name="uq_active_watchlist"),
    )

    id               = Column(Integer, primary_key=True, autoincrement=True)
    date             = Column(Date, nullable=False, index=True)
    symbol           = Column(String(20), nullable=False)
    instrument_key   = Column(String(50), nullable=True)
    direction_bias   = Column(String(20), nullable=False)  # LONG_CANDIDATE / SHORT_CANDIDATE
    added_at         = Column(DateTime, nullable=False, default=get_ist_now)
    removed_at       = Column(DateTime, nullable=True)
    removal_reason   = Column(String(100), nullable=True)


class DailyBlacklist(Base):
    """Stocks that have been traded today — one trade per stock per day per user."""
    __tablename__ = "daily_blacklist"
    __table_args__ = (
        UniqueConstraint("user_id", "date", "symbol", name="uq_daily_blacklist"),
    )

    id               = Column(Integer, primary_key=True, autoincrement=True)
    user_id          = Column(Integer, ForeignKey("users.id"), nullable=False)
    date             = Column(Date, nullable=False, index=True)
    symbol           = Column(String(20), nullable=False)
    blacklisted_at   = Column(DateTime, default=get_ist_now)


class SystemConfigDB(Base):
    """Global system configuration (kill switch, feature flags, etc.)."""
    __tablename__ = "system_config"

    config_key   = Column(String(100), primary_key=True)
    config_value = Column(String(255), nullable=False)
    updated_at   = Column(DateTime, default=get_ist_now, onupdate=get_ist_now)


class DailyReconciliation(Base):
    """End-of-day reconciliation results."""
    __tablename__ = "daily_reconciliation"

    id                = Column(Integer, primary_key=True, autoincrement=True)
    date              = Column(Date, nullable=False, index=True)
    total_trades      = Column(Integer, default=0)
    mismatches        = Column(Integer, default=0)
    ghost_trades      = Column(Integer, default=0)
    untracked_trades  = Column(Integer, default=0)
    details           = Column(Text, nullable=True)
    created_at        = Column(DateTime, default=get_ist_now)


class MasterLinkedAccount(Base):
    """
    Stores accounts linked by Master Admin for multi-account trade replication.
    """
    __tablename__ = "master_linked_accounts"
    __table_args__ = (
        UniqueConstraint("master_user_id", "linked_user_id", name="uq_master_linked_account"),
    )

    id             = Column(Integer, primary_key=True, autoincrement=True)
    master_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    linked_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    is_active      = Column(Integer, default=1)  # 1 = Enabled in multi-trading, 0 = Paused/Excluded
    allocation_pct = Column(Float, default=1.0)  # Quantity multiplier (default 1.0 = exact same qty)
    created_at     = Column(DateTime, default=get_ist_now)
    updated_at     = Column(DateTime, default=get_ist_now, onupdate=get_ist_now)

    # Relationships
    master_user = relationship("User", foreign_keys=[master_user_id])
    linked_user = relationship("User", foreign_keys=[linked_user_id])



# ─────────────────────────────────────────────────────────────────────────────
# HELPER FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────

def log_system_event(db, user_id, level, event, message, metadata=None):
    import json
    try:
        new_log = SystemLog(
            user_id=user_id,
            log_level=level,
            event_type=event,
            message=message[:250],
            metadata_json=json.dumps(metadata) if metadata else None
        )
        db.add(new_log)
        db.commit()
    except Exception as e:
        db.rollback()  # Prevent session corruption from propagating to caller
        print(f"FAILED to log system event: {e}")


# ─── System Config Helpers (Bug Fix 7 — Global Kill Switch) ─────────────────

def get_system_config(key: str, default: str = None) -> str:
    """Read a value from system_config table."""
    db = SessionLocal()
    try:
        row = db.query(SystemConfigDB).filter(SystemConfigDB.config_key == key).first()
        return row.config_value if row else default
    except Exception as e:
        print(f"Error reading system_config '{key}': {e}")
        return default
    finally:
        db.close()


def set_system_config(key: str, value: str):
    """Write a value to system_config table (upsert)."""
    db = SessionLocal()
    try:
        row = db.query(SystemConfigDB).filter(SystemConfigDB.config_key == key).first()
        if row:
            row.config_value = value
            row.updated_at = get_ist_now()
        else:
            row = SystemConfigDB(config_key=key, config_value=value)
            db.add(row)
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"Error writing system_config '{key}': {e}")
    finally:
        db.close()


def get_global_trading_enabled() -> bool:
    """Check if global trading is enabled (Bug Fix 7)."""
    val = get_system_config("GLOBAL_TRADING_ENABLED", "true")
    return val.lower() == "true"


def set_global_trading_enabled(enabled: bool):
    """Set global trading enabled/disabled (Bug Fix 7)."""
    set_system_config("GLOBAL_TRADING_ENABLED", "true" if enabled else "false")


# ─── Daily Blacklist Helpers ────────────────────────────────────────────────

def is_symbol_blacklisted(user_id: int, symbol: str) -> bool:
    """Check if a symbol has already been traded today by this user."""
    db = SessionLocal()
    try:
        today = get_ist_date()
        row = db.query(DailyBlacklist).filter(
            DailyBlacklist.user_id == user_id,
            DailyBlacklist.date == today,
            DailyBlacklist.symbol == symbol
        ).first()
        return row is not None
    finally:
        db.close()


def add_to_daily_blacklist(user_id: int, symbol: str):
    """Add a symbol to the daily blacklist after first trade."""
    db = SessionLocal()
    try:
        today = get_ist_date()
        existing = db.query(DailyBlacklist).filter(
            DailyBlacklist.user_id == user_id,
            DailyBlacklist.date == today,
            DailyBlacklist.symbol == symbol
        ).first()
        if not existing:
            db.add(DailyBlacklist(user_id=user_id, date=today, symbol=symbol))
            db.commit()
    except Exception as e:
        db.rollback()
        print(f"Error adding to daily blacklist: {e}")
    finally:
        db.close()


def get_daily_blacklist(user_id: int) -> list:
    """Get all blacklisted symbols for this user today."""
    db = SessionLocal()
    try:
        today = get_ist_date()
        rows = db.query(DailyBlacklist).filter(
            DailyBlacklist.user_id == user_id,
            DailyBlacklist.date == today
        ).all()
        return [r.symbol for r in rows]
    finally:
        db.close()


def get_trades_count_today(user_id: int) -> int:
    """Bug Fix 5: Query DB for accurate trade count today for a user."""
    db = SessionLocal()
    try:
        today = get_ist_date()
        count = db.query(Trade).filter(
            Trade.user_id == user_id,
            Trade.trade_date == today
        ).count()
        return count
    finally:
        db.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    try:
        Base.metadata.create_all(bind=engine)
        print(f"SUCCESS: Executed Table Migrations on database '{engine.url}'")
        # Safe raw ALTER to add columns if they don't exist (MySQL only)
        if engine.dialect.name == "mysql":
            from sqlalchemy import text
            with engine.connect() as conn:
                # v1 column: sl_order_id
                result = conn.execute(text("SHOW COLUMNS FROM trades LIKE 'sl_order_id'")).fetchone()
            if not result:
                conn.execute(text("ALTER TABLE trades ADD COLUMN sl_order_id VARCHAR(100) NULL"))
                print("SUCCESS: Added sl_order_id column to trades table")
            
            # v2 columns
            for col_name, col_def in [
                ("confidence", "INT NULL"),
                ("reasoning", "TEXT NULL"),
                ("layer3_conditions_met", "INT NULL"),
                ("paper_trade", "BOOLEAN DEFAULT FALSE"),
            ]:
                result = conn.execute(text(f"SHOW COLUMNS FROM trades LIKE '{col_name}'")).fetchone()
                if not result:
                    conn.execute(text(f"ALTER TABLE trades ADD COLUMN {col_name} {col_def}"))
                    print(f"SUCCESS: Added {col_name} column to trades table")
            # user_config columns
            result = conn.execute(text("SHOW COLUMNS FROM user_config LIKE 'is_running'")).fetchone()
            if not result:
                conn.execute(text("ALTER TABLE user_config ADD COLUMN is_running INT DEFAULT 0"))
                print("SUCCESS: Added is_running column to user_config table")

            result = conn.execute(text("SHOW COLUMNS FROM user_config LIKE 'max_open_trades'")).fetchone()
            if not result:
                conn.execute(text("ALTER TABLE user_config ADD COLUMN max_open_trades INT DEFAULT 2"))
                print("SUCCESS: Added max_open_trades column to user_config table")

            # Strategy analytics columns in trades
            for col_name, col_def in [
                ("strategy_type",     "VARCHAR(20) NULL"),
                ("rr_ratio",          "FLOAT NULL"),
                ("timeframe_aligned", "BOOLEAN NULL"),
                ("sector_aligned",    "BOOLEAN NULL"),
                ("vix_at_entry",      "FLOAT NULL"),
                ("time_of_day_bucket","VARCHAR(20) NULL"),
                ("market_regime",     "VARCHAR(20) NULL"),
            ]:
                result = conn.execute(text(f"SHOW COLUMNS FROM trades LIKE '{col_name}'")).fetchone()
                if not result:
                    conn.execute(text(f"ALTER TABLE trades ADD COLUMN {col_name} {col_def}"))
                    print(f"SUCCESS: Added {col_name} column to trades table")

            # Validate Claude column
            result = conn.execute(text("SHOW COLUMNS FROM trades LIKE 'would_have_entered_without_claude'")).fetchone()
            if not result:
                conn.execute(text("ALTER TABLE trades ADD COLUMN would_have_entered_without_claude BOOLEAN DEFAULT FALSE"))
                print("SUCCESS: Added would_have_entered_without_claude column to trades table")

            conn.commit()
        
        # Ensure GLOBAL_TRADING_ENABLED default exists
        set_system_config("GLOBAL_TRADING_ENABLED", 
                          get_system_config("GLOBAL_TRADING_ENABLED", "true"))
        
    except Exception as e:
        print(f"FAILED: to create tables: {e}")


if __name__ == "__main__":
    init_db()


import os
import pymysql
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Date
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime
import pytz
from config import Config

IST = pytz.timezone('Asia/Kolkata')

def get_ist_now():
    return datetime.now(IST).replace(tzinfo=False)

def get_ist_date():
    return datetime.now(IST).date()

def create_database_if_not_exists():
    """Connects to MySQL server and creates the database if it doesn't exist."""
    try:
        # Connect to MySQL server without specifying a database
        connection = pymysql.connect(
            host=Config.DB_HOST,
            user=Config.DB_USER,
            password=Config.DB_PASSWORD,
            port=int(Config.DB_PORT)
        )
        cursor = connection.cursor()
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS {Config.DB_NAME}")
        connection.commit()
        cursor.close()
        connection.close()
        print(f"SUCCESS: MySQL Database '{Config.DB_NAME}' ensured.")
    except Exception as e:
        print(f"FAILED: to connect to MySQL or create database: {e}")

# Ensure database exists before creating SQLAlchemy engine
create_database_if_not_exists()

# Setup SQLAlchemy
DB_URL = f"mysql+pymysql://{Config.DB_USER}:{Config.DB_PASSWORD}@{Config.DB_HOST}:{Config.DB_PORT}/{Config.DB_NAME}"
engine = create_engine(DB_URL, echo=False, pool_recycle=3600)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

class Trade(Base):
    __tablename__ = "trades"
    
    id = Column(String(50), primary_key=True, index=True)
    trade_date = Column(Date, default=get_ist_date, index=True)
    symbol = Column(String(50), index=True)
    side = Column(String(10))
    quantity = Column(Integer)
    
    entry_price = Column(Float)
    sl_price = Column(Float)
    target_price = Column(Float)
    entry_time = Column(DateTime, default=get_ist_now)
    
    exit_price = Column(Float, nullable=True)
    exit_time = Column(DateTime, nullable=True)
    exit_reason = Column(String(50), nullable=True)
    
    realized_pnl = Column(Float, default=0.0)
    status = Column(String(20), default="OPEN", index=True)
    ai_score = Column(Float, nullable=True)

class DailyStats(Base):
    __tablename__ = "daily_sessions"
    
    session_date = Column(Date, primary_key=True, index=True)
    start_capital = Column(Float)
    end_capital = Column(Float)
    total_trades_taken = Column(Integer, default=0)
    winning_trades = Column(Integer, default=0)
    net_realized_pnl = Column(Float, default=0.0)
    max_drawdown = Column(Float, default=0.0)

class SystemLog(Base):
    __tablename__ = "system_logs"
    
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    timestamp = Column(DateTime, default=get_ist_now, index=True)
    log_level = Column(String(20))
    event_type = Column(String(50))
    message = Column(String(255))
    metadata_json = Column(String(1000), nullable=True)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db():
    try:
        Base.metadata.create_all(bind=engine)
        print(f"SUCCESS: Executed MySQL Table Migrations for '{Config.DB_NAME}'")
    except Exception as e:
        print(f"FAILED: to create tables: {e}")

if __name__ == "__main__":
    init_db()

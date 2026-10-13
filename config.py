from pydantic import BaseModel
from typing import List, Optional
import os
import json

# Native .env loader to securely pull keys without external packages
env_file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(env_file_path):
    with open(env_file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip()
                # Handle quoted values
                if len(v) >= 2 and ((v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'"))):
                    v = v[1:-1]
                os.environ[k] = v # Use environ[] to allow overrides from .env

class Config:
    # Execution Mode: "TEST" or "LIVE" — Default is strictly "TEST" for safety
    MODE = os.getenv("MODE", "TEST")

    # Database Configuration
    DB_USER = os.getenv("DB_USER", "app_user")
    DB_PASSWORD = os.getenv("DB_PASSWORD", "")
    DB_HOST = os.getenv("DB_HOST", "localhost")
    DB_PORT = os.getenv("DB_PORT", "3306")
    DB_NAME = os.getenv("DB_NAME", "trade_history")
    DB_DIALECT = os.getenv("DB_DIALECT", "auto")  # "auto", "mysql", "sqlite"

    # Upstox API Credentials
    UPSTOX_CLIENT_ID = os.getenv("UPSTOX_CLIENT_ID", "6528658a-2ce9-44bc-8c70-1100c3b54651")
    UPSTOX_CLIENT_SECRET = os.getenv("UPSTOX_CLIENT_SECRET", "2yoyfwa9e7")
    UPSTOX_TOTP_SECRET = os.getenv("UPSTOX_TOTP_SECRET", "")
    UPSTOX_USER_PIN = os.getenv("UPSTOX_USER_PIN", "")
    UPSTOX_ACCESS_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "")
    ANALYTICS_TOKEN = os.getenv("ANALYTICS_TOKEN", "")
    REDIRECT_URI = os.getenv("REDIRECT_URI", "https://bot.deepcodev.site/callback")

    MAX_TRADES_PER_DAY = 5
    MAX_DAILY_LOSS_PCT = -0.02  # (Legacy fallback)
    DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", "1000.0"))  # Max loss in ₹ before kill-switch
    TRAILING_SL_ACTIVATION = 1.0  # R-Multiple to start trailing
    MAX_OPEN_TRADES = 2
    MAX_POSITION_PCT = 0.10  # Maximum 10% of capital allocated to any single trade during pilot
    CAPITAL = float(os.getenv("INITIAL_CAPITAL", "25000.0"))  # Configurable virtual balance (₹25,000)
    USE_LIVE_CAPITAL = os.getenv("USE_LIVE_CAPITAL", "false").lower() == "true"
    MANUAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", "25000.0"))
    
    # AI/ML Configuration
    GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
    USE_CLAUDE_FILTER = True
    SCORE_THRESHOLD = 4
    SCORE_THRESHOLD_MIN = 3 # Minimum score for a trade
    PROBABILITY_THRESHOLD = 0.65  # Ignored when GROQ is active
    
    # Advanced Filtering Toggles
    USE_VOLUME_FILTER = True
    USE_MOMENTUM_FILTER = True
    USE_INDEX_ALIGNMENT = True
    USE_SYMBOL_COOLDOWN = True
    
    # Filtering Thresholds
    VOLUME_SPIKE_RATIO = 2.0  # (User requested >= 2x)
    MAX_PRE_ENTRY_MOVE_PCT = 0.02  # 2% max move before entry
    MAX_WICK_PCT = 0.35  # Max wick rejection %
    SYMBOLS_COOLDOWN_MINUTES = 120 # 2 hours after SL
    CONSECUTIVE_LOSS_COOLDOWN_MINUTES = 60 # 1 hour after 2 losses
    
    # Adaptive Trailing Settings
    TRAILING_STYLE = "ADAPTIVE" # "FIXED" or "ADAPTIVE"
    TRAILING_ATR_MULT_STRONG = 2.0  # Loose trail for strong trend
    TRAILING_ATR_MULT_WEAK = 1.0    # Tight trail for weak trend
    
    # Strategy configs
    BREAKOUT_PERIOD = 15  # For max/min (10-15 candles)
    VOLUME_AVG_PERIOD = 20
    TARGET_R_MIN = 1.5
    TARGET_R_MAX = 2.0
    VOLUME_SPIKE_RATIO = 1.8
    MIN_CANDLE_STRENGTH = 0.6
    
    # Universe Filter
    MIN_STOCK_PRICE = 50.0
    MAX_STOCK_PRICE = 5000.0

    # ── Paper Trading Mode (Default True for safety) ──────────────────────────
    PAPER_TRADING_MODE = os.getenv("PAPER_TRADING_MODE", "true").lower() == "true"

    # ── v2 Pipeline & Risk Controls (Section 6) ──────────────────────────────
    MAX_CONSECUTIVE_LOSSES   = 2       # stops new entries after 2 consecutive losses
    NO_NEW_ENTRIES_AFTER     = (14, 30)  # 2:30 PM IST — tuple (hour, minute)
    FORCE_EXIT_ALL_AT        = (15, 15)  # 3:15 PM IST
    WEBSOCKET_DISCONNECT_AT  = (15, 25)  # 3:25 PM IST
    MAX_VIX_FOR_TRADING      = 25.0    # above this: no new entries at all
    CAUTIOUS_VIX_THRESHOLD   = 18.0    # above this: Claude requires confidence >= 4
    MAX_PRICE_SLIPPAGE_PCT   = 1.0     # skip entry if LTP moved >1.0% from Claude's entry
    MAX_MOMENTUM_SLIPPAGE_PCT = 0.3    # 0.3% max slippage for Momentum entries
    MAX_ORB_SLIPPAGE_PCT      = 0.5    # 0.5% max slippage for ORB entries
    LAYER4_MAX_STOCKS_PER_CYCLE = 5    # maximum stocks sent to Claude per cycle
    CANDLE_CACHE_REFRESH_MIN = 10      # refresh rolling candle cache every 10 minutes
    MARKET_DATA_REFRESH_MIN  = 15      # refresh Nifty trend and VIX every 15 minutes
    SECTOR_DATA_REFRESH_MIN  = 30      # refresh sector performance every 30 minutes

    # ── AI / LLM Configuration ────────────────────────────────────────────────
    # Primary Evaluator: Claude (Anthropic)
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
    ANTHROPIC_MODEL   = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")
    ANTHROPIC_MAX_TOKENS = 4000
    ANTHROPIC_TEMPERATURE = 0.0

    # Secondary Evaluator: Optional Second AI Model (Groq / Gemini / OpenRouter / Ollama / OpenAI)
    OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY", "")
    GROQ_API_KEY       = os.getenv("GROQ_API_KEY", "")
    GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY", "")
    OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

    SECOND_AI_PROVIDER = os.getenv("SECOND_AI_PROVIDER", "groq" if os.getenv("GROQ_API_KEY") else ("gemini" if os.getenv("GEMINI_API_KEY") else ("openrouter" if os.getenv("OPENROUTER_API_KEY") else ("openai" if os.getenv("OPENAI_API_KEY") else "local_rule"))))
    SECOND_AI_MODEL    = os.getenv("SECOND_AI_MODEL", "")
    
    # Auto-resolve API key for active provider if not explicitly specified
    _prov = SECOND_AI_PROVIDER.lower() if SECOND_AI_PROVIDER else ""
    if _prov == "groq":
        _default_key = GROQ_API_KEY
    elif _prov == "gemini":
        _default_key = GEMINI_API_KEY
    elif _prov == "openrouter":
        _default_key = OPENROUTER_API_KEY
    elif _prov == "openai":
        _default_key = OPENAI_API_KEY
    else:
        _default_key = ""
    SECOND_AI_API_KEY  = os.getenv("SECOND_AI_API_KEY", _default_key)
    
    # Active Evaluator Selector: "claude", "second_ai", "consensus", "local_rule"
    ACTIVE_AI_EVALUATOR = os.getenv("ACTIVE_AI_EVALUATOR", "second_ai" if SECOND_AI_API_KEY or _prov == "ollama" else ("claude" if os.getenv("ANTHROPIC_API_KEY") else "local_rule"))

    # ── Telegram Alerting ─────────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

    # Symbols mapping (User friendly name -> Upstox Format)

    SYMBOLS_MAPPING = {
        "RELIANCE": "NSE_EQ|INE002A01018",
        "HDFCBANK": "NSE_EQ|INE040A01034",
        "ICICIBANK": "NSE_EQ|INE090A01021",
        "INFY": "NSE_EQ|INE009A01021",
        "TCS": "NSE_EQ|INE467B01029",
        "BHARTIARTL": "NSE_EQ|INE397D01024",
        "ITC": "NSE_EQ|INE154A01025",
        "SBIN": "NSE_EQ|INE062A01020",
        "LICI": "NSE_EQ|INE0J1Y01017",
        "HINDUNILVR": "NSE_EQ|INE030A01027",
        "LT": "NSE_EQ|INE018A01030",
        "BAJFINANCE": "NSE_EQ|INE296A01032",
        "AXISBANK": "NSE_EQ|INE238A01034",
        "HCLTECH": "NSE_EQ|INE860A01027",
        "SUNPHARMA": "NSE_EQ|INE044A01036",
        "ADANIENT": "NSE_EQ|INE423A01024",
        "MARUTI": "NSE_EQ|INE585B01010",
        "TATASTEEL": "NSE_EQ|INE081A01020",
        "NTPC": "NSE_EQ|INE733E01010",
        "KOTAKBANK": "NSE_EQ|INE237A01036",
        "TITAN": "NSE_EQ|INE280A01028",
        "JSWSTEEL": "NSE_EQ|INE019A01038",
        "ONGC": "NSE_EQ|INE213A01029",
        "ASIANPAINT": "NSE_EQ|INE021A01026",
        "POWERGRID": "NSE_EQ|INE752E01010",
        "M&M": "NSE_EQ|INE101A01026",
        "ADANIPORTS": "NSE_EQ|INE742F01042",
        "WIPRO": "NSE_EQ|INE075A01022",
        "ULTRACEMCO": "NSE_EQ|INE481G01011",
        "BAJAJFINSV": "NSE_EQ|INE918I01026",
        "INDUSINDBK": "NSE_EQ|INE095A01012",
        "NESTLEIND": "NSE_EQ|INE239A01024",
        "COALINDIA": "NSE_EQ|INE522F01014",
        "ADANIPOWER": "NSE_EQ|INE814H01029",
        "SUNTV": "NSE_EQ|INE424H01027",
        "HINDALCO": "NSE_EQ|INE038A01020",
        "DLF": "NSE_EQ|INE271C01023",
        "ADANIGREEN": "NSE_EQ|INE364U01010",
        "GRASIM": "NSE_EQ|INE047A01021",
        "SBILIFE": "NSE_EQ|INE123W01016",
        "TECHM": "NSE_EQ|INE669C01036",
        "HDFCLIFE": "NSE_EQ|INE795G01014",
        "BRITANNIA": "NSE_EQ|INE216A01030",
        "JIOFIN": "NSE_EQ|INE758E01017",
        "TRENT": "NSE_EQ|INE849A01020",
        "HAL": "NSE_EQ|INE066F01020",
        "BEL": "NSE_EQ|INE263A01024",
        "SIEMENS": "NSE_EQ|INE003A01024",
        "IRFC": "NSE_EQ|INE053F01010",
        "PNB": "NSE_EQ|INE160A01022",
        "CANBK": "NSE_EQ|INE476A01022",
        "ABB": "NSE_EQ|INE117A01022",
        "GAIL": "NSE_EQ|INE129A01019",
        "DMART": "NSE_EQ|INE192R01011",
        "GODREJCP": "NSE_EQ|INE102D01028",
        "TATACONSUM": "NSE_EQ|INE192A01025",
        "VBL": "NSE_EQ|INE200M01039",
        "SHREECEM": "NSE_EQ|INE070A01015",
        "INDIGO": "NSE_EQ|INE646L01027",
        "IOC": "NSE_EQ|INE242A01010",
        "BPCL": "NSE_EQ|INE029A01011",
        "HAVELLS": "NSE_EQ|INE176B01034",
        "AMBUJACEM": "NSE_EQ|INE079A01024",
        "CHOLAFIN": "NSE_EQ|INE121A08PJ0",
        "HEROMOTOCO": "NSE_EQ|INE158A01026",
        "ICICIPRULI": "NSE_EQ|INE726G01019",
        "VEDL": "NSE_EQ|INE205A01025",
        "SRF": "NSE_EQ|INE647A01010",
        "MARICO": "NSE_EQ|INE196A01026",
        "MUTHOOTFIN": "NSE_EQ|INE414G01012",
        "PFC": "NSE_EQ|INE134E01011",
        "RECLTD": "NSE_EQ|INE020B01018",
        "POLYCAB": "NSE_EQ|INE455K01017",
        "DRREDDY": "NSE_EQ|INE089A01031",
        "CIPLA": "NSE_EQ|INE059A01026",
        "EICHERMOT": "NSE_EQ|INE066A01021",
        "DIVISLAB": "NSE_EQ|INE361B01024",
        "LUPIN": "NSE_EQ|INE326A01037",
        "AU SMALL": "NSE_EQ|INE949L01017",
        "PIDILITIND": "NSE_EQ|INE318A01026",
        "APOLLOHOSP": "NSE_EQ|INE437A01024",
        "JSWENERGY": "NSE_EQ|INE121E01018",
        "LODHA": "NSE_EQ|INE670K01029",
        "TATACOMM": "NSE_EQ|INE151A01013",
        "MAXHEALTH": "NSE_EQ|INE027H01010",
        "MPHASIS": "NSE_EQ|INE356A01018",
        "PERSISTENT": "NSE_EQ|INE262H01021",
        "CONCOR": "NSE_EQ|INE111A01025",
        "IDFCFIRSTB": "NSE_EQ|INE092T01019",
        "YESBANK": "NSE_EQ|INE528G01035",
        "OLAELEC": "NSE_EQ|INE0LXG01040"
    }

    @classmethod
    def load(cls):
        if os.path.exists("config.json"):
            try:
                with open("config.json", "r") as f:
                    data = json.load(f)
                    for k, v in data.items():
                        if hasattr(cls, k):
                            setattr(cls, k, v)
            except Exception:
                pass
                
    @classmethod
    def save(cls):
        exclude = ['SYMBOLS_MAPPING', 'ANALYTICS_TOKEN', 'UPSTOX_CLIENT_ID', 'UPSTOX_CLIENT_SECRET', 'GROQ_API_KEY', 'REDIRECT_URI', 'DB_USER', 'DB_PASSWORD', 'DB_HOST', 'DB_PORT', 'DB_NAME']
        # removed UPSTOX_ACCESS_TOKEN from exclude list so it can be saved config.json
        data = {k: getattr(cls, k) for k in dir(cls) if not k.startswith("__") and not callable(getattr(cls, k)) and k not in exclude}
        with open("config.json", "w") as f:
            json.dump(data, f, indent=4)

Config.load()
    
# === Bot State Machine ====================================================
class BotStatus:
    STOPPED = "STOPPED"   # User stopped / never started
    RUNNING = "RUNNING"   # Actively trading
    HALTED  = "HALTED"    # Emergency halt (kill-switch)


class SystemState:
    """Per-user mutable bot state. Uses __init__ so each instance gets its OWN dicts."""
    def __init__(self):
        self.daily_pnl          = 0.0
        self.trades_taken       = 0
        self.consecutive_losses = 0
        self.kill_switch_active = False
        self.is_running         = False   # False on boot — user must press START
        self.last_trade_time    = {}      # symbol -> datetime
        self.blocked_symbols    = {}      # symbol -> unblock_time
        self.market_condition   = "WAITING"
        self.live_indices       = {}
        self.index_prev_close   = {}
        self.active_capital     = 100000.0
        self.status             = "STOPPED"
        self.account_balance    = 0.0
        self.usable_balance     = 0.0
        self.last_balance_update = 0.0

    def start(self):
        """Transition to RUNNING. Always clears kill-switch so restart works after HALT."""
        self.is_running         = True
        self.kill_switch_active = False
        self.status             = "RUNNING"

    def stop(self):
        """Pause trading (can be resumed with start())."""
        self.is_running = False
        if self.status != "HALTED":
            self.status = "STOPPED"

    def halt(self):
        """Emergency halt — requires explicit start() to resume."""
        self.is_running         = False
        self.kill_switch_active = True
        self.status             = "HALTED"

    def can_trade(self) -> bool:
        """True only when actively running AND not halted."""
        return self.is_running and not self.kill_switch_active

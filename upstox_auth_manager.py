"""
upstox_auth_manager.py — Automated Upstox Token Generation via TOTP
Fetches a fresh Upstox access token without requiring manual browser login or OTP entry.
"""

import os
import sys
import time
import base64
import hmac
import hashlib
import struct
import json
import logging
import urllib.request
import urllib.error
from config import Config

logger = logging.getLogger("upstox_auth_manager")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s [%(name)s]: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def generate_totp_code(secret_key: str) -> str:
    """
    Computes a standard 6-digit TOTP code RFC 6238 without requiring third-party libraries.
    """
    clean_key = secret_key.strip().replace(" ", "").upper()
    missing_padding = len(clean_key) % 8
    if missing_padding != 0:
        clean_key += '=' * (8 - missing_padding)
    
    key = base64.b32decode(clean_key, casefold=True)
    time_step = int(time.time() // 30)
    msg = struct.pack(">Q", time_step)
    
    h = hmac.new(key, msg, hashlib.sha1).digest()
    offset = h[19] & 0x0F
    code = (struct.unpack(">I", h[offset:offset+4])[0] & 0x7FFFFFFF) % 1000000
    return f"{code:06d}"


def fetch_fresh_upstox_token(
    client_id: str = None,
    totp_secret: str = None,
    pin: str = None
) -> dict:
    """
    Calls Upstox TOTP login endpoint and retrieves a fresh access token.
    """
    client_id = client_id or Config.UPSTOX_CLIENT_ID or os.getenv("UPSTOX_CLIENT_ID")
    totp_secret = totp_secret or Config.UPSTOX_TOTP_SECRET or os.getenv("UPSTOX_TOTP_SECRET")
    pin = pin or Config.UPSTOX_USER_PIN or os.getenv("UPSTOX_USER_PIN")

    if not client_id or not totp_secret or not pin:
        err = "Missing Upstox credentials (client_id, totp_secret, or pin)."
        logger.error(err)
        return {"status": "error", "message": err}

    totp_code = generate_totp_code(totp_secret)
    logger.info(f"Generated TOTP for login: {totp_code}")

    url = "https://api.upstox.com/v2/totp-login/authorization/token"
    headers = {
        "accept": "application/json",
        "Content-Type": "application/json",
        "x-api-key": client_id,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    payload = json.dumps({"totp": totp_code, "pin": pin}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            access_token = data.get("access_token")
            user_name = data.get("user_name", "")
            user_id = data.get("user_id", "")
            
            if not access_token:
                logger.error(f"No access_token in response: {data}")
                return {"status": "error", "message": "No access_token returned", "data": data}

            logger.info(f"Successfully generated token for user {user_name} ({user_id})")
            return {
                "status": "success",
                "access_token": access_token,
                "user_name": user_name,
                "user_id": user_id,
                "full_response": data
            }
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        logger.error(f"Upstox TOTP login HTTP {e.code}: {error_body}")
        return {"status": "error", "http_code": e.code, "message": error_body}
    except Exception as e:
        logger.error(f"Upstox TOTP login exception: {e}")
        return {"status": "error", "message": str(e)}


def update_system_token(access_token: str) -> bool:
    """
    Updates the access token in .env and database (if available).
    """
    # 1. Update Config memory
    Config.UPSTOX_ACCESS_TOKEN = access_token
    os.environ["UPSTOX_ACCESS_TOKEN"] = access_token

    # 2. Update .env file
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            
            updated = False
            for idx, line in enumerate(lines):
                if line.startswith("UPSTOX_ACCESS_TOKEN="):
                    lines[idx] = f'UPSTOX_ACCESS_TOKEN="{access_token}"\n'
                    updated = True
                    break
            
            if not updated:
                lines.append(f'\nUPSTOX_ACCESS_TOKEN="{access_token}"\n')
            
            with open(env_path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            logger.info("Updated UPSTOX_ACCESS_TOKEN in .env")
        except Exception as e:
            logger.error(f"Failed to update .env: {e}")

    # 3. Update database credentials for all users in primary DB
    try:
        from database import SessionLocal, UserCredential
        from credential_service import _safe_encrypt
        db = SessionLocal()
        try:
            creds = db.query(UserCredential).all()
            for c in creds:
                c.upstox_access_token = _safe_encrypt(access_token)
            db.commit()
            logger.info(f"Updated access token in primary database for {len(creds)} user credential row(s).")
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"Primary database credential update skipped or failed: {e}")

    # 4. If Supabase is also configured, sync Supabase as well
    supabase_url = os.getenv("SUPABASE_DB_URL") or getattr(Config, "SUPABASE_DB_URL", "")
    if supabase_url:
        try:
            from sqlalchemy import create_engine
            from sqlalchemy.orm import sessionmaker
            from database import UserCredential
            from credential_service import _safe_encrypt
            pg_url = supabase_url
            if pg_url.startswith("postgres://"):
                pg_url = pg_url.replace("postgres://", "postgresql+psycopg2://", 1)
            elif pg_url.startswith("postgresql://") and not pg_url.startswith("postgresql+psycopg2://"):
                pg_url = pg_url.replace("postgresql://", "postgresql+psycopg2://", 1)
            supa_engine = create_engine(pg_url, pool_pre_ping=True)
            SupaSession = sessionmaker(bind=supa_engine)
            with SupaSession() as supa_db:
                s_creds = supa_db.query(UserCredential).all()
                for sc in s_creds:
                    sc.upstox_access_token = _safe_encrypt(access_token)
                supa_db.commit()
                logger.info(f"Synchronized access token to Supabase for {len(s_creds)} credential row(s).")
        except Exception as supa_err:
            logger.debug(f"Dual-sync to Supabase skipped: {supa_err}")

    return True


def auto_refresh_and_apply(silent: bool = False) -> dict:
    """
    One-click runner: Generates token and applies it to the system.
    """
    res = fetch_fresh_upstox_token()
    if res.get("status") == "success":
        new_token = res["access_token"]
        update_system_token(new_token)
        
        # Send telegram alert if configured and not explicitly silenced
        if not silent:
            try:
                from telegram_alert import send_telegram
                send_telegram(f"✅ <b>Upstox Token Auto-Refreshed!</b>\nUser: {res.get('user_name')}\nStatus: Active for today's market session.")
            except Exception:
                pass

    return res


if __name__ == "__main__":
    result = auto_refresh_and_apply()
    print("\nResult:", json.dumps({k: v for k, v in result.items() if k != "full_response"}, indent=2))

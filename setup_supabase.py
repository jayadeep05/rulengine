"""
Quick setup script to initialize all IntradayBot database tables in Supabase PostgreSQL.
Run:
    python setup_supabase.py
"""
import os
import sys
from database import engine, Base

def init_supabase():
    db_url = os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL")
    if not db_url:
        print("ERROR: Please set SUPABASE_DB_URL in your .env file or environment.")
        print("Example: SUPABASE_DB_URL=postgresql://postgres:[PASSWORD]@db.[PROJECT_REF].supabase.co:5432/postgres")
        sys.exit(1)

    print("Connecting to Supabase PostgreSQL...")
    try:
        # Create all tables defined in database.py
        Base.metadata.create_all(bind=engine)
        print("SUCCESS: All tables (users, user_credentials, user_config, trades) created/verified in Supabase!")
    except Exception as e:
        print(f"FAILED to initialize Supabase schema: {e}")
        sys.exit(1)

if __name__ == "__main__":
    init_supabase()

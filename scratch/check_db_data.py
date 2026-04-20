import sys
import os
sys.path.append(os.getcwd())

from database import SessionLocal, Trade
from sqlalchemy import desc

def check_history():
    db = SessionLocal()
    try:
        trades = db.query(Trade).all()
        print(f"Total trades in DB: {len(trades)}")
        for i, t in enumerate(trades[:10]):
            print(f"{i+1}: ID={t.id}, Symbol={t.symbol}, Status={t.status}, Date={t.trade_date}, PnL={t.realized_pnl}")
        
        closed_trades = [t for t in trades if t.status == 'CLOSED']
        print(f"Total CLOSED trades: {len(closed_trades)}")
        
        # Check if any trades are from today
        from database import get_ist_date
        today = get_ist_date()
        today_trades = [t for t in trades if t.trade_date == today]
        print(f"Trades from today ({today}): {len(today_trades)}")
        
    except Exception as e:
        print(f"Error: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    check_history()

from database import SessionLocal, Trade
import collections

db = SessionLocal()
try:
    trades = db.query(Trade).all()
    ids = [t.id for t in trades]
    dupes = [item for item, count in collections.Counter(ids).items() if count > 1]
    if dupes:
        print(f"Warning: Found duplicate Trade IDs in DB: {dupes}")
    else:
        print("No duplicate Trade IDs found in DB.")
        
    # Check for identical trades (symbol, time, side)
    signatures = []
    for t in trades:
        sig = (t.symbol, t.side, t.entry_time.strftime('%Y-%m-%d %H:%M') if t.entry_time else 'None')
        signatures.append(sig)
    
    sig_dupes = [item for item, count in collections.Counter(signatures).items() if count > 1]
    if sig_dupes:
        print(f"Warning: Found multiple trades with same signature (symbol, side, minute): {sig_dupes}")
    else:
        print("No duplicate trade signatures found.")

finally:
    db.close()

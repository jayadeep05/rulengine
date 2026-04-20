import pytz

with open('trade_manager.py', 'r', encoding='utf-8') as f:
    content = f.read()

content = content.replace('from database import SessionLocal, Trade, DailyStats, SystemLog', 'from database import SessionLocal, Trade, DailyStats, SystemLog, get_ist_now, get_ist_date\nimport pytz\n\nIST = pytz.timezone(\'Asia/Kolkata\')')

content = content.replace('datetime.datetime.utcnow().date()', 'get_ist_date()')
content = content.replace('datetime.datetime.utcnow()', 'get_ist_now()')
content = content.replace('datetime.datetime.now().isoformat()', 'get_ist_now().isoformat()')
content = content.replace('datetime.datetime.now()', 'get_ist_now()')

# We also need to fix the specific piece of code:
old_system_state_block = """        if not hasattr(SystemState, 'consecutive_losses'):
            SystemState.consecutive_losses = 0
            
        if trade['pnl'] < 0:
            SystemState.consecutive_losses += 1
            if SystemState.consecutive_losses >= 2:
                import pytz
                IST = pytz.timezone('Asia/Kolkata')
                import datetime as dt
                SystemState.loss_cooldown_until = dt.datetime.now(IST) + dt.timedelta(minutes=30)"""

new_system_state_block = """        if not hasattr(SystemState, 'consecutive_losses'):
            SystemState.consecutive_losses = 0
            
        if trade['pnl'] < 0:
            SystemState.consecutive_losses += 1
            if SystemState.consecutive_losses >= 2:
                SystemState.loss_cooldown_until = datetime.datetime.now(IST) + datetime.timedelta(minutes=30)"""

if old_system_state_block in content:
    content = content.replace(old_system_state_block, new_system_state_block)

with open('trade_manager.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("trade_manager.py updated successfully.")

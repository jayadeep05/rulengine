#!/bin/bash
mysql -u root -p'#JAYA1708!!' trade_history 2>&1 << 'EOF'
SELECT COUNT(*) as total_trades FROM trades;
SELECT id, symbol, side, status, entry_price, exit_price, realized_pnl, trade_date, entry_time, exit_time FROM trades ORDER BY entry_time DESC LIMIT 10;
SELECT * FROM daily_sessions ORDER BY session_date DESC LIMIT 5;
EOF

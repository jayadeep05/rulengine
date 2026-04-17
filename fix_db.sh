#!/bin/bash
set -e
echo "=== Step 1: Fix MySQL root auth + create DB ==="
sudo mysql -e "ALTER USER 'root'@'localhost' IDENTIFIED WITH mysql_native_password BY '#JAYA1708!!'; FLUSH PRIVILEGES; CREATE DATABASE IF NOT EXISTS trade_history;"
echo "=== MySQL DB and auth fixed! ==="

echo "=== Step 2: Run schema migrations ==="
cd /home/ubuntu/intradayBot
python3 -c "from database import init_db; init_db()"
echo "=== Tables created! ==="

echo "=== Step 3: Restart the bot ==="
pkill -f uvicorn || true
sleep 2
nohup /home/ubuntu/.local/bin/uvicorn main:app --host 0.0.0.0 --port 8000 > /home/ubuntu/bot.log 2>&1 &
sleep 5
echo "=== Latest bot.log ==="
tail -30 /home/ubuntu/bot.log

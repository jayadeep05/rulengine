#!/bin/bash
echo "Stopping existing bot processes..."
# Kill anything on port 8000
fuser -k 8000/tcp || true
# Kill any remaining uvicorn processes
pkill -9 -f uvicorn || true
sleep 3

cd /home/ubuntu/intradayBot
echo "Starting bot with nohup..."
nohup /home/ubuntu/.local/bin/uvicorn main:app --host 0.0.0.0 --port 8000 > /home/ubuntu/bot.log 2>&1 &

sleep 5
echo "=== BOT STATUS ==="
ps aux | grep uvicorn | grep -v grep
echo "=== RECENT LOGS ==="
tail -n 20 /home/ubuntu/bot.log

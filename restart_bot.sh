#!/bin/bash
pkill -f uvicorn || true
sleep 2
cd /home/ubuntu/intradayBot
nohup /home/ubuntu/.local/bin/uvicorn main:app --host 0.0.0.0 --port 8000 > /home/ubuntu/bot.log 2>&1 &
sleep 4
echo "=== BOT LOG ==="
cat /home/ubuntu/bot.log

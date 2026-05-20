#!/bin/bash
# 等待 yfinance 下载进程 (PID 69560) 结束，再启动 EDGAR 补全
DOWNLOADER_PID=69560
LOG="data/edgar_supplement.log"

echo "[$(date '+%H:%M:%S')] 等待 yfinance 下载完成 (PID $DOWNLOADER_PID)..."

while kill -0 $DOWNLOADER_PID 2>/dev/null; do
    sleep 30
done

echo "[$(date '+%H:%M:%S')] yfinance 下载完成，正在启动 EDGAR 补全..."
cd /Users/bytedance/Documents/Nasdaq_Hunter
python3 edgar_supplement.py --reset >> "$LOG" 2>&1
echo "[$(date '+%H:%M:%S')] EDGAR 补全已完成。查看日志: $LOG"

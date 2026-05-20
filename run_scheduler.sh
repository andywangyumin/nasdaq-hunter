#!/bin/bash
# NASDAQ Hunter — 调度器启动脚本
# 用法:
#   手动启动: bash run_scheduler.sh
#   后台常驻: nohup bash run_scheduler.sh > /dev/null 2>&1 &
#   launchd 自动调用此脚本（需要 /bin/bash 具有 Full Disk Access）

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# 加载 .env 环境变量
if [ -f "$DIR/.env" ]; then
    set -a
    source "$DIR/.env"
    set +a
fi

exec /usr/bin/python3 "$DIR/scheduler_v2.py"

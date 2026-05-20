#!/usr/bin/env python3
"""
定时调度器 v2
触发时间（双任务）：
  · SCAN  — 每天 17:30 EST/EDT（美股收盘1.5小时后）扫描，结果存入 DB
  · PUSH  — 每天 04:00 UTC（北京时间 12:00）统一推送前一次扫描结果

时间换算：
  17:30 EDT (UTC-4) = 21:30 UTC = 北京 05:30 次日  → 12:00 北京时推出（6.5小时后）
  17:30 EST (UTC-5) = 22:30 UTC = 北京 06:30 次日  → 12:00 北京时推出（5.5小时后）

运行:
  python scheduler_v2.py
  # 后台运行:
  nohup bash run_scheduler.sh &
"""

import sys, time, signal, logging
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

sys.path.insert(0, str(Path(__file__).parent))
from scanner_v2 import run_scan, init_db, already_scanned_today, push_pending_report, DATA_DIR
from nasdaq_downloader import init_db as dl_init_db, daily_refresh

# ── 配置 ──────────────────────────────────────────────────────
SCAN_HOUR_EST   = 17    # 扫描时间：17:30 美东（EST/EDT 自动切换）
SCAN_MINUTE_EST = 30
PUSH_HOUR_UTC   = 4     # 推送时间：04:00 UTC = 北京时间 12:00
PUSH_MINUTE_UTC = 0
SKIP_WEEKENDS   = True  # 周末不扫描、不推送
MAX_RETRIES     = 3
RETRY_WAIT_SEC  = 300   # 失败后等5分钟重试

# ── 日志 ──────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(DATA_DIR / "scheduler.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("scheduler")

_running = True


def _signal_handler(sig, frame):
    global _running
    log.info("收到信号 %s，准备退出…", sig)
    _running = False


signal.signal(signal.SIGINT,  _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# ── 时区工具 ──────────────────────────────────────────────────

def _is_dst(dt: datetime) -> bool:
    """判断美国夏令时（3月第2个周日 ~ 11月第1个周日）"""
    m = dt.month
    if m < 3 or m > 11: return False
    if 4 <= m <= 10:    return True
    if m == 3:
        sun2 = 8 + (6 - datetime(dt.year, 3, 1).weekday()) % 7
        return dt.day >= sun2
    # m == 11
    sun1 = 1 + (6 - datetime(dt.year, 11, 1).weekday()) % 7
    return dt.day < sun1


def _est_offset() -> timedelta:
    now_utc = datetime.now(timezone.utc)
    return timedelta(hours=-4) if _is_dst(now_utc) else timedelta(hours=-5)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _now_est() -> datetime:
    return _now_utc() + _est_offset()


# ── 下次触发时间计算 ───────────────────────────────────────────

def _next_scan_utc() -> datetime:
    """下一次扫描的 UTC 时间（跳过周末）"""
    offset = _est_offset()
    est_now = _now_est()
    target = est_now.replace(hour=SCAN_HOUR_EST, minute=SCAN_MINUTE_EST, second=0, microsecond=0)
    if est_now >= target:
        target += timedelta(days=1)
    if SKIP_WEEKENDS:
        while target.weekday() >= 5:
            target += timedelta(days=1)
    return target - offset   # → UTC（naive）


def _next_push_utc() -> datetime:
    """下一次推送的 UTC 时间（跳过周末）"""
    now = _now_utc()
    target = now.replace(hour=PUSH_HOUR_UTC, minute=PUSH_MINUTE_UTC, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    if SKIP_WEEKENDS:
        while target.weekday() >= 5:
            target += timedelta(days=1)
    return target


def _beijing(utc_dt: datetime) -> str:
    """UTC naive → 北京时间字符串"""
    return (utc_dt + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")


# ── 任务执行 ───────────────────────────────────────────────────

def _refresh_db() -> None:
    """扫描前增量刷新历史数据库（价格 + 利率 + 基本面滚动）"""
    try:
        log.info("── 数据库增量刷新开始 ──────────────────────────────")
        conn = dl_init_db()
        stats = daily_refresh(conn)
        conn.close()
        log.info(
            "── 数据库刷新完成: 价格+%d条  基本面刷新%d只 ──",
            stats["price_rows"], stats["fund_refreshed"],
        )
    except Exception as e:
        log.error("数据库刷新失败（不影响扫描）: %s", e, exc_info=True)


def _run_scan_with_retry(force: bool = False) -> bool:
    _refresh_db()
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            log.info("扫描第 %d 次尝试…", attempt)
            run_scan(force=force)
            log.info("扫描完成，报告已存入 DB，等待 12:00 北京时推送")
            return True
        except Exception as e:
            log.error("第 %d 次失败: %s", attempt, e, exc_info=True)
            if attempt < MAX_RETRIES:
                log.info("等待 %d 秒后重试…", RETRY_WAIT_SEC)
                for _ in range(RETRY_WAIT_SEC):
                    if not _running: return False
                    time.sleep(1)
    log.error("扫描失败 %d 次，放弃今日扫描", MAX_RETRIES)
    return False


def _run_push_with_retry() -> bool:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            log.info("推送第 %d 次尝试…", attempt)
            ok = push_pending_report()
            if ok:
                return True
        except Exception as e:
            log.error("推送第 %d 次失败: %s", attempt, e, exc_info=True)
        if attempt < MAX_RETRIES:
            for _ in range(60):   # 推送失败等1分钟重试
                if not _running: return False
                time.sleep(1)
    log.error("推送失败 %d 次", MAX_RETRIES)
    return False


# ── 启动补跑检查 ───────────────────────────────────────────────

def _startup_check() -> None:
    """启动时检查：若错过了扫描或推送时间则立即补执行"""
    now_utc  = _now_utc()
    now_est  = _now_est()
    offset   = _est_offset()
    today    = str(date.today())

    # 是工作日才做补跑检查
    if now_est.weekday() >= 5:
        log.info("今天是周末，跳过补跑检查")
        return

    con = init_db()
    scanned_today = already_scanned_today(con)

    # 当前 EST 时间超过扫描时间 → 若未扫描则补扫
    past_scan = (now_est.hour > SCAN_HOUR_EST or
                 (now_est.hour == SCAN_HOUR_EST and now_est.minute >= SCAN_MINUTE_EST))
    if past_scan and not scanned_today:
        log.info("启动检测：今天 %s 尚未扫描，立即补跑…", today)
        con.close()
        _run_scan_with_retry(force=False)
        con = init_db()

    # 当前 UTC 时间超过推送时间 → 若今日扫描完成但未推送则补推
    past_push = (now_utc.hour > PUSH_HOUR_UTC or
                 (now_utc.hour == PUSH_HOUR_UTC and now_utc.minute >= PUSH_MINUTE_UTC))
    if past_push:
        row = con.execute(
            "SELECT pushed FROM reports WHERE report_date=?", (today,)
        ).fetchone()
        if row and row[0] == 0:
            log.info("启动检测：今天报告尚未推送，立即补推…")
            con.close()
            _run_push_with_retry()
            return

    con.close()
    log.info("启动检测完成，等待定时触发")


# ── 主循环 ─────────────────────────────────────────────────────

def _sleep_until(target_utc: datetime) -> bool:
    """精确睡眠到 target_utc，每分钟检查一次 _running。返回 False 表示提前退出。"""
    while _running:
        remain = (target_utc - _now_utc()).total_seconds()
        if remain <= 0:
            return True
        time.sleep(min(60.0, remain))
    return False


def main():
    log.info("=" * 60)
    log.info("NASDAQ 爆发股猎手 · 定时调度器 v2")
    log.info("  扫描: 每天 %02d:%02d EST/EDT（美股收盘后）", SCAN_HOUR_EST, SCAN_MINUTE_EST)
    log.info("  推送: 每天 %02d:%02d UTC = 北京时间 12:00", PUSH_HOUR_UTC, PUSH_MINUTE_UTC)
    log.info("=" * 60)

    _startup_check()

    while _running:
        next_scan = _next_scan_utc()
        next_push = _next_push_utc()

        # 取最近的触发时间
        if next_scan <= next_push:
            next_evt    = next_scan
            evt_label   = "扫描"
            evt_beijing = _beijing(next_scan)
            est_str     = (next_scan + _est_offset()).strftime("%Y-%m-%d %H:%M")
            log.info("下次事件: [扫描] %s EST  (北京 %s)  还有 %.1f 小时",
                     est_str, evt_beijing,
                     (next_scan - _now_utc()).total_seconds() / 3600)
        else:
            next_evt    = next_push
            evt_label   = "推送"
            evt_beijing = _beijing(next_push)
            log.info("下次事件: [推送] %s UTC  (北京 %s)  还有 %.1f 小时",
                     next_push.strftime("%Y-%m-%d %H:%M"),
                     evt_beijing,
                     (next_push - _now_utc()).total_seconds() / 3600)

        if not _sleep_until(next_evt):
            break

        # 检查漂移（允许 ±2 分钟）
        drift = abs((_now_utc() - next_evt).total_seconds())
        if drift > 120:
            log.info("时间漂移 %.0f 秒，执行补偿检查", drift)
            _startup_check()   # 补跑被 Mac 睡眠打断的扫描/推送
            time.sleep(5)
            continue

        if evt_label == "扫描":
            log.info("▶ 触发每日扫描 (%s EST)", (next_scan + _est_offset()).strftime("%H:%M"))
            _run_scan_with_retry(force=False)
        else:
            log.info("▶ 触发每日推送 (北京 12:00)")
            _run_push_with_retry()

        time.sleep(90)   # 防止同一分钟重复触发

    log.info("调度器已停止")


if __name__ == "__main__":
    main()

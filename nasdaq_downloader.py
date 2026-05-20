"""
nasdaq_downloader.py — NASDAQ 历史价格数据下载器
=================================================
数据来源：
  · 价格 OHLCV  : yfinance (免费批量, ~20min)
  · 季度基本面  : Finnhub  (含真实 filedDate 实现严格 Point-in-Time)
  · 宏观利率    : Finnhub economic endpoint

输出: data/nasdaq_history.db

用法：
  python3 nasdaq_downloader.py              # 首次下载（默认 5 年）
  python3 nasdaq_downloader.py --years 5   # 显式指定年数
  python3 nasdaq_downloader.py --extend    # 向前补充：不重置，仅下载早于现有最早日期的数据
  python3 nasdaq_downloader.py --reset     # 重置全部进度重新下载
"""

import os, sys, time, sqlite3, logging, argparse
from datetime import date, timedelta, datetime
from pathlib import Path

# ── 依赖检查 ─────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import yfinance as yf
    import pandas as pd
except ImportError:
    sys.exit("请先安装: pip3 install yfinance pandas")

import requests

# ── 配置 ─────────────────────────────────────────────────────────────────
FINNHUB_KEY = os.getenv("FINNHUB_KEY", "d826l09r01qtiq8ur880d826l09r01qtiq8ur88g")
FH_BASE     = "https://finnhub.io/api/v1"

DATA_DIR    = Path(__file__).parent / "data"
DB_PATH     = DATA_DIR / "nasdaq_history.db"

DEFAULT_YEARS = 5
END_DATE      = date.today().isoformat()

# Finnhub 免费 60 req/min，保守 1.2s 间隔
FH_DELAY   = 1.2
YF_BATCH   = 50   # 每批 yfinance 下载数量

# ── 日志 ─────────────────────────────────────────────────────────────────
DATA_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(DATA_DIR / "downloader.log", mode="a", encoding="utf-8"),
    ],
)
log = logging.getLogger("dl")


# ══════════════════════════════════════════════════════════════
#  股票宇宙
# ══════════════════════════════════════════════════════════════

def get_universe() -> "list":
    """
    组合两个来源：
      1. scanner_v2.py 现有 500 只宇宙（已验证、高质量）
      2. Finnhub 交易所列表补充更多 NASDAQ 普通股
    """
    base = _load_scanner_universe()
    extra = _fetch_nasdaq_from_finnhub()
    combined = list(dict.fromkeys(base + extra))   # 去重保序
    log.info(f"最终宇宙: {len(combined)} 只 (scanner={len(base)}, 新增={len(set(extra)-set(base))})")
    return combined


def _load_scanner_universe() -> "list":
    """读取 scanner_v2.py 的 TICKERS 列表"""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "sv2", Path(__file__).parent / "scanner_v2.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        tickers = list(getattr(mod, "TICKERS", []))
        log.info(f"scanner_v2.py 宇宙: {len(tickers)} 只")
        return tickers
    except Exception as e:
        log.warning(f"无法读取 scanner_v2.py ({e}), 使用内置列表")
        return [
            "NVDA","AMD","MRVL","CRDO","APP","AXON","CELH","ELF","FTAI","SNDK",
            "AAPL","MSFT","GOOGL","META","AMZN","TSLA","PLTR","ARM","AVGO","MU",
        ]


def _fetch_nasdaq_from_finnhub() -> "list":
    """从 Finnhub 获取所有 NASDAQ 上市普通股（补充 scanner_v2 之外的股票）"""
    log.info("从 Finnhub 获取 NASDAQ 股票列表...")
    try:
        r = requests.get(
            f"{FH_BASE}/stock/symbol",
            params={"exchange": "US", "token": FINNHUB_KEY},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning(f"获取失败: {e}")
        return []

    tickers = []
    for item in data:
        sym  = item.get("symbol", "")
        typ  = item.get("type", "")
        mic  = item.get("mic", "")
        # 仅保留 NASDAQ 三大市场的普通股
        if (typ in ("Common Stock", "EQS")
                and mic in ("XNAS", "XNMS", "XNCM")
                and sym.isalpha()
                and len(sym) <= 5):
            tickers.append(sym)

    log.info(f"Finnhub 返回 {len(tickers)} 只 NASDAQ 普通股")
    return tickers


# ══════════════════════════════════════════════════════════════
#  数据库初始化
# ══════════════════════════════════════════════════════════════

def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        -- 日线价格 (OHLCV)
        CREATE TABLE IF NOT EXISTS prices (
            ticker TEXT NOT NULL,
            date   TEXT NOT NULL,
            open   REAL, high REAL, low REAL, close REAL, volume REAL,
            PRIMARY KEY (ticker, date)
        );
        CREATE INDEX IF NOT EXISTS idx_p_date   ON prices(date);
        CREATE INDEX IF NOT EXISTS idx_p_ticker ON prices(ticker, date);

        -- 季度基本面 (严格 Point-in-Time: 使用真实 SEC filedDate)
        CREATE TABLE IF NOT EXISTS fundamentals (
            ticker        TEXT NOT NULL,
            period        TEXT NOT NULL,   -- 财季末日期 YYYY-MM-DD
            filing_date   TEXT NOT NULL,   -- SEC 实际披露日 (10-Q/10-K)
            revenue       REAL,            -- 当季营收 (USD)
            revenue_yoy   REAL,            -- 营收同比增速 %
            gross_margin  REAL,            -- 毛利率 %
            eps_actual    REAL,            -- 实际 EPS
            eps_estimate  REAL,            -- 一致预期 EPS
            eps_surprise  REAL,            -- EPS 超预期幅度 %
            beats_4q      INTEGER,         -- 过去4季超预期次数 (>5%)
            PRIMARY KEY (ticker, period)
        );
        CREATE INDEX IF NOT EXISTS idx_f_filing ON fundamentals(filing_date);
        CREATE INDEX IF NOT EXISTS idx_f_ticker ON fundamentals(ticker, filing_date);

        -- 宏观利率历史
        CREATE TABLE IF NOT EXISTS macro_rates (
            date     TEXT PRIMARY KEY,
            fed_rate REAL
        );

        -- 下载进度 (支持 Ctrl+C 断点续传)
        CREATE TABLE IF NOT EXISTS dl_progress (
            ticker    TEXT PRIMARY KEY,
            price_ok  INTEGER DEFAULT 0,
            fund_ok   INTEGER DEFAULT 0,
            updated   TEXT
        );

        -- 每日扫描信号（scanner_v2.py 写入，列比旧 daily_scores 更丰富）
        CREATE TABLE IF NOT EXISTS daily_signals (
            ticker      TEXT NOT NULL,
            scan_date   TEXT NOT NULL,
            dna         INTEGER,
            trend       INTEGER,
            comp        INTEGER,
            threshold   INTEGER,
            triggered   INTEGER DEFAULT 0,
            price       REAL,
            stop_price  REAL,
            fed_rate    REAL,
            gm          REAL,
            rev_yoy     REAL,
            eps_sur     REAL,
            beats_4q    INTEGER,
            fail_reason TEXT,
            source      TEXT DEFAULT 'scanner',
            PRIMARY KEY (ticker, scan_date)
        );
        CREATE INDEX IF NOT EXISTS idx_ds_date   ON daily_signals(scan_date);
        CREATE INDEX IF NOT EXISTS idx_ds_ticker ON daily_signals(ticker, scan_date);

        -- 每日扫描元数据
        CREATE TABLE IF NOT EXISTS scan_log (
            scan_date   TEXT PRIMARY KEY,
            started_at  TEXT,
            finished_at TEXT,
            status      TEXT,
            n_scanned   INTEGER DEFAULT 0,
            n_passed    INTEGER DEFAULT 0
        );

        -- Claude AI 每日报告存档
        CREATE TABLE IF NOT EXISTS reports (
            report_date TEXT PRIMARY KEY,
            action      TEXT,
            json        TEXT,
            pushed      INTEGER DEFAULT 0,
            created_at  TEXT
        );

        -- 信号实际收益追踪（可由外部脚本定期回填）
        CREATE TABLE IF NOT EXISTS signal_outcomes (
            ticker      TEXT NOT NULL,
            signal_date TEXT NOT NULL,
            entry_price REAL,
            exit_date   TEXT,
            exit_price  REAL,
            exit_reason TEXT,
            ret_1m      REAL,
            ret_3m      REAL,
            ret_6m      REAL,
            ret_12m     REAL,
            PRIMARY KEY (ticker, signal_date)
        );
    """)
    conn.commit()
    return conn


# ══════════════════════════════════════════════════════════════
#  Finnhub 请求工具
# ══════════════════════════════════════════════════════════════

def fh_get(endpoint: str, params: dict, retries: int = 3):
    params = {**params, "token": FINNHUB_KEY}
    url    = f"{FH_BASE}/{endpoint}"
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=15)
            if r.status_code == 429:
                wait = 65 * (attempt + 1)
                log.warning(f"Finnhub 限流, 等待 {wait}s")
                time.sleep(wait)
                continue
            if r.status_code != 200:
                return None
            return r.json()
        except Exception as e:
            if attempt == retries - 1:
                log.debug(f"fh_get {endpoint}: {e}")
            time.sleep(2 ** attempt)
    return None


# ══════════════════════════════════════════════════════════════
#  阶段1: 价格下载 (yfinance 批量)
# ══════════════════════════════════════════════════════════════

def download_prices(tickers: list, conn: sqlite3.Connection, start: str,
                    extend: bool = False) -> None:
    if extend:
        # 扩充模式：只重新下载那些最早价格日期晚于目标 start 的 ticker
        earliest = conn.execute(
            "SELECT ticker, MIN(date) FROM prices GROUP BY ticker"
        ).fetchall()
        need_extend = {tk for tk, min_date in earliest if min_date > start}
        conn.executemany(
            "UPDATE dl_progress SET price_ok=0 WHERE ticker=?",
            [(t,) for t in need_extend]
        )
        conn.commit()
        log.info("扩充模式：%d 只 ticker 需向前补充价格（最早日期晚于 %s）", len(need_extend), start)

    done = {r[0] for r in conn.execute("SELECT ticker FROM dl_progress WHERE price_ok=1")}
    todo = [t for t in tickers if t not in done]

    if not todo:
        log.info("价格数据已全部下载 ✓")
        return

    n_batches = (len(todo) + YF_BATCH - 1) // YF_BATCH
    log.info(f"下载价格: {len(todo)} 只, {n_batches} 批, 每批 {YF_BATCH} 只")
    total_rows = 0

    for b_start in range(0, len(todo), YF_BATCH):
        batch = todo[b_start : b_start + YF_BATCH]
        b_num = b_start // YF_BATCH + 1
        log.info(f"  价格批次 {b_num}/{n_batches}: {batch[:3]}{'...' if len(batch)>3 else ''} ({len(batch)}只)")

        try:
            raw = yf.download(
                tickers=batch,
                start=start,
                end=END_DATE,
                auto_adjust=True,
                progress=False,
            )
            if raw.empty:
                continue

            rows = []
            for ticker in batch:
                try:
                    # yfinance 多 ticker 返回 MultiIndex columns (field, ticker)
                    if len(batch) > 1:
                        df = raw.xs(ticker, axis=1, level=1)
                    else:
                        df = raw
                    df = df.dropna(subset=["Close"])
                    for dt, row in df.iterrows():
                        rows.append((
                            ticker,
                            dt.strftime("%Y-%m-%d"),
                            _f(row.get("Open")),
                            _f(row.get("High")),
                            _f(row.get("Low")),
                            _f(row.get("Close")),
                            _f(row.get("Volume")),
                        ))
                except Exception:
                    pass

            if rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO prices(ticker,date,open,high,low,close,volume)"
                    " VALUES(?,?,?,?,?,?,?)", rows
                )
                conn.commit()
                total_rows += len(rows)

            # 标记本批已完成
            conn.executemany("""
                INSERT OR REPLACE INTO dl_progress(ticker,price_ok,fund_ok,updated)
                VALUES(?, 1,
                    COALESCE((SELECT fund_ok FROM dl_progress WHERE ticker=?), 0),
                    datetime('now'))
            """, [(t, t) for t in batch])
            conn.commit()
            log.info(f"    → 入库 {len(rows)} 条")

        except KeyboardInterrupt:
            log.info("中断，进度已保存，重启可续传")
            sys.exit(0)
        except Exception as e:
            log.error(f"  批次 {b_num} 失败: {e}")

    log.info(f"价格下载完成，共 {total_rows:,} 条记录 ✓")


def _f(v):
    """安全转 float，NaN/inf 返回 None"""
    try:
        x = float(v)
        return x if (x == x and x != float("inf")) else None
    except (TypeError, ValueError):
        return None


# ══════════════════════════════════════════════════════════════
#  阶段2: 基本面下载 (Finnhub, 含真实 SEC filedDate)
# ══════════════════════════════════════════════════════════════

def download_fundamentals(tickers: list, conn: sqlite3.Connection) -> None:
    done = {r[0] for r in conn.execute("SELECT ticker FROM dl_progress WHERE fund_ok=1")}
    todo = [t for t in tickers if t not in done]

    if not todo:
        log.info("基本面数据已全部下载 ✓")
        return

    est_min = len(todo) * 2 * FH_DELAY / 60
    log.info(f"下载基本面: {len(todo)} 只, 预计约 {est_min:.0f} 分钟")
    log.info("（可随时 Ctrl+C 中断，重启自动续传）")

    for idx, ticker in enumerate(todo, 1):
        try:
            _fund_one(ticker, conn)
        except KeyboardInterrupt:
            log.info("中断，进度已保存")
            sys.exit(0)
        except Exception as e:
            log.debug(f"  {ticker}: {e}")

        # 无论成功与否，标记已处理（避免重试同一个失败 ticker 卡住进度）
        conn.execute("""
            INSERT OR REPLACE INTO dl_progress(ticker, price_ok, fund_ok, updated)
            VALUES(?,
                COALESCE((SELECT price_ok FROM dl_progress WHERE ticker=?), 0),
                1, datetime('now'))
        """, (ticker, ticker))
        conn.commit()

        if idx % 100 == 0 or idx == len(todo):
            pct = idx / len(todo) * 100
            eta = (len(todo) - idx) * 2 * FH_DELAY / 60
            log.info(f"  进度: {idx}/{len(todo)} ({pct:.0f}%) ETA ≈{eta:.0f}min")


def _fund_one(ticker: str, conn: sqlite3.Connection) -> None:
    """下载单只股票的季度基本面，写入 fundamentals 表
    数据来源：yfinance 季度财报（免费，含收入/毛利润历史）
              Finnhub stock/earnings（免费，含 EPS 超预期历史）
    注：Finnhub stock/financials 需要付费账户，故改用 yfinance
    """
    import yfinance as yf

    # ① yfinance 季度财务报表 (收入、毛利润) — 免费，最多12季
    quarterly = []
    try:
        tk = yf.Ticker(ticker)
        qinc = tk.quarterly_income_stmt   # columns = quarter dates, rows = line items
        if qinc is not None and not qinc.empty:
            rev_row = None
            gp_row  = None
            for idx_name in qinc.index:
                n = str(idx_name).lower()
                if rev_row is None and ("total revenue" in n or "revenue" == n):
                    rev_row = qinc.loc[idx_name]
                if gp_row is None and "gross profit" in n:
                    gp_row = qinc.loc[idx_name]

            for col in qinc.columns:
                period = col.strftime("%Y-%m-%d") if hasattr(col, "strftime") else str(col)[:10]
                rev = float(rev_row[col]) if rev_row is not None and not pd.isna(rev_row[col]) else None
                gp  = float(gp_row[col])  if gp_row  is not None and not pd.isna(gp_row[col])  else None
                if not period or rev is None:
                    continue
                gm = (gp / rev * 100) if gp is not None and rev > 0 else None
                # filing_date: SEC 规定 10-Q 须在季末 45 天内提交
                try:
                    filed_date = (datetime.strptime(period, "%Y-%m-%d")
                                  + timedelta(days=45)).strftime("%Y-%m-%d")
                except Exception:
                    filed_date = period
                quarterly.append({"period": period, "filing_date": filed_date,
                                   "revenue": rev, "gm": gm})
    except Exception as e:
        log.debug("yfinance quarterly %s: %s", ticker, e)

    # ② Finnhub stock/earnings — EPS 超预期历史（免费）
    earn = fh_get("stock/earnings", {"symbol": ticker, "limit": 20})
    time.sleep(FH_DELAY)

    # 按财季升序排列，计算 revenue_yoy（与 4 季前对比）
    quarterly.sort(key=lambda x: x["period"])
    for i, q in enumerate(quarterly):
        q["revenue_yoy"] = None
        if i >= 4:
            prior = quarterly[i - 4]["revenue"]
            if prior and prior > 0:
                q["revenue_yoy"] = (q["revenue"] - prior) / prior * 100

    # ── 解析 EPS 超预期 ────────────────────────────────────────
    eps_map = {}
    if earn and isinstance(earn, list):
        for e in earn:
            p = (e.get("period") or "")[:10]
            if not p:
                continue
            actual   = e.get("actual")
            estimate = e.get("estimate")
            surprise = None
            if actual is not None and estimate and estimate != 0:
                surprise = (actual - estimate) / abs(estimate) * 100
            eps_map[p] = {"actual": actual, "estimate": estimate, "surprise": surprise}

    # 计算截至某季的最近4季超预期次数
    eps_sorted = sorted(eps_map.items())

    def beats_4q(as_of):
        eligible = [(p, v) for p, v in eps_sorted if p <= as_of][-4:]
        return sum(1 for _, v in eligible
                   if v["surprise"] is not None and v["surprise"] > 5)

    # ── 组装并入库 ─────────────────────────────────────────────
    rows = []
    for q in quarterly:
        eps = eps_map.get(q["period"], {})
        rows.append((
            ticker,
            q["period"],
            q["filing_date"],
            q["revenue"],
            q["revenue_yoy"],
            q["gm"],
            eps.get("actual"),
            eps.get("estimate"),
            eps.get("surprise"),
            beats_4q(q["period"]),
        ))

    if rows:
        conn.executemany("""
            INSERT OR REPLACE INTO fundamentals
            (ticker,period,filing_date,revenue,revenue_yoy,gross_margin,
             eps_actual,eps_estimate,eps_surprise,beats_4q)
            VALUES(?,?,?,?,?,?,?,?,?,?)
        """, rows)
        conn.commit()


# ══════════════════════════════════════════════════════════════
#  阶段3: 宏观利率历史
# ══════════════════════════════════════════════════════════════

def download_macro(conn: sqlite3.Connection) -> None:
    log.info("下载 Fed Funds Rate 历史...")
    data = fh_get("economic", {"code": "FEDFUNDS"})

    rows = []
    if data and isinstance(data, dict):
        for pt in (data.get("data") or []):
            d = str(pt.get("date") or pt.get("d") or "")[:10]
            v = pt.get("value") or pt.get("v")
            if d and v is not None:
                rows.append((d, float(v)))

    if not rows:
        # 2022-2026 真实利率历史兜底（月度快照）
        rows = [
            ("2022-01-01",0.08),("2022-03-01",0.33),("2022-05-01",0.83),
            ("2022-06-01",1.58),("2022-07-01",2.33),("2022-09-01",3.08),
            ("2022-11-01",3.83),("2022-12-01",4.33),("2023-02-01",4.57),
            ("2023-05-01",5.08),("2023-07-01",5.33),("2023-09-01",5.33),
            ("2024-01-01",5.33),("2024-09-01",5.33),("2024-10-01",4.83),
            ("2024-12-01",4.33),("2025-01-01",4.33),("2025-06-01",4.33),
            ("2026-01-01",4.33),("2026-05-01",4.33),
        ]
        log.warning("Finnhub 利率接口失败，使用内置历史数据")

    conn.executemany("INSERT OR IGNORE INTO macro_rates(date,fed_rate) VALUES(?,?)", rows)
    conn.commit()
    log.info(f"宏观利率入库 {len(rows)} 条 ✓")


# ══════════════════════════════════════════════════════════════
#  每日增量刷新（scheduler 调用）
# ══════════════════════════════════════════════════════════════

def daily_refresh(conn: sqlite3.Connection, lookback_days: int = 5) -> dict:
    """
    每日增量更新，在 run_scan() 之前调用：
      1. 最近 lookback_days 天价格（yfinance 批量，~3 min）
      2. Fed Funds Rate（Finnhub，秒级）
      3. 基本面刷新：每次最多 MAX_FUND_REFRESH 只，
         优先选最久未更新的 ticker（每次滚动，覆盖所有 3000+ 只约 15 天一轮）
    返回统计 dict。
    """
    MAX_FUND_REFRESH = 200   # 每天最多刷新 200 只基本面（Finnhub 60 req/min 限额内）
    today = date.today().isoformat()

    # ── 1. 增量价格 ────────────────────────────────────────────
    start    = (date.today() - timedelta(days=lookback_days)).isoformat()
    end_excl = (date.today() + timedelta(days=1)).isoformat()  # yfinance end 是排他的
    tickers = get_universe()
    log.info("每日增量价格刷新: %d 只, 起始 %s", len(tickers), start)

    price_rows = 0
    for b_start in range(0, len(tickers), YF_BATCH):
        batch = tickers[b_start: b_start + YF_BATCH]
        try:
            raw = yf.download(
                tickers=batch, start=start, end=end_excl,
                auto_adjust=True, progress=False,
            )
            if raw.empty:
                continue
            rows = []
            for ticker in batch:
                try:
                    df = raw.xs(ticker, axis=1, level=1) if len(batch) > 1 else raw
                    df = df.dropna(subset=["Close"])
                    for dt, row in df.iterrows():
                        rows.append((
                            ticker,
                            dt.strftime("%Y-%m-%d"),
                            _f(row.get("Open")),
                            _f(row.get("High")),
                            _f(row.get("Low")),
                            _f(row.get("Close")),
                            _f(row.get("Volume")),
                        ))
                except Exception:
                    pass
            if rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO prices(ticker,date,open,high,low,close,volume)"
                    " VALUES(?,?,?,?,?,?,?)", rows,
                )
                conn.commit()
                price_rows += len(rows)
        except Exception as e:
            log.warning("价格增量批次失败: %s", e)

    log.info("增量价格入库 %d 条 ✓", price_rows)

    # ── 2. 宏观利率 ────────────────────────────────────────────
    download_macro(conn)

    # ── 3. 基本面滚动刷新 ──────────────────────────────────────
    # 按最后更新时间升序排，每次取最陈旧的 MAX_FUND_REFRESH 只
    stale = [r[0] for r in conn.execute("""
        SELECT ticker FROM (
            SELECT ticker, MAX(filing_date) AS last_filed
            FROM fundamentals GROUP BY ticker
        )
        ORDER BY last_filed ASC
        LIMIT ?
    """, (MAX_FUND_REFRESH,)).fetchall()]

    fund_ok = 0
    for tk in stale:
        try:
            _fund_one(tk, conn)
            fund_ok += 1
        except Exception as e:
            log.warning("基本面刷新 %s 失败: %s", tk, e)

    log.info("基本面滚动刷新 %d 只 ✓", fund_ok)

    return {"price_rows": price_rows, "fund_refreshed": fund_ok}


# ══════════════════════════════════════════════════════════════
#  主函数
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="NASDAQ 历史数据下载器")
    parser.add_argument("--years", type=int, default=DEFAULT_YEARS,
                        help="回溯年数（默认 5）")
    parser.add_argument("--extend", action="store_true",
                        help="扩充模式：向前补充缺失的更早价格，不重置已完成的 ticker")
    parser.add_argument("--reset", action="store_true",
                        help="重置下载进度，重新下载全部数据")
    args = parser.parse_args()

    start_date = (date.today() - timedelta(days=args.years * 365 + 30)).isoformat()

    log.info("=" * 62)
    log.info(f"  NASDAQ 历史数据下载器")
    log.info(f"  周期: {start_date} → {END_DATE}  ({args.years} 年)")
    log.info(f"  数据库: {DB_PATH}")
    log.info("=" * 62)

    conn = init_db()

    if args.reset:
        log.info("重置下载进度...")
        conn.execute("DELETE FROM dl_progress")
        conn.commit()

    # 查看已有进度
    done_p = conn.execute("SELECT COUNT(*) FROM dl_progress WHERE price_ok=1").fetchone()[0]
    done_f = conn.execute("SELECT COUNT(*) FROM dl_progress WHERE fund_ok=1").fetchone()[0]

    # 获取宇宙
    tickers = get_universe()
    log.info(f"待下载: {len(tickers)} 只  已完成价格={done_p} 基本面={done_f}")

    # 宏观利率（快速，先下载）
    if conn.execute("SELECT COUNT(*) FROM macro_rates").fetchone()[0] < 5:
        download_macro(conn)

    # ── 阶段1: 价格（yfinance批量，快）────────────────────────
    log.info("\n── 阶段1: 下载价格数据 (yfinance, 约15-20分钟) ──")
    download_prices(tickers, conn, start_date, extend=args.extend)

    # ── 阶段2: 基本面（Finnhub逐只，含真实filedDate）──────────
    log.info("\n── 阶段2: 下载基本面数据 (Finnhub, 约60-90分钟) ──")
    download_fundamentals(tickers, conn)

    # 统计汇总
    n_p  = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    n_f  = conn.execute("SELECT COUNT(*) FROM fundamentals").fetchone()[0]
    n_tk = conn.execute("SELECT COUNT(DISTINCT ticker) FROM prices").fetchone()[0]
    conn.close()

    log.info("\n" + "=" * 62)
    log.info("  ✅ 下载完成！")
    log.info(f"     股票数量      : {n_tk}")
    log.info(f"     价格记录      : {n_p:,}")
    log.info(f"     基本面记录    : {n_f:,}")
    log.info(f"     数据库路径    : {DB_PATH}")
    log.info(f"\n  下一步: python3 historical_backtest.py")
    log.info("=" * 62)


if __name__ == "__main__":
    main()

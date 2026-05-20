#!/usr/bin/env python3
"""
edgar_supplement.py — SEC EDGAR 基本面历史补全
=================================================
补全 nasdaq_history.db 中 fundamentals 表的历史数据：
  · 数据来源：SEC EDGAR XBRL API（完全免费，无需 Key）
  · 覆盖深度：最早可追溯 10 年以上（默认 5 年）
  · 精确 filing_date：SEC 实际提交日期，而非估算值
  · 内容：季度收入、毛利率（从 10-Q 表单提取）

用法：
  python3 edgar_supplement.py               # 补全全部（默认 5 年）
  python3 edgar_supplement.py --years 5     # 显式指定 5 年
  python3 edgar_supplement.py --limit 50    # 快速测试前 50 只
  python3 edgar_supplement.py --reset       # 重置进度重新补全
  python3 edgar_supplement.py --ticker NVDA AMD MRVL  # 仅补全指定股票

SEC 速率限制：10 req/s。本脚本使用 0.15s 间隔（6.7 req/s），安全稳定。
"""

import os, sys, time, sqlite3, logging, argparse, json
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests

# ── 配置 ─────────────────────────────────────────────────────────────────
DATA_DIR   = Path(__file__).parent / "data"
DB_PATH    = DATA_DIR / "nasdaq_history.db"
LOG_PATH   = DATA_DIR / "edgar_supplement.log"

SEC_DELAY      = 0.15        # 秒，6.7 req/s（SEC 上限 10 req/s）
DEFAULT_YEARS  = 5           # 默认补全年数（命令行 --years 可覆盖）
USER_AGENT     = "nasdaq-hunter andy.wangyumin@gmail.com"

# START_DATE 在 main() 解析参数后设定；模块级提供默认值供直接 import 使用
START_DATE     = (date.today() - timedelta(days=DEFAULT_YEARS * 365)).isoformat()

# XBRL 收入 tag 优先级（找到第一个有数据的即用）
REV_TAGS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
    "RevenueFromContractWithCustomer",
    "RevenueFromContractWithCustomerNet",
    "NetRevenues",
    "TotalRevenues",
]
GP_TAG = "GrossProfit"

# ── 日志 ─────────────────────────────────────────────────────────────────
DATA_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_PATH, mode="a", encoding="utf-8"),
    ],
)
log = logging.getLogger("edgar")


# ══════════════════════════════════════════════════════════════
#  数据库
# ══════════════════════════════════════════════════════════════

def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS edgar_progress (
            ticker   TEXT PRIMARY KEY,
            done     INTEGER DEFAULT 0,
            rows_ins INTEGER DEFAULT 0,
            updated  TEXT
        )
    """)
    conn.commit()
    return conn


# ══════════════════════════════════════════════════════════════
#  SEC EDGAR API
# ══════════════════════════════════════════════════════════════

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})


def sec_get(url: str, retries: int = 3):
    for attempt in range(retries):
        try:
            r = _session.get(url, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None
            if r.status_code == 429:
                log.warning("SEC 限速，等待 30s")
                time.sleep(30)
                continue
            time.sleep(2 ** attempt)
        except Exception as e:
            if attempt == retries - 1:
                log.debug("sec_get %s: %s", url, e)
            time.sleep(2 ** attempt)
    return None


def get_cik_map() -> dict:
    """返回 ticker -> zero-padded CIK（10位）的映射"""
    log.info("正在下载 SEC ticker→CIK 映射表...")
    data = sec_get("https://www.sec.gov/files/company_tickers.json")
    if not data:
        log.error("无法获取 SEC CIK 映射表")
        sys.exit(1)
    result = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in data.values()}
    log.info("CIK 映射表加载完成: %d 家公司", len(result))
    return result


def fetch_company_facts(cik: str) -> dict:
    """下载公司全量 XBRL facts（包含所有财务数据）"""
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    data = sec_get(url)
    time.sleep(SEC_DELAY)
    if not data:
        return {}
    return data.get("facts", {}).get("us-gaap", {})


# ══════════════════════════════════════════════════════════════
#  XBRL 数据解析
# ══════════════════════════════════════════════════════════════

def _is_quarterly(entry: dict) -> bool:
    """判断一条 XBRL 记录是否为 ~3 个月的单季数据（排除 YTD 累计值）"""
    start = entry.get("start", "")
    end   = entry.get("end",   "")
    if not start or not end:
        return False
    try:
        days = (datetime.strptime(end, "%Y-%m-%d") -
                datetime.strptime(start, "%Y-%m-%d")).days
        return 60 <= days <= 105  # 3 个月区间
    except Exception:
        return False


def extract_revenue(facts: dict) -> dict:
    """
    从 facts 中提取季度收入记录。
    返回: {period_end_date: {"revenue": float, "filing_date": str}}

    Point-in-Time 关键：取该财季的【最早】filing_date（原始 10-Q），
    而非比较报告中的重复记录，确保回测不产生未来信息泄露。
    """
    for tag in REV_TAGS:
        if tag not in facts:
            continue
        entries = facts[tag].get("units", {}).get("USD", [])
        quarterly = [
            e for e in entries
            if e.get("form") in ("10-Q", "10-K")
            and e.get("end", "")[:10] >= START_DATE   # 按财季结束日期过滤
            and _is_quarterly(e)
        ]
        if not quarterly:
            continue
        result = {}
        for e in quarterly:
            period = e["end"][:10]
            filed  = e.get("filed", "")[:10]
            val    = e.get("val")
            if val is None or not filed:
                continue
            if period not in result:
                result[period] = {"revenue": float(val), "filing_date": filed}
            else:
                # 保留最早的 filing_date（原始 10-Q）；数据值取自同一来源
                if filed < result[period]["filing_date"]:
                    result[period] = {"revenue": float(val), "filing_date": filed}
        return result
    return {}


def extract_gross_profit(facts: dict) -> dict:
    """
    从 facts 中提取季度毛利润。
    返回: {period_end_date: float}
    """
    if GP_TAG not in facts:
        return {}
    entries = facts[GP_TAG].get("units", {}).get("USD", [])
    best = {}  # period -> {"gp": float, "filed": str}
    for e in entries:
        if e.get("form") not in ("10-Q", "10-K"):
            continue
        if e.get("end", "")[:10] < START_DATE:   # 按财季结束日期过滤
            continue
        if not _is_quarterly(e):
            continue
        period = e["end"][:10]
        filed  = e.get("filed", "")[:10]
        val    = e.get("val")
        if val is None or not filed:
            continue
        # 取最早 filed（原始 10-Q）保证 Point-in-Time
        if period not in best or filed < best[period]["filed"]:
            best[period] = {"gp": float(val), "filed": filed}
    return {period: info["gp"] for period, info in best.items()}


# ══════════════════════════════════════════════════════════════
#  FRED 宏观利率（替代已挂掉的 Finnhub economic 端点）
# ══════════════════════════════════════════════════════════════

def supplement_macro(conn: sqlite3.Connection) -> None:
    """从 FRED 补全 Fed Funds Rate 完整历史"""
    log.info("补全宏观利率：从 FRED 下载 FEDFUNDS 历史...")
    try:
        r = _session.get(
            "https://fred.stlouisfed.org/graph/fredgraph.csv?id=FEDFUNDS",
            timeout=30
        )
        r.raise_for_status()
        rows = []
        for line in r.text.strip().split("\n")[1:]:  # 跳过 header
            parts = line.strip().split(",")
            if len(parts) == 2 and parts[1] != ".":
                rows.append((parts[0], float(parts[1])))
        rows = [(d, v) for d, v in rows if d >= START_DATE]
        conn.executemany(
            "INSERT OR REPLACE INTO macro_rates(date, fed_rate) VALUES(?,?)",
            rows
        )
        conn.commit()
        log.info("宏观利率补全完成: %d 条月度记录 (%s ~ %s)",
                 len(rows), rows[0][0] if rows else "-", rows[-1][0] if rows else "-")
    except Exception as e:
        log.error("FRED 宏观利率下载失败: %s", e)


# ══════════════════════════════════════════════════════════════
#  单只股票处理
# ══════════════════════════════════════════════════════════════

def _recompute_rev_yoy(conn: sqlite3.Connection, ticker: str) -> None:
    """重新计算该 ticker 所有季度的 revenue_yoy（与 4 季前比较）"""
    rows = conn.execute(
        "SELECT period, revenue FROM fundamentals WHERE ticker=? ORDER BY period",
        (ticker,)
    ).fetchall()
    if len(rows) < 5:
        return
    for i, (period, rev) in enumerate(rows):
        if i >= 4:
            prior_rev = rows[i - 4][1]
            if prior_rev and prior_rev > 0 and rev is not None:
                yoy = (rev - prior_rev) / prior_rev * 100
                conn.execute(
                    "UPDATE fundamentals SET revenue_yoy=? WHERE ticker=? AND period=?",
                    (round(yoy, 2), ticker, period)
                )
    conn.commit()


def supplement_ticker(ticker: str, cik: str, conn: sqlite3.Connection) -> int:
    """
    补全单只股票的历史基本面数据。
    返回新插入的行数。
    """
    facts = fetch_company_facts(cik)
    if not facts:
        return 0

    rev_map = extract_revenue(facts)
    gp_map  = extract_gross_profit(facts)

    if not rev_map:
        return 0

    # 获取已有数据（避免覆盖 yfinance 下载的 EPS 信息）
    existing = {
        row[0]: {"eps_actual": row[1], "eps_estimate": row[2],
                 "eps_surprise": row[3], "beats_4q": row[4]}
        for row in conn.execute(
            "SELECT period, eps_actual, eps_estimate, eps_surprise, beats_4q "
            "FROM fundamentals WHERE ticker=?",
            (ticker,)
        ).fetchall()
    }

    rows_to_upsert = []
    for period, rev_info in sorted(rev_map.items()):
        rev       = rev_info["revenue"]
        filed     = rev_info["filing_date"]
        gp        = gp_map.get(period)
        gm        = (gp / rev * 100) if gp is not None and rev > 0 else None
        eps_data  = existing.get(period, {})

        rows_to_upsert.append((
            ticker,
            period,
            filed,
            rev,
            None,               # revenue_yoy：后面统一计算
            gm,
            eps_data.get("eps_actual"),
            eps_data.get("eps_estimate"),
            eps_data.get("eps_surprise"),
            eps_data.get("beats_4q"),
        ))

    if not rows_to_upsert:
        return 0

    conn.executemany("""
        INSERT OR REPLACE INTO fundamentals
        (ticker, period, filing_date, revenue, revenue_yoy, gross_margin,
         eps_actual, eps_estimate, eps_surprise, beats_4q)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, rows_to_upsert)
    conn.commit()

    # 重新计算 revenue_yoy（现在有了更多历史数据）
    _recompute_rev_yoy(conn, ticker)

    return len(rows_to_upsert)


# ══════════════════════════════════════════════════════════════
#  主流程
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="SEC EDGAR 基本面历史补全")
    parser.add_argument("--years",  type=int, default=DEFAULT_YEARS,
                        help=f"回溯年数（默认 {DEFAULT_YEARS}）")
    parser.add_argument("--limit",  type=int, default=0,  help="仅处理前 N 只（测试用）")
    parser.add_argument("--reset",  action="store_true",  help="重置 edgar 进度重新补全")
    parser.add_argument("--ticker", nargs="+",            help="仅补全指定股票")
    args = parser.parse_args()

    # 根据 --years 动态设定 START_DATE（覆盖模块级默认值）
    global START_DATE
    START_DATE = (date.today() - timedelta(days=args.years * 365)).isoformat()
    log.info("数据窗口: %s 年  起始日期: %s", args.years, START_DATE)

    if not DB_PATH.exists():
        log.error("数据库不存在: %s", DB_PATH)
        sys.exit(1)

    conn = init_db()

    if args.reset:
        conn.execute("DELETE FROM edgar_progress")
        conn.commit()
        log.info("Edgar 进度已重置")

    # ── 补全宏观利率 ──────────────────────────────────────────
    supplement_macro(conn)
    time.sleep(SEC_DELAY)

    # ── 获取 CIK 映射 ─────────────────────────────────────────
    cik_map = get_cik_map()
    time.sleep(SEC_DELAY)

    # ── 确定处理范围 ──────────────────────────────────────────
    if args.ticker:
        universe = [t.upper() for t in args.ticker]
    else:
        universe = [r[0] for r in conn.execute(
            "SELECT ticker FROM dl_progress ORDER BY ticker"
        ).fetchall()]

    done_set = {r[0] for r in conn.execute(
        "SELECT ticker FROM edgar_progress WHERE done=1"
    ).fetchall()}

    todo = [t for t in universe if t not in done_set and t in cik_map]
    if args.limit:
        todo = todo[:args.limit]

    skipped_no_cik = len([t for t in universe if t not in cik_map])
    log.info("=" * 60)
    log.info("SEC EDGAR 补全启动")
    log.info("  目标宇宙: %d 只", len(universe))
    log.info("  SEC 无 CIK（外国股/ETF）: %d 只（跳过）", skipped_no_cik)
    log.info("  已完成（跳过）: %d 只", len(done_set))
    log.info("  本次处理: %d 只", len(todo))
    log.info("  时间窗口: %s ~ %s", START_DATE, date.today().isoformat())
    log.info("=" * 60)

    total_rows = 0
    n_success  = 0
    n_empty    = 0
    n_error    = 0

    for i, ticker in enumerate(todo, 1):
        cik = cik_map[ticker]
        try:
            rows = supplement_ticker(ticker, cik, conn)
            conn.execute("""
                INSERT OR REPLACE INTO edgar_progress(ticker, done, rows_ins, updated)
                VALUES (?,1,?,?)
            """, (ticker, rows, datetime.utcnow().isoformat()))
            conn.commit()

            if rows > 0:
                total_rows += rows
                n_success  += 1
            else:
                n_empty += 1

            if i % 100 == 0 or i == len(todo):
                pct = i / len(todo) * 100
                done_tickers = conn.execute(
                    "SELECT count(*) FROM edgar_progress WHERE done=1"
                ).fetchone()[0]
                fund_total = conn.execute(
                    "SELECT count(*) FROM fundamentals"
                ).fetchone()[0]
                log.info("进度: %d/%d (%.0f%%)  有数据: %d 只  基本面总行数: %d",
                         i, len(todo), pct, n_success, fund_total)

        except KeyboardInterrupt:
            log.info("用户中断，已完成 %d 只，可重启续传", i - 1)
            break
        except Exception as e:
            log.warning("处理 %s 失败: %s", ticker, e)
            n_error += 1
            conn.execute("""
                INSERT OR REPLACE INTO edgar_progress(ticker, done, rows_ins, updated)
                VALUES (?,0,0,?)
            """, (ticker, datetime.utcnow().isoformat()))
            conn.commit()

    # ── 最终统计 ──────────────────────────────────────────────
    fund_final = conn.execute("SELECT count(*) FROM fundamentals").fetchone()[0]
    tickers_with_data = conn.execute(
        "SELECT count(distinct ticker) FROM fundamentals"
    ).fetchone()[0]
    macro_count = conn.execute("SELECT count(*) FROM macro_rates").fetchone()[0]

    log.info("=" * 60)
    log.info("EDGAR 补全完成")
    log.info("  处理股票: %d 只  有数据: %d  无EDGAR数据: %d  错误: %d",
             len(todo), n_success, n_empty, n_error)
    log.info("  新增/更新基本面行数: %d", total_rows)
    log.info("  fundamentals 表总计: %d 行 (%d 只股票)", fund_final, tickers_with_data)
    log.info("  macro_rates 表总计: %d 条", macro_count)
    log.info("=" * 60)

    conn.close()


if __name__ == "__main__":
    main()

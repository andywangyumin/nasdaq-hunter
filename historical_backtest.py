"""
historical_backtest.py — 5年历史走步验证引擎 v2.0
=================================================
三级度量体系：
  🌟 North Star : Alpha vs QQQ（策略年化收益 − QQQ年化收益，目标 >+5%）
  🚨 P0         : 精确率 ≥50%  |  真实误判率 <15%（排除系统性崩盘止损）
  📊 P1         : 平均提前量 >45天  |  同类优质召回率 >15%

性能优化：预加载全部价格+基本面+成交量到内存，避免数百万次 SQLite 查询。

用法：
  python3 historical_backtest.py
  python3 historical_backtest.py --start 2023-06-01
  python3 historical_backtest.py --dry-run           # 不推送 Lark
"""

import os, sys, json, sqlite3, logging, argparse, bisect, time
from datetime import date, timedelta, datetime
from pathlib import Path
from collections import defaultdict

try:
    import yfinance as yf
    HAS_YF = True
except ImportError:
    HAS_YF = False

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests

# ── 配置 ──────────────────────────────────────────────────────────────────
DB_PATH     = Path(__file__).parent / "data" / "nasdaq_history.db"
OUT_SIGNALS = Path(__file__).parent / "data" / "backtest_signals.json"
OUT_REPORT  = Path(__file__).parent / "data" / "backtest_report_3yr.txt"
FEISHU_HOOK = os.getenv("FEISHU_WEBHOOK",
              "https://open.larkoffice.com/open-apis/bot/v2/hook/f0f60b3c-c410-43af-8065-dab17318891f")

# DNA 模型参数（调优后版本 v2）
MIN_DNA      = 72
MIN_TREND    = 45
MIN_HIST     = 3       # 最少历史积累天数
BASE_THRESH  = 70
STOP_LOSS    = 0.20    # 高增速成长股波动性高，用20%止损（原15%触发过多假止损）
FRESH_DAYS   = 180     # 基本面超过180天视为过期
DEDUP_DAYS   = 180     # 同票180天内不重复发信号
REQUIRE_ACCEL = True   # 要求当季收入增速 > 上季增速×1.15（防止增速顶峰买入）
MIN_PRICE    = 5.0     # 最低股价（过滤便士股）
MAX_REV_YOY  = 800.0   # 收入增速上限（过滤低基数周转型噪音）
STOP_ON_CLOSE = True   # True=收盘价止损（避免盘中假突破），False=低价止损
# 史诗级跃升豁免：SNDK型困境反转（单季毛利率跳升≥20pp 且 增速极高）
# 阈值设高是为了只豁免真正"量级改变"的突破，而非普通季报好转
EPIC_GM_JUMP  = 20.0   # 毛利率单季跳升阈值（百分点，如 26%→51% = +25pp ✅，16%→30% = +14pp ❌）
EPIC_YOY_JUMP = 80.0   # 收入增速环比跳升阈值（百分点，150%→619% = +469pp ✅）
EPIC_MIN_DNA  = 75     # 豁免所需的最低DNA分（75允许无EPS数据的新上市公司触发）
EPIC_MAX_GM   = 200.0  # 毛利率上限（过滤 NVEC 888% 等数据错误）
MIN_AVG_DOLLAR_VOLUME = 1_000_000  # 流动性门：20日平均日成交额下限（$1M）
MIN_MED_DOLLAR_VOLUME = 500_000   # 流动性门：20日中位数日成交额下限（$0.5M，过滤单日spike虚高均值）
MIN_AVG_VOLUME_SHARES = 10_000    # 流动性门：20日平均日成交量下限（1万股，过滤高价微盘股如SINT $2348/share）
MAX_DV_SPIKE_RATIO    = 8         # 流动性门：avg/median比值上限（>8倍说明spike天主导，TNON型催化剂虚高）

# ── 度量体系 v2.0 ────────────────────────────────────────────────────
MARKET_CRASH_QQQ_DROP = 0.05   # QQQ 7日跌幅超过此阈值 → 系统性崩跌
MARKET_CRASH_VIX_LVL  = 30.0   # VIX 收盘超过此值 → 极度恐慌
LEAD_TIME_MILESTONE   = 0.50   # 提前量里程碑：信号后达到 +50% 涨幅的天数
PORTFOLIO_CAPITAL     = 100_000 # 组合模拟初始资金（$100K）

# ── V3.0 优化参数 ─────────────────────────────────────────────────────
CASH_YIELD_APY        = 0.05   # 闲置现金年化收益率（国债/货币市场）
CASH_DAILY_YIELD      = (1 + CASH_YIELD_APY) ** (1 / 252) - 1
TRAILING_STOP_TRIGGER = 0.50   # 最大浮盈达到 +50% 后启用追踪止盈
TRAILING_STOP_PCT     = 0.25   # 追踪止盈：从历史最高价回撤 25%（动态保护大涨利润）
HYPER_GROWTH_YOY      = 80     # 超高速增长豁免门槛（收入 YoY > 80%）
HYPER_GROWTH_MIN_GM   = 20     # 超高速增长豁免时允许的最低毛利率

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("backtest")


# ══════════════════════════════════════════════════════════════
#  数据预加载（核心性能优化：一次性把全部数据读入内存）
# ══════════════════════════════════════════════════════════════

def preload_prices(conn: sqlite3.Connection) -> dict:
    """
    加载全部价格数据。
    返回: {ticker: {date_str: (low, close)}}
    """
    t0 = time.time()
    log.info("预加载价格数据（约 220 万行）...")
    rows = conn.execute(
        "SELECT ticker, date, low, close, volume FROM prices ORDER BY ticker, date"
    ).fetchall()
    prices: dict = {}
    for ticker, dt, low, close, vol in rows:
        if close is None or close <= 0:
            continue
        if ticker not in prices:
            prices[ticker] = {}
        prices[ticker][dt] = (low if low else close, close, vol if vol else 0)
    log.info(f"价格预加载完成: {len(rows):,} 行, {len(prices)} 只股票 ({time.time()-t0:.1f}s)")
    return prices


def preload_fundamentals(conn: sqlite3.Connection) -> tuple:
    """
    加载全部基本面数据并建立检索索引。
    关键步骤：对 revenue_yoy 缺失的条目，从已有季报数据中自行计算
    （对比前一年同季度收入，避免依赖数据库中不完整的预计算值）。

    返回:
      funds      : {ticker: [fund_dict, ...]}  按 filing_date 升序
      fund_fdates: {ticker: [filing_date_str,...]}  用于 bisect
      prev_map   : {ticker: {period: prev_fund_dict or None}}  预计算上一季
    """
    t0 = time.time()
    log.info("预加载基本面数据...")
    cols = ["period", "filing_date", "revenue", "revenue_yoy", "gross_margin",
            "eps_actual", "eps_estimate", "eps_surprise", "beats_4q"]
    rows = conn.execute(f"""
        SELECT ticker, {','.join(cols)}
        FROM fundamentals
        ORDER BY ticker, period, filing_date
    """).fetchall()

    # 每只股票只保留每个 period 的最早 filing_date（Point-in-Time 正确）
    seen: dict = {}   # (ticker, period) -> first row
    for row in rows:
        key = (row[0], row[1])   # (ticker, period)
        if key not in seen:
            seen[key] = row

    funds: dict = {}
    for (ticker, period), row in seen.items():
        d = dict(zip(cols, row[1:]))
        if ticker not in funds:
            funds[ticker] = []
        funds[ticker].append(d)

    # 按 filing_date 排序（用于 PIT bisect）
    for fl in funds.values():
        fl.sort(key=lambda f: f["filing_date"])

    # ── 关键修复：对缺失 revenue_yoy 的条目，自行从前一年同季度计算 ──────
    filled = 0
    for ticker, fl in funds.items():
        by_period = sorted(fl, key=lambda f: f["period"])
        for i, f in enumerate(by_period):
            if f.get("revenue_yoy") is not None:
                continue
            if not f.get("revenue"):
                continue
            # 搜索前330-420天的同季度数据
            curr_dt = datetime.strptime(f["period"][:10], "%Y-%m-%d")
            for j in range(i - 1, max(-1, i - 8), -1):
                prev = by_period[j]
                if not prev.get("revenue"):
                    continue
                prev_dt = datetime.strptime(prev["period"][:10], "%Y-%m-%d")
                diff = (curr_dt - prev_dt).days
                if 300 <= diff <= 420:
                    pv = prev["revenue"]
                    if pv and pv > 0:
                        f["revenue_yoy"] = round(
                            (f["revenue"] - pv) / pv * 100, 1
                        )
                        filled += 1
                    break

    log.info(f"revenue_yoy 补算: {filled} 条  合计:{sum(len(fl) for fl in funds.values()):,}行")

    # 去重同季度重复记录（period 相差 <45 天 = 同一财季的日历/财务双版本）
    # 保留有 EPS 数据的版本；两者都有时保留 filing_date 更晚的（更完整）
    dedup_removed = 0
    for ticker in list(funds.keys()):
        fl = sorted(funds[ticker], key=lambda f: f["period"])
        deduped = [fl[0]]
        for f in fl[1:]:
            last = deduped[-1]
            d1 = datetime.strptime(last["period"][:10], "%Y-%m-%d")
            d2 = datetime.strptime(f["period"][:10], "%Y-%m-%d")
            if abs((d2 - d1).days) < 45:
                # 同一财季：选择数据更完整的那个
                last_has_eps = last.get("beats_4q") is not None
                cur_has_eps  = f.get("beats_4q") is not None
                if cur_has_eps and not last_has_eps:
                    deduped[-1] = f  # 替换为有 EPS 的版本
                elif cur_has_eps and last_has_eps and f["filing_date"] > last["filing_date"]:
                    deduped[-1] = f  # 两者都有 EPS 取更新的
                # else: 保留 existing
                dedup_removed += 1
            else:
                deduped.append(f)
        funds[ticker] = deduped
    if dedup_removed:
        log.info(f"去重同季度重复记录: -{dedup_removed} 条")

    # 建立 filing_date 列表（用于 bisect_right）
    fund_fdates: dict = {t: [f["filing_date"] for f in fl] for t, fl in funds.items()}

    # 预计算每只股票每个 period 的上一季度基本面（按 period 排序取前一个）
    prev_map: dict = {}
    for ticker, fl in funds.items():
        by_period = sorted(fl, key=lambda f: f["period"])
        prev_map[ticker] = {}
        for i, f in enumerate(by_period):
            prev_map[ticker][f["period"]] = by_period[i - 1] if i > 0 else None

    log.info(f"基本面预加载完成: {len(funds)} 只股票 ({time.time()-t0:.1f}s)")
    return funds, fund_fdates, prev_map


def preload_macro(conn: sqlite3.Connection) -> tuple:
    """返回 (sorted_dates_list, rates_list) 用于 bisect 查询"""
    rows = conn.execute("SELECT date, fed_rate FROM macro_rates ORDER BY date").fetchall()
    return [r[0] for r in rows], [r[1] for r in rows]


def preload_dv(prices: dict) -> dict:
    """预计算每只股票的日成交额和日成交量时序，用于20日滚动流动性检查。
    返回: {ticker: (sorted_dates_list, dv_values_list, vol_values_list)}
    """
    dv: dict = {}
    for ticker, daily in prices.items():
        items = sorted(
            [(dt, c * v, v) for dt, (l, c, v) in daily.items() if c and v],
            key=lambda x: x[0],
        )
        if items:
            dv[ticker] = (
                [x[0] for x in items],
                [x[1] for x in items],
                [x[2] for x in items],
            )
    return dv


def get_liquidity_stats(dv_data: dict, ticker: str, as_of: str) -> tuple:
    """计算 as_of 日之前（不含当日）20个交易日的 (avg_dv, median_dv, avg_shares)。
    用 bisect_left 排除当日，避免单日spike拉高均值误判流动性。
    """
    entry = dv_data.get(ticker)
    if not entry:
        return 0.0, 0.0, 0.0
    dates, dvs, vols = entry
    idx = bisect.bisect_left(dates, as_of) - 1  # 不含当日
    if idx < 0:
        return 0.0, 0.0, 0.0
    start = max(0, idx - 19)
    dv_chunk  = dvs[start : idx + 1]
    vol_chunk = vols[start : idx + 1]
    if not dv_chunk:
        return 0.0, 0.0, 0.0
    n = len(dv_chunk)
    avg_dv  = sum(dv_chunk) / n
    s = sorted(dv_chunk)
    mid = n // 2
    med_dv  = (s[mid - 1] + s[mid]) / 2 if n % 2 == 0 else s[mid]
    avg_vol = sum(vol_chunk) / n
    return avg_dv, med_dv, avg_vol


# ══════════════════════════════════════════════════════════════
#  市场基准数据（QQQ + VIX）
# ══════════════════════════════════════════════════════════════

def fetch_market_data(start: str, end: str) -> dict:
    """获取 QQQ 和 ^VIX 日线数据。
    优先从本地 DB 读 QQQ；VIX 通过 yfinance 获取。
    返回: {date_str: {"qqq": close_price, "vix": close_or_None}}
    """
    result: dict = {}

    # 先从本地 DB 拿 QQQ（如果有）
    try:
        conn = sqlite3.connect(str(DB_PATH))
        rows = conn.execute(
            "SELECT date, close FROM prices WHERE ticker='QQQ' AND date BETWEEN ? AND ? ORDER BY date",
            (start, end),
        ).fetchall()
        conn.close()
        for dt, c in rows:
            if c:
                result[dt] = {"qqq": float(c), "vix": None}
        if result:
            log.info(f"本地 QQQ 数据: {len(result)} 条")
    except Exception as e:
        log.warning(f"本地 QQQ 读取失败: {e}")

    # yfinance 补充 QQQ（本地不足时）
    if HAS_YF and len(result) < 100:
        try:
            hist = yf.download("QQQ", start=start, end=end, progress=False, auto_adjust=True)
            for idx_dt in hist.index:
                dt_str = idx_dt.strftime("%Y-%m-%d")
                close_val = float(hist.loc[idx_dt, "Close"])
                if dt_str not in result:
                    result[dt_str] = {"qqq": close_val, "vix": None}
                else:
                    result[dt_str]["qqq"] = close_val
            log.info(f"yfinance QQQ: {len(result)} 条")
        except Exception as e:
            log.warning(f"yfinance QQQ 失败: {e}")

    # yfinance 拉 VIX（用于崩跌判定）
    if HAS_YF:
        try:
            vix_hist = yf.download("^VIX", start=start, end=end, progress=False, auto_adjust=True)
            loaded = 0
            for idx_dt in vix_hist.index:
                dt_str = idx_dt.strftime("%Y-%m-%d")
                vv = float(vix_hist.loc[idx_dt, "Close"])
                if dt_str in result:
                    result[dt_str]["vix"] = vv
                else:
                    result[dt_str] = {"qqq": None, "vix": vv}
                loaded += 1
            log.info(f"yfinance VIX: {loaded} 条")
        except Exception as e:
            log.warning(f"yfinance VIX 失败（崩跌检测不可用）: {e}")

    if not result:
        log.warning("未能获取 QQQ/VIX 数据，Alpha 对比和崩跌检测将不可用")
    return result


def build_qqq_weekly_returns(market_data: dict) -> dict:
    """计算 QQQ 7日滚动跌幅，用于系统性崩跌判定。
    返回: {date_str: weekly_return}  (负数=下跌)
    """
    dates = sorted(d for d in market_data if market_data[d].get("qqq"))
    result: dict = {}
    for i, dt in enumerate(dates):
        target = (datetime.strptime(dt, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
        prev_i = bisect.bisect_left(dates, target)
        prev_dt = dates[max(0, prev_i - 1)]
        p0 = market_data[prev_dt]["qqq"]
        p1 = market_data[dt]["qqq"]
        if p0 and p0 > 0 and prev_dt != dt:
            result[dt] = (p1 - p0) / p0
    return result


def is_market_crash(date_str: str, market_data: dict, qqq_weekly: dict) -> bool:
    """判断该日是否处于系统性市场崩跌（QQQ 7日跌>5% 或 VIX>30）。"""
    entry = market_data.get(date_str, {})
    vix = entry.get("vix")
    if vix and vix > MARKET_CRASH_VIX_LVL:
        return True
    wret = qqq_weekly.get(date_str)
    if wret is not None and wret < -MARKET_CRASH_QQQ_DROP:
        return True
    return False


# ══════════════════════════════════════════════════════════════
#  内存内点查函数
# ══════════════════════════════════════════════════════════════

def pit_fund(funds_list: list, fdates_list: list, as_of: str):
    """Point-in-Time：取 filing_date <= as_of 的最新一条基本面"""
    idx = bisect.bisect_right(fdates_list, as_of) - 1
    return funds_list[idx] if idx >= 0 else None


def pit_fed(macro_dates: list, macro_rates: list, as_of: str) -> float:
    idx = bisect.bisect_right(macro_dates, as_of) - 1
    return macro_rates[idx] if idx >= 0 else 4.5


# ══════════════════════════════════════════════════════════════
#  DNA 评分逻辑（与 scanner_v2.py 保持完全一致）
# ══════════════════════════════════════════════════════════════

def score_margin(gm) -> int:
    if gm is None or gm <= 0: return 0
    if gm > 70: return 100
    if gm > 55: return 82
    if gm > 40: return 62
    if gm > 25: return 38
    if gm > 15: return 18
    return 5


def score_rev_accel(rev_yoy, prev_rev_yoy) -> int:
    if rev_yoy is None or rev_yoy <= 0: return 0
    accel = prev_rev_yoy is not None and rev_yoy > prev_rev_yoy * 1.15
    base = (100 if rev_yoy > 200 else 88 if rev_yoy > 100 else
            68 if rev_yoy > 50 else 45 if rev_yoy > 30 else
            28 if rev_yoy > 20 else 10)
    return min(100, base + (12 if accel else 0))


def score_beats(beats_4q, latest_surprise) -> int:
    n = beats_4q or 0
    mag = latest_surprise or 0.0
    base = (100 if n >= 4 else 80 if n == 3 else 55 if n == 2 else 25 if n == 1 else 0)
    bonus = (20 if mag > 50 else 12 if mag > 25 else 5 if mag > 10 else 0)
    return min(100, base + bonus)


def compute_dna(fund: dict, prev_fund) -> tuple:
    gm      = fund.get("gross_margin")
    rev_yoy = fund.get("revenue_yoy")
    beats   = fund.get("beats_4q")
    surp    = fund.get("eps_surprise")
    prev_yoy = (prev_fund or {}).get("revenue_yoy")

    s_margin = score_margin(gm)
    s_rev    = score_rev_accel(rev_yoy, prev_yoy)
    s_beats  = score_beats(beats, surp)

    beats_missing = (beats is None and surp is None)

    if beats_missing:
        # 仅用两个有数据的维度（归一化到100）
        raw_dna = (s_margin * 24 + s_rev * 26) / 50
        failed = []
        if s_margin < 40: failed.append(f"GM({s_margin})")
        if s_rev    < 45: failed.append(f"RevAccel({s_rev})")
    else:
        raw_dna = (s_margin * 24 + s_rev * 26 + s_beats * 22) / 72
        failed = []
        if s_margin < 40: failed.append(f"GM({s_margin})")
        if s_rev    < 45: failed.append(f"RevAccel({s_rev})")
        if s_beats  < 35: failed.append(f"Beats({s_beats})")

    dna = round(raw_dna * 0.5 if failed else raw_dna)
    return dna, failed


def compute_trend(dna_history: list) -> int:
    if len(dna_history) < 2: return 0
    recent = dna_history[-10:]
    n  = len(recent)
    xm = (n - 1) / 2
    ym = sum(recent) / n
    num = sum((i - xm) * (y - ym) for i, y in enumerate(recent))
    den = sum((i - xm) ** 2 for i in range(n))
    slope = num / den if den else 0.0

    streak = 0
    for v in reversed(recent):
        if v >= 55: streak += 1
        else: break

    delta = recent[-1] - recent[0]
    return min(100, max(0,
        (35 if slope > 4 else 22 if slope > 2 else 12 if slope > 0.5 else 0) +
        (30 if streak >= 7 else 20 if streak >= 5 else 10 if streak >= 3 else 0) +
        (25 if delta > 25 else 15 if delta > 12 else 8 if delta > 5 else 0)
    ))


def compute_composite(dna: int, trend: int, hist_len: int, is_epic: bool = False) -> int:
    if is_epic:
        # 史诗跃升：基本面突变本身即最强趋势信号，直接取DNA分，跳过历史天数降权
        return dna
    if hist_len < 2: return round(dna * 0.45)
    if hist_len < 5: return round(dna * 0.6 + trend * 0.4)
    return round(dna * 0.5 + trend * 0.5)


def is_epic_turnaround(fund: dict, prev_fund) -> bool:
    """单季基本面史诗级跃升检测（SNDK型困境反转冷启动豁免）。
    触发条件：(毛利率单季+15pp 或 收入增速环比+30pp) AND DNA≥EPIC_MIN_DNA。
    豁免效果：绕过 MIN_HIST 和 REQUIRE_ACCEL 两道门控。
    """
    if prev_fund is None:
        return False
    cur_gm   = fund.get("gross_margin")
    prev_gm  = prev_fund.get("gross_margin")
    cur_yoy  = fund.get("revenue_yoy")
    prev_yoy = prev_fund.get("revenue_yoy")
    if any(v is None for v in [cur_gm, prev_gm, cur_yoy, prev_yoy]):
        return False
    if cur_gm > EPIC_MAX_GM:   # 数据错误保护
        return False
    gm_jump  = cur_gm - prev_gm
    yoy_jump = cur_yoy - prev_yoy
    # SNDK路径：从低毛利率跨越到高毛利率（如 26%→51%，prev<40，cur≥40，jump≥20pp）
    # 不包括本已高毛利的生物医药股季度波动（如 75%→95%，prev已很高，不算困境反转）
    sndk_path = prev_gm < 40 and cur_gm >= 40 and gm_jump >= EPIC_GM_JUMP
    # ALAB路径：收入爆炸式加速且绝对增速极高（150%→619%，jump≥80pp，cur≥200%，gm≥50%）
    alab_path = yoy_jump >= EPIC_YOY_JUMP and cur_yoy >= 200 and cur_gm >= 50
    return sndk_path or alab_path


def macro_threshold(fed_rate: float) -> int:
    if fed_rate > 5.0: return 88
    if fed_rate > 4.5: return 83
    return BASE_THRESH


def l1_gate(fund: dict, price: float) -> tuple:
    gm      = fund.get("gross_margin") or 0
    rev_yoy = fund.get("revenue_yoy") or 0
    # 超高速增长豁免：收入 YoY > 80% 时允许毛利低至 20%（以市场份额换利润的阶段）
    min_gm  = HYPER_GROWTH_MIN_GM if rev_yoy >= HYPER_GROWTH_YOY else 40
    if gm < min_gm:          return False, f"毛利不足({gm:.1f}%,需>{min_gm}%)"
    if rev_yoy < 50:         return False, f"增速不足({rev_yoy:.0f}%)"
    if rev_yoy > MAX_REV_YOY: return False, f"增速异常({rev_yoy:.0f}%,可能低基数)"
    if price < MIN_PRICE:    return False, f"价格过低(${price:.2f})"
    if price > 3000:         return False, f"价格无效(${price})"
    return True, ""


# ══════════════════════════════════════════════════════════════
#  走步回测主循环（全内存，无 SQLite 查询）
# ══════════════════════════════════════════════════════════════

def run_walkforward(prices: dict, funds: dict, fund_fdates: dict,
                    prev_map: dict, macro_dates: list, macro_rates: list,
                    dv_data: dict, start: str, end: str) -> list:
    # 从价格数据获取交易日历
    all_dates = sorted({dt for p in prices.values() for dt in p})
    trading_dates = [d for d in all_dates if start <= d <= end]
    all_tickers   = sorted(prices.keys())

    log.info(f"走步回测: {len(trading_dates)} 个交易日 × {len(all_tickers)} 只股票")
    log.info(f"日期范围: {trading_dates[0]} → {trading_dates[-1]}")

    dna_history: dict = defaultdict(list)
    last_signal: dict = {}
    signals = []
    report_every = max(1, len(trading_dates) // 20)
    t0 = time.time()

    for day_idx, cur_date in enumerate(trading_dates):
        if day_idx % report_every == 0:
            pct  = day_idx / len(trading_dates) * 100
            ela  = time.time() - t0
            eta  = ela / max(day_idx, 1) * (len(trading_dates) - day_idx)
            log.info(f"  [{pct:5.1f}%] {cur_date}  信号数:{len(signals)}  "
                     f"耗时:{ela:.0f}s  预计剩余:{eta:.0f}s")

        fed_rate  = pit_fed(macro_dates, macro_rates, cur_date)
        threshold = macro_threshold(fed_rate)

        for ticker in all_tickers:
            # 当日价格
            ticker_prices = prices.get(ticker)
            if not ticker_prices:
                continue
            pd = ticker_prices.get(cur_date)
            if not pd:
                continue
            low_price, close_price, _ = pd
            if close_price <= 0:
                continue

            # Point-in-Time 基本面
            fl = funds.get(ticker)
            if not fl:
                continue
            fund = pit_fund(fl, fund_fdates[ticker], cur_date)
            if not fund:
                continue

            # 数据新鲜度
            days_stale = (datetime.strptime(cur_date, "%Y-%m-%d") -
                          datetime.strptime(fund["filing_date"], "%Y-%m-%d")).days
            if days_stale > FRESH_DAYS:
                continue

            # Quality 质检门
            ok, _ = l1_gate(fund, close_price)
            if not ok:
                continue

            # DNA 评分
            prev_fund = prev_map.get(ticker, {}).get(fund["period"])
            dna, failed = compute_dna(fund, prev_fund)
            dna_history[ticker].append(dna)

            # 史诗级跃升检测（SNDK型困境反转，豁免冷启动限制）
            epic = is_epic_turnaround(fund, prev_fund) and dna >= EPIC_MIN_DNA

            # Outlook 预期门（可选）：当季增速 > 上季×1.15；史诗跃升豁免（绝对提升已极高）
            if REQUIRE_ACCEL and not epic and prev_fund is not None:
                prev_yoy = prev_fund.get("revenue_yoy")
                cur_yoy  = fund.get("revenue_yoy")
                if prev_yoy is not None and cur_yoy is not None:
                    if cur_yoy <= prev_yoy * 1.15:
                        continue

            # Signal 信号门；史诗跃升豁免 MIN_HIST（单季跃升本身已是最强信号）
            hist_len = len(dna_history[ticker])
            if (not epic and hist_len < MIN_HIST) or dna < MIN_DNA:
                continue

            trend = compute_trend(dna_history[ticker])
            # 史诗跃升：赋予初始趋势分65（基本面突变即趋势），否则按正常逻辑判断
            if epic and trend < MIN_TREND:
                trend = 65

            comp = compute_composite(dna, trend, hist_len, is_epic=epic)
            if comp < threshold:
                continue

            # P0 流动性四维门：avg<$1M | median<$500K | avg_shares<1万 | spike比>8 → 跳过
            _avg_dv, _med_dv, _avg_vol = get_liquidity_stats(dv_data, ticker, cur_date)
            _spike = _avg_dv / _med_dv if _med_dv > 0 else 999
            if (_avg_dv  < MIN_AVG_DOLLAR_VOLUME
                    or _med_dv  < MIN_MED_DOLLAR_VOLUME
                    or _avg_vol < MIN_AVG_VOLUME_SHARES
                    or _spike   > MAX_DV_SPIKE_RATIO):
                continue

            # 去重
            last = last_signal.get(ticker)
            if last:
                gap = (datetime.strptime(cur_date, "%Y-%m-%d") -
                       datetime.strptime(last, "%Y-%m-%d")).days
                if gap < DEDUP_DAYS:
                    continue

            last_signal[ticker] = cur_date
            signals.append({
                "date":        cur_date,
                "ticker":      ticker,
                "entry":       round(close_price, 2),
                "dna":         dna,
                "trend":       trend,
                "comp":        comp,
                "threshold":   threshold,
                "fed_rate":    fed_rate,
                "gm":          fund.get("gross_margin"),
                "rev_yoy":     fund.get("revenue_yoy"),
                "eps_sur":     fund.get("eps_surprise"),
                "beats_4q":    fund.get("beats_4q"),
                "filing_date": fund["filing_date"],
                "epic":        epic,
                "avg_dv_m":    round(_avg_dv / 1e6, 2),
                "med_dv_m":    round(_med_dv / 1e6, 2),
                "avg_vol_k":   round(_avg_vol / 1e3, 1),
            })

    log.info(f"走步模拟完成，共触发 {len(signals)} 个信号  总耗时:{time.time()-t0:.1f}s")
    return signals


# ══════════════════════════════════════════════════════════════
#  信号结果评估（内存内）
# ══════════════════════════════════════════════════════════════

def evaluate_signals(signals: list, prices: dict,
                     market_data: dict, qqq_weekly: dict) -> list:
    today_str = date.today().isoformat()
    log.info(f"评估 {len(signals)} 个信号的真实走势...")

    for sig in signals:
        ticker     = sig["ticker"]
        entry      = sig["entry"]
        entry_date = sig["date"]
        stop_price = entry * (1 - STOP_LOSS)

        end_check = _add_days(entry_date, 420)
        ticker_prices = prices.get(ticker, {})

        future_dates = sorted(
            d for d in ticker_prices if entry_date <= d <= min(end_check, today_str)
        )

        sig.update({
            "stop_hit": False, "stop_date": None, "stop_type": None,
            "ret_1m": None, "ret_3m": None, "ret_6m": None, "ret_12m": None,
            "max_ret": None, "outcome": "OPEN",
        })

        if not future_dates:
            continue

        max_close        = entry
        stop_price       = entry * (1 - STOP_LOSS)   # 初始止损 -20%
        trailing_locked  = False                      # 已触发追踪止盈？

        for i, dt in enumerate(future_dates):
            low_p, close_p, _ = ticker_prices[dt]
            if close_p and close_p > max_close:
                max_close = close_p
            # 浮盈≥+50% → 切换追踪止盈（从历史高点回撤25%，但不低于入场价）
            if max_close >= entry * (1 + TRAILING_STOP_TRIGGER):
                trail = max(entry, max_close * (1 - TRAILING_STOP_PCT))
                stop_price     = max(stop_price, trail)   # 止损只升不降
                trailing_locked = True
            chk_price = close_p if STOP_ON_CLOSE else low_p
            if not sig["stop_hit"] and chk_price is not None and chk_price <= stop_price:
                sig["stop_hit"]  = True
                sig["stop_date"] = dt
                if trailing_locked:
                    # 保本止盈触发：锁定 +10% 利润，计为 WIN
                    sig["stop_type"] = "PROFIT_LOCK"
                else:
                    crash = is_market_crash(dt, market_data, qqq_weekly) if market_data else False
                    sig["stop_type"] = "MARKET_CRASH" if crash else "WRONG_PICK"
            if close_p and close_p > 0:
                ret = (close_p - entry) / entry * 100
                if sig["ret_1m"]  is None and i >= 20:  sig["ret_1m"]  = round(ret, 1)
                if sig["ret_3m"]  is None and i >= 60:  sig["ret_3m"]  = round(ret, 1)
                if sig["ret_6m"]  is None and i >= 125: sig["ret_6m"]  = round(ret, 1)
                if sig["ret_12m"] is None and i >= 250: sig["ret_12m"] = round(ret, 1)

        sig["max_ret"] = round((max_close - entry) / entry * 100, 1)

        days_since = (_today_dt() - datetime.strptime(entry_date, "%Y-%m-%d")).days
        if sig.get("stop_type") == "PROFIT_LOCK":
            # 保本止盈：锁住正收益，永远计 WIN（不受 12M 未满限制）
            sig["outcome"] = "WIN"
        elif days_since < 365:
            sig["outcome"] = "OPEN"
        elif sig["stop_hit"]:
            sig["outcome"] = "STOP"
        elif sig["ret_12m"] is not None and sig["ret_12m"] > 0:
            sig["outcome"] = "WIN"
        else:
            sig["outcome"] = "LOSS"

    return signals


# ══════════════════════════════════════════════════════════════
#  遗漏机会识别（内存内）
# ══════════════════════════════════════════════════════════════

def find_missed_breakouts(prices: dict, signals: list,
                          start: str, end: str) -> list:
    log.info("识别遗漏的100%+爆发股...")
    signaled = {s["ticker"] for s in signals}
    missed   = []

    for ticker, ticker_prices in prices.items():
        period_data = [(d, c) for d, (l, c, v) in ticker_prices.items()
                       if start <= d <= end and c and c > 0]
        if len(period_data) < 50:
            continue
        period_data.sort()
        p0 = period_data[0][1]
        if not p0 or p0 <= 0:
            continue
        max_close = max(c for _, c in period_data)
        max_ret   = (max_close - p0) / p0 * 100

        if max_ret > 100 and ticker not in signaled:
            missed.append({
                "ticker":      ticker,
                "max_ret":     round(max_ret, 1),
                "start_price": round(p0, 2),
                "max_price":   round(max_close, 2),
            })

    missed.sort(key=lambda x: -x["max_ret"])
    log.info(f"发现 {len(missed)} 只遗漏的爆发股（涨幅>100%但未发信号）")
    return missed


# ══════════════════════════════════════════════════════════════
#  P1 辅助计算
# ══════════════════════════════════════════════════════════════

def compute_lead_times(signals: list, prices: dict) -> dict:
    """P1 平均提前量：信号日到首次达到 +50% 里程碑的天数均值。"""
    lead_days = []
    for sig in signals:
        ticker, entry, entry_date = sig["ticker"], sig["entry"], sig["date"]
        target = entry * (1 + LEAD_TIME_MILESTONE)
        future = sorted(d for d in prices.get(ticker, {}) if d >= entry_date)
        for dt in future:
            _, cp, _ = prices[ticker][dt]
            if cp and cp >= target:
                delta = (datetime.strptime(dt, "%Y-%m-%d") -
                         datetime.strptime(entry_date, "%Y-%m-%d")).days
                lead_days.append(delta)
                break
    avg = round(sum(lead_days) / len(lead_days)) if lead_days else 0
    return {"avg_days": avg, "count": len(lead_days), "total": len(signals)}


def compute_quality_cohort_recall(signals: list, missed: list, funds: dict) -> dict:
    """P1 同类优质召回率：在"L1达标且涨幅>100%"宇宙中的命中率。
    L1达标 = 历史上曾有一个季度同时满足 GM≥40% 且 rev_yoy≥50%。
    """
    def passes_l1(ticker: str) -> bool:
        for f in funds.get(ticker, []):
            gm  = f.get("gross_margin") or 0
            yoy = f.get("revenue_yoy")  or 0
            min_gm = HYPER_GROWTH_MIN_GM if yoy >= HYPER_GROWTH_YOY else 40
            if gm >= min_gm and yoy >= 50:
                return True
        return False

    # 命中标准：已确认 max_ret>100%，或 OPEN 信号浮盈≥50%（强势进行中）
    captured = [
        s["ticker"] for s in signals
        if (s.get("max_ret") or 0) > 100
        or (s["outcome"] == "OPEN" and (s.get("max_ret") or 0) >= 50)
    ]
    quality_missed = [m["ticker"] for m in missed if passes_l1(m["ticker"])]
    numerator   = len(set(captured))
    denominator = numerator + len(set(quality_missed))
    recall_pct  = numerator / denominator * 100 if denominator else 0
    return {
        "pct":              round(recall_pct, 1),
        "captured":         numerator,
        "quality_missed":   len(set(quality_missed)),
        "denominator":      denominator,
    }


def simulate_portfolio(signals: list, prices: dict, start_dt: str = "") -> dict:
    """V6.0 组合模拟：无持仓上限 + 完整层级抢占 + deployed_keys 追踪。
    - 唯一限制：idle_cash（不设 MAX_POSITIONS 槽位）
    - STRONG：现金不足时踢最差 WEAK；若无 WEAK，踢最差 MEDIUM
    - MEDIUM：现金不足时只踢最差 WEAK
    - WEAK：无抢占权，有钱则买，无钱跳过
    - deployed_keys：记录实际入场的 (ticker, date)，供 P0/P1 指标过滤
    """
    def tier(comp: int) -> str:
        return "STRONG" if comp >= 85 else ("MEDIUM" if comp >= 75 else "WEAK")

    def nav_frac(comp: int) -> float:
        return 0.40 if comp >= 85 else (0.20 if comp >= 75 else 0.10)

    def hard_stop(pos: dict) -> float:
        return pos["entry"] * (0.90 if pos.get("comp", 75) < 75 else (1 - STOP_LOSS))

    def cur_nav() -> float:
        n = cash
        for t, p in positions.items():
            pd_ = prices.get(t, {}).get(cur_date)
            _, cp_, _ = pd_ if pd_ else (None, None, None)
            n += p["shares"] * (cp_ if cp_ else p["entry"])
        return n

    def unrealized(tkr_: str, pos_: dict) -> float:
        pd_ = prices.get(tkr_, {}).get(cur_date)
        _, cp_, _ = pd_ if pd_ else (None, None, None)
        return (cp_ - pos_["entry"]) / pos_["entry"] if cp_ else 0.0

    def evict_worst(tier_filter: str) -> bool:
        """踢出 tier_filter 层中浮盈最差的仓位，返回是否成功。"""
        candidates = {t: p for t, p in positions.items()
                      if tier(p.get("comp", 75)) == tier_filter}
        if not candidates:
            return False
        nonlocal cash
        evict_tkr = min(candidates, key=lambda t: unrealized(t, positions[t]))
        ep = prices.get(evict_tkr, {}).get(cur_date)
        _, cp_ev, _ = ep if ep else (None, None, None)
        if cp_ev:
            cash += positions[evict_tkr]["shares"] * cp_ev
            del positions[evict_tkr]
        else:
            del positions[evict_tkr]   # 无价格时按入场价清仓（保守）
        return True

    if not signals:
        return {}
    sorted_sigs = sorted(signals, key=lambda s: (s["date"], -s.get("comp", 75)))
    all_dates   = sorted({dt for p in prices.values() for dt in p})
    if not start_dt:
        start_dt = sorted_sigs[0]["date"]

    cash         = float(PORTFOLIO_CAPITAL)
    positions: dict = {}
    deployed_keys: set = set()   # (ticker, signal_date) 实际入场记录
    sig_idx   = 0
    daily_vals: list = []

    for cur_date in (d for d in all_dates if d >= start_dt):
        # ① 检查持仓退出（追踪止盈 / 分层硬止损 / 12M）
        to_exit = []
        for tkr, pos in positions.items():
            pd = prices.get(tkr, {}).get(cur_date)
            if not pd:
                continue
            _, cp, _ = pd
            if not cp:
                continue
            if cp > pos["max_cp"]:
                pos["max_cp"] = cp
            if pos["max_cp"] >= pos["entry"] * (1 + TRAILING_STOP_TRIGGER):
                stop_p = max(pos["entry"], pos["max_cp"] * (1 - TRAILING_STOP_PCT))
            else:
                stop_p = hard_stop(pos)
            days_hld = (datetime.strptime(cur_date, "%Y-%m-%d") -
                        datetime.strptime(pos["entry_date"], "%Y-%m-%d")).days
            if cp <= stop_p:
                cash += pos["shares"] * max(cp, stop_p);  to_exit.append(tkr)
            elif days_hld >= 365:
                cash += pos["shares"] * cp;               to_exit.append(tkr)
        for tkr in to_exit:
            del positions[tkr]

        # 闲置现金生息（5% APY）
        cash *= (1 + CASH_DAILY_YIELD)

        # ② 建仓：无槽位限制，现金驱动 + 完整层级抢占（每信号至多踢一次）
        nav = cur_nav()

        while sig_idx < len(sorted_sigs) and sorted_sigs[sig_idx]["date"] <= cur_date:
            sig = sorted_sigs[sig_idx];  sig_idx += 1
            tkr = sig["ticker"]
            if tkr in positions:
                continue

            comp     = sig.get("comp", 75)
            sig_tier = tier(comp)
            frac     = nav_frac(comp)
            need     = nav * frac   # 目标仓位额度

            # 现金不足时：按层级抢占（每信号至多踢一次）
            if cash * 0.98 < need:
                if sig_tier == "STRONG":
                    if not evict_worst("WEAK"):
                        evict_worst("MEDIUM")   # 无弱仓则踢中仓
                elif sig_tier == "MEDIUM":
                    evict_worst("WEAK")         # 仅踢弱仓
                # WEAK 无抢占权，直接用剩余现金
                nav = cur_nav()

            deploy = min(nav * frac, cash * 0.98)
            if deploy < 10:
                continue
            positions[tkr] = {
                "shares":     deploy / sig["entry"],
                "entry":      sig["entry"],
                "entry_date": sig["date"],
                "max_cp":     sig["entry"],
                "comp":       comp,
            }
            cash -= deploy
            deployed_keys.add((tkr, sig["date"]))

        # ③ 逐日盯市
        mkt = cash
        for tkr, pos in positions.items():
            pd = prices.get(tkr, {}).get(cur_date)
            _, cp, _ = pd if pd else (None, None, None)
            mkt += pos["shares"] * (cp if cp else pos["entry"])
        daily_vals.append((cur_date, mkt))

    if not daily_vals:
        return {}
    v1   = daily_vals[-1][1]
    days = (datetime.strptime(daily_vals[-1][0], "%Y-%m-%d") -
            datetime.strptime(daily_vals[0][0],  "%Y-%m-%d")).days
    total_ret = (v1 - PORTFOLIO_CAPITAL) / PORTFOLIO_CAPITAL
    annual_ret = ((1 + total_ret) ** (365 / max(days, 1))) - 1 if days > 0 else 0
    return {
        "start":          daily_vals[0][0],
        "end":            daily_vals[-1][0],
        "final":          round(v1),
        "total_ret_pct":  round(total_ret  * 100, 1),
        "annual_ret_pct": round(annual_ret * 100, 1),
        "days":           days,
        "deployed_keys":  deployed_keys,
    }


def compute_qqq_return(market_data: dict, start: str, end: str) -> dict:
    """QQQ 同期买入持有年化收益"""
    dates = sorted(d for d in market_data if market_data[d].get("qqq") and start <= d <= end)
    if len(dates) < 2:
        return {}
    p0, p1 = market_data[dates[0]]["qqq"], market_data[dates[-1]]["qqq"]
    days = (datetime.strptime(dates[-1], "%Y-%m-%d") -
            datetime.strptime(dates[0],  "%Y-%m-%d")).days
    total_ret = (p1 - p0) / p0
    annual_ret = ((1 + total_ret) ** (365 / max(days, 1))) - 1
    return {
        "start": dates[0],  "end": dates[-1],
        "p0": round(p0, 2), "p1": round(p1, 2),
        "total_ret_pct":  round(total_ret  * 100, 1),
        "annual_ret_pct": round(annual_ret * 100, 1),
        "days": days,
    }


# ══════════════════════════════════════════════════════════════
#  统计计算
# ══════════════════════════════════════════════════════════════

def compute_stats(signals: list, missed: list,
                  funds: dict = None, prices: dict = None,
                  market_data: dict = None,
                  backtest_start: str = "", backtest_end: str = "") -> dict:
    # 组合模拟先跑（V6.0：deployed_keys 用于过滤 P0/P1 基数）
    portfolio = (simulate_portfolio(signals, prices, backtest_start)
                 if prices else {})
    qqq_ret   = (compute_qqq_return(market_data, backtest_start, backtest_end)
                 if market_data else {})
    alpha = (portfolio.get("annual_ret_pct", 0) - qqq_ret.get("annual_ret_pct", 0)
             if portfolio and qqq_ret else None)

    deployed_keys = portfolio.pop("deployed_keys", set())
    # metric_sigs：仅统计实际入场的信号；若 portfolio 未跑则回退到全部
    metric_sigs = (
        [s for s in signals if (s["ticker"], s["date"]) in deployed_keys]
        if deployed_keys else signals
    )
    n_deployed = len(deployed_keys)

    # P0/P1 均基于 metric_sigs（实际交易）
    closed = [s for s in metric_sigs if s["outcome"] in ("WIN", "STOP", "LOSS")]
    open_  = [s for s in metric_sigs if s["outcome"] == "OPEN"]
    wins   = [s for s in closed if s["outcome"] == "WIN"]
    stops  = [s for s in closed if s["outcome"] == "STOP"]
    losses = [s for s in closed if s["outcome"] == "LOSS"]

    n_closed  = len(closed)
    precision = len(wins)  / n_closed * 100 if n_closed else 0
    stop_rate = len(stops) / n_closed * 100 if n_closed else 0
    loss_rate = (len(stops) + len(losses)) / n_closed * 100 if n_closed else 0

    # P0 真实误判率（排除系统性崩跌止损和保本止盈）
    wins_lock   = [s for s in wins if s.get("stop_type") == "PROFIT_LOCK"]
    stops_wrong = [s for s in stops if s.get("stop_type") not in ("MARKET_CRASH",)]
    stops_crash = [s for s in stops if s.get("stop_type") == "MARKET_CRASH"]
    true_err    = len(stops_wrong) / n_closed * 100 if n_closed else 0

    # 全局召回率（基于所有信号，仅供参考）
    signaled_big = {s["ticker"] for s in signals if (s.get("max_ret") or 0) > 100}
    total_big    = len(signaled_big) + len(missed)
    recall       = len(signaled_big) / total_big * 100 if total_big else 0

    rets_12m = [s["ret_12m"] for s in closed if s["ret_12m"] is not None]
    avg_ret  = sum(rets_12m) / len(rets_12m) if rets_12m else 0

    max_rets = [s["max_ret"] for s in signals if s.get("max_ret") is not None]
    avg_max  = sum(max_rets) / len(max_rets) if max_rets else 0

    yearly: dict = defaultdict(int)
    for s in signals:
        yearly[s["date"][:4]] += 1

    # P1 提前量（基于实际交易信号）
    lead = compute_lead_times(metric_sigs, prices) if prices else {"avg_days": 0, "count": 0, "total": 0}

    # P1 同类优质召回率（基于实际交易信号）
    qcr = (compute_quality_cohort_recall(metric_sigs, missed, funds)
           if funds else {"pct": 0, "captured": 0, "quality_missed": 0, "denominator": 0})

    return {
        # ── 基础计数 ──────────────────────────────
        "total_signals": len(signals),
        "n_deployed":    n_deployed,
        "closed":        n_closed,
        "open":          len(open_),
        "wins":          len(wins),
        "stops":         len(stops),
        "losses":        len(losses),
        "stops_wrong":   len(stops_wrong),
        "stops_crash":   len(stops_crash),
        "wins_lock":     len(wins_lock),
        # ── 🌟 North Star ─────────────────────────
        "portfolio":     portfolio,
        "qqq_ret":       qqq_ret,
        "alpha":         round(alpha, 1) if alpha is not None else None,
        # ── 🚨 P0 ────────────────────────────────
        "precision":     round(precision, 1),
        "true_err_rate": round(true_err, 1),
        # ── 📊 P1 ────────────────────────────────
        "lead_time":     lead,
        "qcr":           qcr,
        # ── 参考指标 ──────────────────────────────
        "stop_rate":     round(stop_rate, 1),
        "loss_rate":     round(loss_rate, 1),
        "recall":        round(recall, 1),
        "signaled_big":  len(signaled_big),
        "missed_big":    len(missed),
        "avg_ret_12m":   round(avg_ret, 1),
        "avg_max_ret":   round(avg_max, 1),
        "yearly":        dict(yearly),
        # 内部用（不序列化到 JSON）
        "_deployed_keys": deployed_keys,
    }


# ══════════════════════════════════════════════════════════════
#  报告生成
# ══════════════════════════════════════════════════════════════

def _tick(val, target, higher_is_better=True) -> str:
    ok = val >= target if higher_is_better else val <= target
    return "✅" if ok else "❌"


def write_text_report(signals: list, missed: list,
                      stats: dict, start: str, end: str) -> str:
    W  = 70
    eq = "═" * W
    dh = "─" * W

    def fmt(v): return f"{v:+.0f}%" if v is not None else "   —"

    # ── North Star ───────────────────────────────────────────
    pf  = stats.get("portfolio", {})
    qqq = stats.get("qqq_ret",   {})
    alpha = stats.get("alpha")
    strat_ann = pf.get("annual_ret_pct")
    qqq_ann   = qqq.get("annual_ret_pct")

    if strat_ann is not None and qqq_ann is not None:
        alpha_line = (
            f"  策略年化收益  : {strat_ann:+.1f}%"
            f"    QQQ同期年化 : {qqq_ann:+.1f}%\n"
            f"  Alpha (超额) : {alpha:+.1f}%   "
            f"目标>+5%  {_tick(alpha, 5)}"
        )
        if pf.get("final"):
            alpha_line += (
                f"\n  模拟组合终值 : ${pf['final']:,.0f}"
                f"  (初始 ${PORTFOLIO_CAPITAL:,} → {pf['total_ret_pct']:+.1f}%  /{pf['days']}天)"
            )
        if qqq.get("p0"):
            alpha_line += (
                f"\n  QQQ 期间     : ${qqq['p0']:.2f} → ${qqq['p1']:.2f}"
                f"  (+{qqq['total_ret_pct']:.1f}%  /{qqq['days']}天)"
            )
    else:
        alpha_line = "  Alpha: N/A  (未获取 QQQ/VIX 数据，请确认 yfinance 已安装)"

    # ── P0 ───────────────────────────────────────────────────
    prec    = stats["precision"]
    ter     = stats["true_err_rate"]
    n_wrong = stats["stops_wrong"]
    n_crash = stats["stops_crash"]

    p0_lines = (
        f"  精确率         : {prec:.1f}%   目标≥50%  {_tick(prec, 50)}\n"
        f"                   WIN {stats['wins']}条 / 已结算 {stats['closed']}条\n"
        f"  真实误判率     : {ter:.1f}%   目标<30%  {_tick(ter, 30, higher_is_better=False)}\n"
        f"                   STOP_WRONG_PICK {n_wrong}条  |  STOP_MARKET_CRASH {n_crash}条\n"
        f"  崩跌判定条件   : QQQ 7日跌>{MARKET_CRASH_QQQ_DROP*100:.0f}%  或  VIX>{MARKET_CRASH_VIX_LVL:.0f}"
    )

    # ── P1 ───────────────────────────────────────────────────
    lt   = stats.get("lead_time", {})
    qcr  = stats.get("qcr",       {})
    lt_days = lt.get("avg_days", 0)
    qcr_pct = qcr.get("pct",     0)

    p1_lines = (
        f"  平均提前量     : {lt_days:.0f} 天   目标>45天  {_tick(lt_days, 45)}\n"
        f"                   ({lt.get('count',0)}条信号达到+50%里程碑 / 共{lt.get('total',0)}条)\n"
        f"  同类优质召回率 : {qcr_pct:.1f}%   目标>15%  {_tick(qcr_pct, 15)}\n"
        f"                   命中 {qcr.get('captured',0)}只 / L1达标100%+宇宙 {qcr.get('denominator',0)}只\n"
        f"                   (全局召回率 {stats['recall']:.1f}%，遗漏 {stats['missed_big']} 只)"
    )

    # ── Signal detail ────────────────────────────────────────
    deployed_set = stats.get("_deployed_keys", set())
    detail_lines = []
    for s in sorted(signals, key=lambda x: x["date"]):
        outcome   = s["outcome"]
        stop_type = s.get("stop_type") or ""
        dep_icon  = "📈" if (s["ticker"], s["date"]) in deployed_set else "⏩"
        if stop_type == "PROFIT_LOCK":
            tag = "🔒WIN/LOCK  "
        elif outcome == "STOP":
            icon = "💥" if stop_type == "MARKET_CRASH" else "🛑"
            tag  = f"{icon}STOP/{stop_type[:5]}"
        else:
            icon = {"WIN": "✅", "LOSS": "❌", "OPEN": "⏳"}.get(outcome, "?")
            tag  = f"{icon}{outcome:<10}"
        sdot = (s.get("stop_date") or "")[:10] if s.get("stop_hit") else ""
        detail_lines.append(
            f" {dep_icon}{s['date']:<12} {s['ticker']:<6} ${s['entry']:>8.2f}"
            f" dna={s['dna']:>3} cmp={s['comp']:>3}"
            f" {fmt(s['ret_1m']):>6} {fmt(s['ret_3m']):>6}"
            f" {fmt(s['ret_6m']):>6} {fmt(s['ret_12m']):>6}"
            f" pk={fmt(s['max_ret']):>6}  {tag}  {sdot}"
        )

    n_dep = stats.get("n_deployed", stats["total_signals"])

    lines = [
        eq,
        f"  NASDAQ HUNTER — 历史回测报告 v6.0 Production",
        f"  回测周期: {start} → {end}  |  生成: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"  信号总计: {stats['total_signals']}  实际入场: {n_dep}  已结算: {stats['closed']}  Open: {stats['open']}",
        eq,
        "",
        "┌─ 🌟 NORTH STAR: Alpha vs QQQ " + "─" * 38 + "┐",
        *[f"│  {ln}" if not ln.startswith("  ") else f"│{ln}" for ln in alpha_line.splitlines()],
        "└" + "─" * (W - 1) + "┘",
        "",
        f"┌─ 🚨 P0: 生死线指标 (基于 {n_dep} 实际交易) " + "─" * 26 + "┐",
        *[f"│{ln}" for ln in p0_lines.splitlines()],
        "└" + "─" * (W - 1) + "┘",
        "",
        f"┌─ 📊 P1: 质量指标 (基于 {n_dep} 实际交易) " + "─" * 28 + "┐",
        *[f"│{ln}" for ln in p1_lines.splitlines()],
        "└" + "─" * (W - 1) + "┘",
        "",
        dh,
        f"  年度分布: " + "  ".join(f"{yr}:{cnt}" for yr, cnt in sorted(stats.get("yearly", {}).items())),
        f"  结果分布: ✅WIN {stats['wins']}  🔒LOCK {stats.get('wins_lock',0)}"
        f"  🛑STOP_WRONG {n_wrong}  💥STOP_CRASH {n_crash}"
        f"  ❌LOSS {stats['losses']}  ⏳OPEN {stats['open']}",
        dh,
        "",
        "── 所有 BUY 信号明细 (📈=入场 ⏩=跳过) " + "─" * 32,
        f"  {'日期':<12} {'代码':<6} {'入场':>9} {'DNA':>4} {'综':>4}"
        f" {'1M%':>6} {'3M%':>6} {'6M%':>6} {'12M%':>6} {'峰值%':>7}  {'结果':<20} {'止损日'}",
        "  " + "─" * 95,
        *detail_lines,
        "",
        "── 遗漏的爆发机会（涨幅>100% 但未发信号，前50只）" + "─" * 20,
        f"  {'代码':<7} {'最大涨幅':>9} {'起始价':>9} {'峰值价':>9}",
        "  " + "─" * 40,
        *[f"  {m['ticker']:<7} {m['max_ret']:>+8.0f}%  ${m['start_price']:>8.2f}  ${m['max_price']:>8.2f}"
          for m in missed[:50]],
        *(["  ... 共 {} 只遗漏（仅展示前50）".format(len(missed))] if len(missed) > 50 else []),
        "",
        eq,
        "  免责声明: 本报告仅供研究参考，不构成投资建议",
        eq,
    ]

    report = "\n".join(lines)
    OUT_REPORT.write_text(report, encoding="utf-8")
    log.info(f"文字报告已保存: {OUT_REPORT}")
    return report


# ══════════════════════════════════════════════════════════════
#  Lark 推送
# ══════════════════════════════════════════════════════════════

def push_lark_report(stats: dict, top_wins: list,
                     start: str, end: str, dry_run: bool) -> None:
    today = date.today().isoformat()
    prec  = stats["precision"]
    alpha = stats.get("alpha")
    color = "green" if prec >= 60 else "orange" if prec >= 45 else "red"
    title = f"📊 回测报告 v2.0 · {today}"

    pf  = stats.get("portfolio", {})
    qqq = stats.get("qqq_ret",   {})
    lt  = stats.get("lead_time", {})
    qcr = stats.get("qcr",       {})

    # North Star
    if alpha is not None:
        ns_line = (
            f"策略年化 **{pf.get('annual_ret_pct', 0):+.1f}%**  vs  "
            f"QQQ **{qqq.get('annual_ret_pct', 0):+.1f}%**\n"
            f"**Alpha = {alpha:+.1f}%** {'✅' if alpha >= 5 else '❌'}  目标>+5%"
        )
    else:
        ns_line = "Alpha: N/A（需 yfinance）"

    p0_line = (
        f"精确率 **{prec:.1f}%** {'✅' if prec >= 50 else '❌'}  ≥50%\n"
        f"真实误判率 **{stats['true_err_rate']:.1f}%** {'✅' if stats['true_err_rate'] < 30 else '❌'}  <30%\n"
        f"STOP_WRONG {stats['stops_wrong']}  |  STOP_MARKET_CRASH {stats['stops_crash']}"
    )

    p1_line = (
        f"平均提前量 **{lt.get('avg_days', 0):.0f}天** {'✅' if lt.get('avg_days', 0) >= 45 else '❌'}  >45天\n"
        f"同类召回率 **{qcr.get('pct', 0):.1f}%** {'✅' if qcr.get('pct', 0) >= 15 else '❌'}  >15%"
    )

    best = "\n".join(
        f"  {i+1}. **{s['ticker']}** 峰值 {s.get('max_ret', 0):+.0f}%"
        f"  入场 ${s['entry']:.1f}  {s['date']}"
        for i, s in enumerate(top_wins[:5])
    ) or "  暂无"

    body = (
        f"**回测周期**: {start} → {end}  |  "
        f"信号 {stats['total_signals']} 条  已结算 {stats['closed']} 条\n\n"
        f"━━ 🌟 North Star: Alpha vs QQQ ━━\n{ns_line}\n\n"
        f"━━ 🚨 P0 生死线 ━━\n{p0_line}\n\n"
        f"━━ 📊 P1 质量 ━━\n{p1_line}\n\n"
        f"━━ 最佳信号 ━━\n{best}\n\n"
        f"_avg12M {stats['avg_ret_12m']:+.1f}%  |  avgPeak {stats['avg_max_ret']:+.1f}%_"
    )

    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": title},
                       "template": color},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": body}}],
        },
    }

    if dry_run:
        log.info("[dry-run] 不推送 Lark，卡片内容如下:")
        log.info(body)
        return
    if not FEISHU_HOOK:
        log.warning("FEISHU_WEBHOOK 未设置，跳过推送")
        return
    try:
        r  = requests.post(FEISHU_HOOK, json=payload, timeout=10)
        ok = r.json().get("code", -1) == 0
        log.info(f"Lark 推送: {'成功' if ok else '失败 ' + r.text}")
    except Exception as e:
        log.error(f"Lark 推送异常: {e}")


# ══════════════════════════════════════════════════════════════
#  工具函数
# ══════════════════════════════════════════════════════════════

def _add_days(dt_str: str, n: int) -> str:
    return (datetime.strptime(dt_str, "%Y-%m-%d") + timedelta(days=n)).strftime("%Y-%m-%d")


def _today_dt() -> datetime:
    return datetime.strptime(date.today().isoformat(), "%Y-%m-%d")


# ══════════════════════════════════════════════════════════════
#  主函数
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="NASDAQ Hunter 历史回测验证")
    parser.add_argument("--start",   default=None,
                        help="回测起始日期 YYYY-MM-DD（默认数据库最早日期）")
    parser.add_argument("--end",     default=date.today().isoformat(),
                        help="回测结束日期（默认今天）")
    parser.add_argument("--dry-run", action="store_true",
                        help="不推送 Lark，只生成本地报告")
    args = parser.parse_args()

    log.info("=" * 62)
    log.info("  NASDAQ Hunter — 历史走步验证引擎")
    log.info(f"  数据库: {DB_PATH}")
    log.info("=" * 62)

    if not DB_PATH.exists():
        sys.exit("❌ 数据库不存在，请先运行: python3 nasdaq_downloader.py")

    conn = sqlite3.connect(str(DB_PATH))

    n_prices = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    n_funds  = conn.execute("SELECT COUNT(*) FROM fundamentals").fetchone()[0]
    if n_prices < 1000:
        sys.exit(f"❌ 价格数据不足 ({n_prices}条)")
    log.info(f"数据库: {n_prices:,} 条价格, {n_funds:,} 条基本面")

    # ── 预加载全部数据到内存 ──────────────────────────────────
    prices                       = preload_prices(conn)
    dv_data                      = preload_dv(prices)
    funds, fund_fdates, prev_map = preload_fundamentals(conn)
    macro_dates, macro_rates     = preload_macro(conn)
    conn.close()

    # 确定回测起始日
    all_dates = sorted({dt for p in prices.values() for dt in p})
    start = args.start or all_dates[0]
    end   = args.end
    log.info(f"回测周期: {start} → {end}")

    # ── 0. 市场基准数据（QQQ / VIX）──────────────────────────
    log.info("获取市场基准数据 (QQQ / VIX)…")
    market_data  = fetch_market_data(start, end)
    qqq_weekly   = build_qqq_weekly_returns(market_data)

    # ── 1. 走步模拟 ────────────────────────────────────────────
    signals = run_walkforward(
        prices, funds, fund_fdates, prev_map,
        macro_dates, macro_rates, dv_data, start, end,
    )

    # ── 2. 评估真实结果（含崩跌标记）────────────────────────
    signals = evaluate_signals(signals, prices, market_data, qqq_weekly)

    # ── 3. 识别遗漏机会 ───────────────────────────────────────
    missed = find_missed_breakouts(prices, signals, start, end)

    # ── 4. 统计（含 North Star / P0 / P1）───────────────────
    stats = compute_stats(signals, missed,
                          funds=funds, prices=prices,
                          market_data=market_data,
                          backtest_start=start, backtest_end=end)

    # ── 5. 保存 JSON ──────────────────────────────────────────
    stats_json = {k: v for k, v in stats.items() if k != "_deployed_keys"}
    OUT_SIGNALS.write_text(
        json.dumps({"stats": stats_json, "signals": signals, "missed": missed[:80]},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info(f"信号详情已保存: {OUT_SIGNALS}")

    # ── 6. 文字报告 ───────────────────────────────────────────
    write_text_report(signals, missed, stats, start, end)

    # ── 7. 控制台摘要 ─────────────────────────────────────────
    alpha = stats.get("alpha")
    pf    = stats.get("portfolio", {})
    qqq   = stats.get("qqq_ret",   {})
    lt    = stats.get("lead_time", {})
    qcr   = stats.get("qcr",       {})
    log.info("")
    log.info("=" * 62)
    log.info("  ★ 回测验证结果摘要 v2.0")
    log.info("=" * 62)
    log.info(f"  总信号数       : {stats['total_signals']}")
    log.info(f"  已结算         : {stats['closed']}  (Open: {stats['open']})")
    log.info("")
    log.info("  🌟 NORTH STAR")
    if alpha is not None:
        log.info(f"     策略年化    : {pf.get('annual_ret_pct', 0):+.1f}%")
        log.info(f"     QQQ年化     : {qqq.get('annual_ret_pct', 0):+.1f}%")
        log.info(f"     Alpha       : {alpha:+.1f}%  {'✅' if alpha >= 5 else '❌'}  目标>+5%")
    else:
        log.info("     Alpha       : N/A (无 QQQ 数据)")
    log.info("")
    log.info("  🚨 P0 生死线")
    log.info(f"     精确率       : {stats['precision']:.1f}%  {'✅' if stats['precision'] >= 50 else '❌'}  目标≥50%")
    log.info(f"     真实误判率   : {stats['true_err_rate']:.1f}%  {'✅' if stats['true_err_rate'] < 30 else '❌'}  目标<30%")
    log.info(f"     STOP_WRONG   : {stats['stops_wrong']}条  |  STOP_CRASH : {stats['stops_crash']}条")
    log.info("")
    log.info("  📊 P1 质量")
    log.info(f"     平均提前量   : {lt.get('avg_days', 0):.0f}天  {'✅' if lt.get('avg_days', 0) >= 45 else '❌'}  目标>45天")
    log.info(f"     同类召回率   : {qcr.get('pct', 0):.1f}%  {'✅' if qcr.get('pct', 0) >= 15 else '❌'}  目标>15%")
    log.info("")
    log.info(f"  平均12M收益    : {stats['avg_ret_12m']:+.1f}%")
    log.info(f"  平均峰值涨幅   : {stats['avg_max_ret']:+.1f}%")
    log.info("=" * 62)

    # ── 8. Lark 推送 ──────────────────────────────────────────
    top_wins = sorted(
        [s for s in signals if s.get("max_ret") is not None],
        key=lambda x: -(x["max_ret"] or 0)
    )
    push_lark_report(stats, top_wins, start, end, args.dry_run)

    log.info(f"\n  完成! 详细报告: {OUT_REPORT}")


if __name__ == "__main__":
    main()

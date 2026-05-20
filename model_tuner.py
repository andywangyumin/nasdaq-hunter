"""
model_tuner.py — SNDK DNA 模型参数调优引擎 v2
=============================================
目标：通过网格搜索，找到最优化以下指标的参数组合：
  1. 准确率（Precision）:  成熟信号中，股票在270天内实现+50%的比例
  2. 遗漏率（Miss Rate）:  "可识别质量爆发股"中，模型未发信号的比例
  3. 错误率（Error Rate）: 1 - 准确率
  4. 提前量（Lead Time）:  信号比股票涨+50%领先的平均天数（负值=信号落后）

改进要点：
  - 地面真实 (GT) = 符合当前L1条件、且在270天内涨+50%的股票（不包括便士股）
  - 成熟度过滤：仅评估距今≥EVAL_WINDOW天的信号（确保有足够时间观测）
  - 提前量：信号日到+50%日的天数，取正（领先）/负（滞后）平均
  - 综合分 = precision×0.5 + coverage×0.3 + lead_score×0.2

两阶段方法：
  Phase 1: 对每个L1组合跑一次走步，收集原始候选事件（含完整评分）
  Phase 2: 对阈值参数网格快速过滤，每组合 <0.1s

用法：
  python3 model_tuner.py
  python3 model_tuner.py --apply-best   # 写入 historical_backtest.py
  python3 model_tuner.py --start 2024-01-01
"""

import os, sys, json, sqlite3, logging, bisect, time, itertools, argparse, re
from datetime import date, timedelta, datetime
from pathlib import Path
from collections import defaultdict

DB_PATH     = Path(__file__).parent / "data" / "nasdaq_history.db"
OUT_TUNING  = Path(__file__).parent / "data" / "tuning_results.json"
BACKTEST_PY = Path(__file__).parent / "historical_backtest.py"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("tuner")

# ── 固定参数 ───────────────────────────────────────────────────────────
STOP_LOSS   = 0.15
FRESH_DAYS  = 180
DEDUP_DAYS  = 180
MIN_TREND   = 55

# ── 评估参数 ───────────────────────────────────────────────────────────
SUCCESS_GAIN   = 0.50   # +50% 视为成功
SUCCESS_WINDOW = 270    # 日历天 (≈ 180 交易日)
EVAL_WINDOW    = 180    # 信号发出后至少 N 天才纳入"成熟"评估
GT_GAIN        = 0.50   # 地面真实爆发门槛
MIN_PRICE_GT   = 5.0    # GT 起始价格过滤
MIN_PRICE_SIG  = 5.0    # 信号生成最低价格（过滤便士股）
MAX_REV_YOY    = 800.0  # 收入增速上限（过滤低基数周转型公司）

# ── 参数搜索网格 v2 ────────────────────────────────────────────────────
# L1 门控（每个组合独立走步，约 7×12s = 84s）
L1_GRID = [
    {"min_rev_yoy": 35,  "min_gm": 0},
    {"min_rev_yoy": 50,  "min_gm": 0},
    {"min_rev_yoy": 50,  "min_gm": 25},
    {"min_rev_yoy": 50,  "min_gm": 40},
    {"min_rev_yoy": 70,  "min_gm": 30},
    {"min_rev_yoy": 70,  "min_gm": 40},
    {"min_rev_yoy": 100, "min_gm": 40},
]

# 阈值参数（候选池快速过滤）
# 新增 min_beats：要求过去4季中至少 N 次EPS超预期
THRESH_GRID = {
    "min_dna":       [68, 72, 76],
    "base_thresh":   [72, 75, 78],
    "min_hist":      [3, 5],
    "require_accel": [False, True],
    "min_beats":     [0, 2, 3],   # 最少EPS超预期次数（0=不限制）
}

# ══════════════════════════════════════════════════════════════
#  数据加载
# ══════════════════════════════════════════════════════════════

def preload_prices(conn):
    log.info("预加载价格数据...")
    t0 = time.time()
    rows = conn.execute(
        "SELECT ticker, date, low, close FROM prices ORDER BY ticker, date"
    ).fetchall()
    prices = {}
    for ticker, dt, low, close in rows:
        if not close or close <= 0:
            continue
        prices.setdefault(ticker, {})[dt] = (low if low else close, close)
    log.info(f"价格预加载: {len(rows):,} 行  {len(prices)} 只股票  ({time.time()-t0:.1f}s)")
    return prices


def preload_fundamentals(conn):
    log.info("预加载基本面数据...")
    t0 = time.time()
    cols = ["period", "filing_date", "revenue", "revenue_yoy", "gross_margin",
            "eps_actual", "eps_estimate", "eps_surprise", "beats_4q"]
    rows = conn.execute(
        f"SELECT ticker, {','.join(cols)} FROM fundamentals "
        "ORDER BY ticker, period, filing_date"
    ).fetchall()

    seen = {}
    for row in rows:
        key = (row[0], row[1])
        if key not in seen:
            seen[key] = row

    funds = {}
    for (ticker, period), row in seen.items():
        d = dict(zip(cols, row[1:]))
        funds.setdefault(ticker, []).append(d)

    for fl in funds.values():
        fl.sort(key=lambda f: f["filing_date"])

    # 补算 revenue_yoy
    filled = 0
    for ticker, fl in funds.items():
        by_period = sorted(fl, key=lambda f: f["period"])
        for i, f in enumerate(by_period):
            if f.get("revenue_yoy") is not None:
                continue
            if not f.get("revenue"):
                continue
            curr_dt = datetime.strptime(f["period"][:10], "%Y-%m-%d")
            for j in range(i - 1, max(-1, i - 8), -1):
                prev = by_period[j]
                if not prev.get("revenue"):
                    continue
                prev_dt = datetime.strptime(prev["period"][:10], "%Y-%m-%d")
                diff = (curr_dt - prev_dt).days
                if 300 <= diff <= 420 and prev["revenue"] > 0:
                    f["revenue_yoy"] = round(
                        (f["revenue"] - prev["revenue"]) / prev["revenue"] * 100, 1
                    )
                    filled += 1
                    break

    fund_fdates = {t: [f["filing_date"] for f in fl] for t, fl in funds.items()}
    prev_map = {}
    for ticker, fl in funds.items():
        by_period = sorted(fl, key=lambda f: f["period"])
        prev_map[ticker] = {}
        for i, f in enumerate(by_period):
            prev_map[ticker][f["period"]] = by_period[i - 1] if i > 0 else None

    log.info(f"基本面预加载: {len(funds)} 只股票  yoy补算: {filled}  ({time.time()-t0:.1f}s)")
    return funds, fund_fdates, prev_map


def preload_macro(conn):
    rows = conn.execute("SELECT date, fed_rate FROM macro_rates ORDER BY date").fetchall()
    return [r[0] for r in rows], [r[1] for r in rows]


# ══════════════════════════════════════════════════════════════
#  评分计算（与 historical_backtest.py 一致）
# ══════════════════════════════════════════════════════════════

def pit_fund(fl, fdates, as_of):
    idx = bisect.bisect_right(fdates, as_of) - 1
    return fl[idx] if idx >= 0 else None


def pit_fed(mdates, mrates, as_of):
    idx = bisect.bisect_right(mdates, as_of) - 1
    return mrates[idx] if idx >= 0 else 4.5


def score_margin(gm):
    if gm is None or gm <= 0: return 0
    if gm > 70: return 100
    if gm > 55: return 82
    if gm > 40: return 62
    if gm > 25: return 38
    if gm > 15: return 18
    return 5


def score_rev_accel(rev_yoy, prev_rev_yoy):
    if rev_yoy is None or rev_yoy <= 0: return 0
    accel = prev_rev_yoy is not None and rev_yoy > prev_rev_yoy * 1.15
    base = (100 if rev_yoy > 200 else 88 if rev_yoy > 100 else
            68 if rev_yoy > 50 else 45 if rev_yoy > 30 else
            28 if rev_yoy > 20 else 10)
    return min(100, base + (12 if accel else 0))


def score_beats(beats_4q, surp):
    n   = beats_4q or 0
    mag = surp or 0.0
    base = (100 if n >= 4 else 80 if n == 3 else 55 if n == 2 else 25 if n == 1 else 0)
    bonus = (20 if mag > 50 else 12 if mag > 25 else 5 if mag > 10 else 0)
    return min(100, base + bonus)


def compute_dna(fund, prev_fund):
    gm       = fund.get("gross_margin")
    rev_yoy  = fund.get("revenue_yoy")
    beats    = fund.get("beats_4q")
    surp     = fund.get("eps_surprise")
    prev_yoy = (prev_fund or {}).get("revenue_yoy")

    s_margin = score_margin(gm)
    s_rev    = score_rev_accel(rev_yoy, prev_yoy)
    s_beats  = score_beats(beats, surp)

    beats_missing = (beats is None and surp is None)
    if beats_missing:
        raw_dna = (s_margin * 24 + s_rev * 26) / 50
        failed  = [(s_margin < 40, f"GM({s_margin})"), (s_rev < 45, f"RevAccel({s_rev})")]
    else:
        raw_dna = (s_margin * 24 + s_rev * 26 + s_beats * 22) / 72
        failed  = [(s_margin < 40, f"GM({s_margin})"), (s_rev < 45, f"RevAccel({s_rev})"),
                   (s_beats < 35, f"Beats({s_beats})")]
    failed = [x for cond, x in failed if cond]
    return round(raw_dna * 0.5 if failed else raw_dna), failed


def compute_trend(dna_hist):
    if len(dna_hist) < 2: return 0
    recent = dna_hist[-10:]
    n  = len(recent)
    xm = (n - 1) / 2
    ym = sum(recent) / n
    num = sum((i - xm) * (y - ym) for i, y in enumerate(recent))
    den = sum((i - xm) ** 2 for i in range(n))
    slope = num / den if den else 0.0
    streak = sum(1 for _ in (v for v in reversed(recent) if (True if v >= 55 else False))
                 for __ in [None] if True)
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


def compute_composite(dna, trend, hist_len):
    if hist_len < 2: return round(dna * 0.45)
    if hist_len < 5: return round(dna * 0.6 + trend * 0.4)
    return round(dna * 0.5 + trend * 0.5)


def dyn_threshold(fed_rate, base_thresh):
    if fed_rate > 5.0: return base_thresh + 10
    if fed_rate > 4.5: return base_thresh + 5
    return base_thresh


def _add_days(dt_str, n):
    return (datetime.strptime(dt_str, "%Y-%m-%d") + timedelta(days=n)).strftime("%Y-%m-%d")


# ══════════════════════════════════════════════════════════════
#  Phase 1: 候选事件收集（每个 L1 组合独立运行）
# ══════════════════════════════════════════════════════════════

def collect_candidates(prices, funds, fund_fdates, prev_map,
                       macro_dates, macro_rates, start, end,
                       min_rev_yoy, min_gm) -> list:
    """
    用给定 L1 参数跑走步，收集所有通过 L1 的候选事件及原始评分。
    注意: dna_history 是该 L1 参数下真实积累的，确保 trend/hist_len 准确。
    """
    all_dates = sorted({dt for p in prices.values() for dt in p})
    trading_dates = [d for d in all_dates if start <= d <= end]
    all_tickers   = sorted(prices.keys())

    dna_history = defaultdict(list)
    candidates  = []
    t0 = time.time()

    for cur_date in trading_dates:
        fed_rate = pit_fed(macro_dates, macro_rates, cur_date)

        for ticker in all_tickers:
            tpx = prices.get(ticker)
            if not tpx: continue
            pd = tpx.get(cur_date)
            if not pd: continue
            _, close_price = pd
            if close_price <= 0: continue

            fl = funds.get(ticker)
            if not fl: continue
            fund = pit_fund(fl, fund_fdates[ticker], cur_date)
            if not fund: continue

            days_stale = (datetime.strptime(cur_date, "%Y-%m-%d") -
                          datetime.strptime(fund["filing_date"], "%Y-%m-%d")).days
            if days_stale > FRESH_DAYS: continue

            gm      = fund.get("gross_margin") or 0
            rev_yoy = fund.get("revenue_yoy") or 0

            if gm < min_gm:                   continue
            if rev_yoy < min_rev_yoy:         continue
            if rev_yoy > MAX_REV_YOY:         continue  # 过滤低基数周转型
            if close_price < MIN_PRICE_SIG:   continue  # 过滤便士股
            if close_price > 3000:            continue

            prev_fund = prev_map.get(ticker, {}).get(fund.get("period", ""))
            dna, _    = compute_dna(fund, prev_fund)
            dna_history[ticker].append(dna)

            hist_len = len(dna_history[ticker])
            trend    = compute_trend(dna_history[ticker])
            comp     = compute_composite(dna, trend, hist_len)
            prev_yoy = (prev_fund or {}).get("revenue_yoy")

            candidates.append({
                "date":     cur_date,
                "ticker":   ticker,
                "entry":    round(close_price, 2),
                "dna":      dna,
                "trend":    trend,
                "comp":     comp,
                "fed_rate": fed_rate,
                "gm":       gm,
                "rev_yoy":  rev_yoy,
                "prev_yoy": prev_yoy,
                "hist_len": hist_len,
                "beats_4q": fund.get("beats_4q"),  # None or 0-4
            })

    log.info(f"  L1(rev≥{min_rev_yoy},gm≥{min_gm}) → {len(candidates):,} 候选事件  ({time.time()-t0:.1f}s)")
    return candidates


# ══════════════════════════════════════════════════════════════
#  Phase 2: 快速阈值过滤
# ══════════════════════════════════════════════════════════════

def apply_filter(candidates, min_dna, base_thresh, min_hist,
                 require_accel, min_beats=0) -> list:
    signals  = []
    last_sig = {}
    for cand in candidates:   # already date-sorted from collect step
        if cand["dna"]      < min_dna:  continue
        if cand["hist_len"] < min_hist: continue
        if cand["trend"]    < MIN_TREND: continue
        if cand["comp"]     < dyn_threshold(cand["fed_rate"], base_thresh): continue
        if require_accel:
            py = cand.get("prev_yoy")
            if py is None or cand["rev_yoy"] <= py * 1.15:
                continue
        # EPS 超预期次数过滤（None 视为 0 次）
        if min_beats > 0:
            beats = cand.get("beats_4q") or 0
            if beats < min_beats:
                continue
        ticker = cand["ticker"]
        last   = last_sig.get(ticker)
        if last:
            gap = (datetime.strptime(cand["date"], "%Y-%m-%d") -
                   datetime.strptime(last, "%Y-%m-%d")).days
            if gap < DEDUP_DAYS:
                continue
        last_sig[ticker] = cand["date"]
        signals.append(cand)
    return signals


# ══════════════════════════════════════════════════════════════
#  L1-过滤的地面真实集（针对每个 L1 参数组合）
# ══════════════════════════════════════════════════════════════

def build_ground_truth_l1(prices, funds, fund_fdates, prev_map,
                          macro_dates, macro_rates, start, end,
                          min_rev_yoy, min_gm) -> set:
    """
    地面真实 = 在测试期内，曾满足 L1 条件（在 filing_date 后 FRESH_DAYS 内有效），
    且在该窗口内股价从首次满足L1条件起算 270天内涨幅≥50%的股票。
    排除便士股（价格 < $5）。
    """
    today_str = date.today().isoformat()
    end_cap   = min(end, today_str)
    gt        = set()

    # 对每只股票：找它首次满足L1的日期，然后检查之后270天内是否涨50%
    all_dates   = sorted({dt for p in prices.values() for dt in p})
    trading_dates = [d for d in all_dates if start <= d <= end_cap]

    # 快速计算每只股票的L1首次满足日
    l1_first = {}   # ticker -> first L1-pass date
    for ticker in prices:
        fl = funds.get(ticker)
        if not fl: continue
        fdates = fund_fdates[ticker]
        for d in trading_dates:
            tpx = prices.get(ticker, {})
            pd  = tpx.get(d)
            if not pd: continue
            _, close = pd
            if not close or close <= 0: continue
            fund = pit_fund(fl, fdates, d)
            if not fund: continue
            days_stale = (datetime.strptime(d, "%Y-%m-%d") -
                          datetime.strptime(fund["filing_date"], "%Y-%m-%d")).days
            if days_stale > FRESH_DAYS: continue
            gm      = fund.get("gross_margin") or 0
            rev_yoy = fund.get("revenue_yoy") or 0
            if (gm >= min_gm and min_rev_yoy <= rev_yoy <= MAX_REV_YOY
                    and MIN_PRICE_GT <= close <= 3000):
                l1_first[ticker] = d
                break

    # 检查首次 L1 通过后 270 天内是否涨 50%
    for ticker, first_date in l1_first.items():
        tpx    = prices[ticker]
        entry  = tpx.get(first_date, (None, None))[1]
        if not entry or entry < MIN_PRICE_GT:
            continue
        target  = entry * (1 + GT_GAIN)
        horizon = _add_days(first_date, SUCCESS_WINDOW)
        for d, (_, close) in sorted(tpx.items()):
            if d <= first_date: continue
            if d > horizon: break
            if close and close >= target:
                gt.add(ticker)
                break

    return gt


# ══════════════════════════════════════════════════════════════
#  4 项指标计算（含成熟度过滤 + 提前量）
# ══════════════════════════════════════════════════════════════

def compute_4_metrics(signals, prices, ground_truth) -> dict:
    """
    成熟信号：距今 >= EVAL_WINDOW 天（才有足够时间观察 +50%）
    精确率：成熟信号中，270天内实现 +50% 的比例
    提前量：信号日到+50%达成日的天数（正=领先，负=滞后）
    遗漏率：GT中未被任何信号覆盖的比例
    """
    today_str = date.today().isoformat()
    today_dt  = datetime.strptime(today_str, "%Y-%m-%d")

    mature    = []
    success   = 0
    lead_acc  = []
    sig_tickers = set()

    for sig in signals:
        ticker  = sig["ticker"]
        entry   = sig["entry"]
        sig_date = sig["date"]
        sig_tickers.add(ticker)

        sig_dt   = datetime.strptime(sig_date, "%Y-%m-%d")
        days_old = (today_dt - sig_dt).days
        if days_old < EVAL_WINDOW:
            continue   # 太新，无法公正评估
        mature.append(sig)

        target  = entry * (1 + SUCCESS_GAIN)
        horizon = _add_days(sig_date, SUCCESS_WINDOW)
        tpx     = prices.get(ticker, {})
        future  = sorted(d for d in tpx if sig_date < d <= min(horizon, today_str))

        for dt in future:
            _, close = tpx[dt]
            if close and close >= target:
                ld = (datetime.strptime(dt, "%Y-%m-%d") - sig_dt).days
                lead_acc.append(ld)
                success += 1
                break

    n_mature  = len(mature)
    precision = success / n_mature * 100 if n_mature else 0

    # 遗漏率：GT 中无信号覆盖的
    missed_gt = ground_truth - sig_tickers
    miss_rate = len(missed_gt) / len(ground_truth) * 100 if ground_truth else 100

    avg_lead  = sum(lead_acc) / len(lead_acc) if lead_acc else 0

    return {
        "n_signals":    len(signals),
        "n_mature":     n_mature,
        "n_success":    success,
        "precision":    round(precision, 1),
        "error_rate":   round(100 - precision, 1),
        "miss_rate":    round(miss_rate, 1),
        "avg_lead":     round(avg_lead, 1),
        "n_gt":         len(ground_truth),
        "n_missed_gt":  len(missed_gt),
        "n_hit_gt":     len(ground_truth) - len(missed_gt),
    }


def combined_score(m: dict) -> float:
    """
    综合评分（0-100，越高越好）：
      - 准确率 50% 权重
      - GT覆盖率(1-遗漏率) 30% 权重
      - 提前量 20% 权重（以90天为满分）
    如果成熟信号数 < 5，视为数据不足，降权处理
    """
    if m["n_mature"] < 3:
        return 0.0
    cov   = 100 - m["miss_rate"]
    lead  = max(0.0, min(m["avg_lead"] / 90.0, 1.0)) * 100
    return m["precision"] * 0.5 + cov * 0.3 + lead * 0.2


# ══════════════════════════════════════════════════════════════
#  网格搜索
# ══════════════════════════════════════════════════════════════

def run_grid_search(prices, funds, fund_fdates, prev_map,
                    macro_dates, macro_rates, start, end):
    t_keys   = list(THRESH_GRID.keys())
    t_combos = list(itertools.product(*[THRESH_GRID[k] for k in t_keys]))
    total    = len(L1_GRID) * len(t_combos)
    log.info(f"参数网格: {len(L1_GRID)} 个L1 × {len(t_combos)} 个阈值 = {total} 种参数")

    results   = []
    combo_idx = 0
    gt_cache  = {}   # (min_rev_yoy, min_gm) -> ground_truth set

    for l1 in L1_GRID:
        l1_key = (l1["min_rev_yoy"], l1["min_gm"])

        # 构建该 L1 对应的地面真实集
        if l1_key not in gt_cache:
            log.info(f"  构建GT: L1(rev≥{l1['min_rev_yoy']},gm≥{l1['min_gm']})")
            gt_cache[l1_key] = build_ground_truth_l1(
                prices, funds, fund_fdates, prev_map,
                macro_dates, macro_rates, start, end,
                l1["min_rev_yoy"], l1["min_gm"]
            )
            log.info(f"    → GT: {len(gt_cache[l1_key])} 只质量爆发股")

        ground_truth = gt_cache[l1_key]

        log.info(f"── L1 walk-forward: rev≥{l1['min_rev_yoy']} gm≥{l1['min_gm']} ──")
        candidates = collect_candidates(
            prices, funds, fund_fdates, prev_map,
            macro_dates, macro_rates, start, end,
            l1["min_rev_yoy"], l1["min_gm"]
        )

        for tc in t_combos:
            params = dict(zip(t_keys, tc))
            params.update(l1)
            combo_idx += 1

            signals = apply_filter(
                candidates,
                params["min_dna"],
                params["base_thresh"],
                params["min_hist"],
                params["require_accel"],
                params.get("min_beats", 0),
            )
            m     = compute_4_metrics(signals, prices, ground_truth)
            score = combined_score(m)

            results.append({"params": params, "metrics": m, "score": round(score, 2)})

    results.sort(key=lambda r: -r["score"])
    return results, gt_cache


# ══════════════════════════════════════════════════════════════
#  信号详情分析（打印最优参数下的每条信号）
# ══════════════════════════════════════════════════════════════

def analyze_best_signals(best_params, prices, funds, fund_fdates, prev_map,
                         macro_dates, macro_rates, start, end):
    """打印最优参数下的信号明细"""
    candidates = collect_candidates(
        prices, funds, fund_fdates, prev_map,
        macro_dates, macro_rates, start, end,
        best_params["min_rev_yoy"], best_params["min_gm"]
    )
    signals = apply_filter(
        candidates,
        best_params["min_dna"],
        best_params["base_thresh"],
        best_params["min_hist"],
        best_params["require_accel"],
        best_params.get("min_beats", 0),
    )

    today_str = date.today().isoformat()
    today_dt  = datetime.strptime(today_str, "%Y-%m-%d")

    log.info("")
    log.info("── 最优参数下的信号明细 ─────────────────────────────────────────────────")
    log.info(f"  {'日期':<12} {'代码':<7} {'入场':>7} {'DNA':>4} {'综':>4} "
             f"{'rev%':>6} {'gm':>4} {'beats':>5} {'结果':>10}")
    log.info("  " + "─" * 75)

    for sig in sorted(signals, key=lambda s: s["date"]):
        ticker   = sig["ticker"]
        entry    = sig["entry"]
        sig_date = sig["date"]
        sig_dt   = datetime.strptime(sig_date, "%Y-%m-%d")
        days_old = (today_dt - sig_dt).days

        target  = entry * (1 + SUCCESS_GAIN)
        horizon = _add_days(sig_date, SUCCESS_WINDOW)
        tpx     = prices.get(ticker, {})
        future  = sorted(d for d in tpx if sig_date < d <= min(horizon, today_str))

        result = "⏳开放"
        if days_old >= EVAL_WINDOW:
            hit = False
            for dt in future:
                _, close = tpx[dt]
                if close and close >= target:
                    ld = (datetime.strptime(dt, "%Y-%m-%d") - sig_dt).days
                    result = f"✅+{ld}d"
                    hit = True
                    break
            if not hit:
                latest = sorted(d for d in tpx if d >= sig_date)
                if latest:
                    _, lc = tpx[latest[-1]]
                    ret = (lc - entry) / entry * 100 if lc else 0
                    result = f"{'❌' if ret < 0 else '📈'}{ret:+.0f}%"
                else:
                    result = "❌无数据"

        beats_str = str(sig.get("beats_4q") or "?") + "/4"
        log.info(
            f"  {sig_date:<12} {ticker:<7} ${entry:>6.2f}"
            f" {sig['dna']:>4} {sig['comp']:>4}"
            f" {sig['rev_yoy']:>5.0f}% {sig['gm']:>3.0f}% {beats_str:>5}"
            f"  {result}"
        )


# ══════════════════════════════════════════════════════════════
#  结果展示
# ══════════════════════════════════════════════════════════════

def print_results(results, top_n=15):
    log.info("")
    log.info("=" * 90)
    log.info(f"  ★ 模型调优结果 — Top {top_n}")
    log.info("=" * 90)
    log.info(
        f"  {'排名':>4}  {'综合分':>6}  {'准确率':>7}  {'遗漏率':>7}  {'错误率':>7}  "
        f"{'提前量':>7}  {'成熟N':>5}  {'信号N':>5}  参数概要"
    )
    log.info("  " + "─" * 88)

    for i, r in enumerate(results[:top_n], 1):
        m  = r["metrics"]
        p  = r["params"]
        ps = (f"rev≥{p['min_rev_yoy']} gm≥{p['min_gm']} "
              f"dna≥{p['min_dna']} th={p['base_thresh']} "
              f"h≥{p['min_hist']} acc={int(p['require_accel'])}")
        log.info(
            f"  {i:>4}  {r['score']:>6.1f}  "
            f"{m['precision']:>6.1f}%  {m['miss_rate']:>6.1f}%  {m['error_rate']:>6.1f}%  "
            f"{m['avg_lead']:>6.0f}d  {m['n_mature']:>5}  {m['n_signals']:>5}  {ps}"
        )

    log.info("")
    best = results[0]
    b    = best["metrics"]
    bp   = best["params"]
    log.info("── 最优参数 ─────────────────────────────────────────────────────────────")
    log.info(f"  综合评分:  {best['score']}")
    log.info(f"  准确率:    {b['precision']}%  (成熟信号中+50%成功率, N={b['n_mature']})")
    log.info(f"  遗漏率:    {b['miss_rate']}%  ({b['n_missed_gt']}/{b['n_gt']} 质量爆发股遗漏)")
    log.info(f"  错误率:    {b['error_rate']}%")
    log.info(f"  提前量:    {b['avg_lead']} 天  (信号到+50%的平均天数)")
    log.info(f"  总信号数:  {b['n_signals']}  成熟可评估: {b['n_mature']}")
    log.info(f"  参数: min_rev_yoy={bp['min_rev_yoy']}  min_gm={bp['min_gm']}  "
             f"min_dna={bp['min_dna']}  base_thresh={bp['base_thresh']}  "
             f"min_hist={bp['min_hist']}  require_accel={bp['require_accel']}")
    log.info("")
    log.info("── 当前基准对比 ──────────────────────────────────────────────────────────")
    log.info("  基准参数: rev≥20 gm≥0 dna≥68 thresh=78 hist≥3 accel=False")
    log.info("  基准指标: 精确率15.8%  止损率84.2%  (3年回测，WIN/已结算定义)")
    log.info("=" * 90)


def apply_best_params(best_params: dict):
    """将最优参数写入 historical_backtest.py"""
    if not BACKTEST_PY.exists():
        log.error(f"找不到 {BACKTEST_PY}")
        return False
    src     = BACKTEST_PY.read_text(encoding="utf-8")
    new_src = src
    subs = [
        (r"^MIN_DNA\s+=\s+\d+",     f"MIN_DNA     = {best_params['min_dna']}"),
        (r"^BASE_THRESH\s+=\s+\d+", f"BASE_THRESH = {best_params['base_thresh']}"),
        (r"^MIN_HIST\s+=\s+\d+",    f"MIN_HIST    = {best_params['min_hist']}"),
    ]
    for pat, rep in subs:
        new_src = re.sub(pat, rep, new_src, flags=re.MULTILINE)

    new_rev = best_params["min_rev_yoy"]
    new_gm  = best_params["min_gm"]
    new_src = re.sub(r"if rev_yoy < \d+: return False",
                     f"if rev_yoy < {new_rev}: return False", new_src)
    new_src = re.sub(r"if gm < [-\d]+:\s+return False, f\"负毛利",
                     f"if gm < {new_gm}:       return False, f\"负毛利", new_src)

    BACKTEST_PY.write_text(new_src, encoding="utf-8")
    log.info(f"✅ 已写入 {BACKTEST_PY}")
    log.info(f"   MIN_DNA={best_params['min_dna']}  BASE_THRESH={best_params['base_thresh']}  "
             f"MIN_HIST={best_params['min_hist']}  "
             f"L1: rev≥{new_rev}%  gm≥{new_gm}%")
    return True


# ══════════════════════════════════════════════════════════════
#  主函数
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start",      default=None)
    parser.add_argument("--end",        default=date.today().isoformat())
    parser.add_argument("--apply-best", action="store_true")
    parser.add_argument("--show-signals", action="store_true",
                        help="打印最优参数下的信号明细")
    args = parser.parse_args()

    log.info("=" * 68)
    log.info("  NASDAQ Hunter — 模型参数调优引擎 v2")
    log.info(f"  成功门槛: +{SUCCESS_GAIN*100:.0f}% / {SUCCESS_WINDOW}天")
    log.info(f"  成熟度窗口: {EVAL_WINDOW} 天")
    log.info("=" * 68)

    if not DB_PATH.exists():
        sys.exit("❌ 数据库不存在")

    conn = sqlite3.connect(str(DB_PATH))
    prices                       = preload_prices(conn)
    funds, fund_fdates, prev_map = preload_fundamentals(conn)
    macro_dates, macro_rates     = preload_macro(conn)
    conn.close()

    all_dates = sorted({dt for p in prices.values() for dt in p})
    start = args.start or all_dates[0]
    end   = args.end
    log.info(f"回测周期: {start} → {end}")

    t0 = time.time()
    results, gt_cache = run_grid_search(
        prices, funds, fund_fdates, prev_map,
        macro_dates, macro_rates, start, end
    )
    log.info(f"网格搜索完成  耗时: {time.time()-t0:.1f}s")

    print_results(results, top_n=15)

    if args.show_signals or True:   # 默认打印最优参数信号明细
        analyze_best_signals(
            results[0]["params"], prices, funds, fund_fdates, prev_map,
            macro_dates, macro_rates, start, end
        )

    OUT_TUNING.write_text(
        json.dumps(
            {"start": start, "end": end,
             "top_results": results[:30],
             "best_params": results[0]["params"],
             "best_metrics": results[0]["metrics"]},
            ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )
    log.info(f"\n调优结果已保存: {OUT_TUNING}")

    if args.apply_best:
        apply_best_params(results[0]["params"])
        log.info("建议重新运行: python3 historical_backtest.py")

    log.info("\n完成！下一步:")
    log.info("  python3 model_tuner.py --apply-best  # 应用最优参数")
    log.info("  python3 historical_backtest.py        # 验证改进效果")


if __name__ == "__main__":
    main()

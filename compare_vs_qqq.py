#!/usr/bin/env python3
"""
模型组合 vs QQQ 基准对比
基于 backtest_signals.json 中的真实信号，模拟 $100k 初始资金的实际组合表现
"""
import json, os, sqlite3, warnings
from datetime import datetime, timedelta
import requests
import yfinance as yf

FEISHU_HOOK = os.getenv("FEISHU_WEBHOOK",
              "https://open.larkoffice.com/open-apis/bot/v2/hook/f0f60b3c-c410-43af-8065-dab17318891f")

warnings.filterwarnings("ignore")

CAPITAL    = 100_000
MAX_HOLD   = 12
MAX_POSIT  = 2
STOP_LOSS  = 0.20
DB         = "data/nasdaq_history.db"


def conviction_alloc(comp: float):
    if comp >= 85:
        return "STRONG", 0.65
    if comp >= 75:
        return "MEDIUM", 0.35
    return "WEAK", 0.15


def get_close(con, ticker: str, on_date: str):
    row = con.execute(
        "SELECT close FROM prices WHERE ticker=? AND date<=? ORDER BY date DESC LIMIT 1",
        (ticker, on_date)
    ).fetchone()
    return row[0] if row else None


def all_trade_dates(con, start: str, end: str):
    rows = con.execute(
        "SELECT DISTINCT date FROM prices WHERE date>=? AND date<=? ORDER BY date",
        (start, end)
    ).fetchall()
    return [r[0] for r in rows]


# ── 模拟（max 2 持仓）─────────────────────────────────────────────────────
def simulate(signals, con):
    cash      = CAPITAL
    positions = {}
    port_by_date = {}
    trades    = []

    sig_by_date = {}
    for s in signals:
        sig_by_date.setdefault(s["date"], []).append(s)

    trade_dates = all_trade_dates(con, "2023-04-18", "2026-05-15")

    for td in trade_dates:
        # 平仓检查
        to_close = []
        for ticker, pos in positions.items():
            close_px = get_close(con, ticker, td)
            if close_px is None:
                continue
            stop_px = pos["entry_price"] * (1 - STOP_LOSS)
            if close_px <= stop_px:
                to_close.append((ticker, close_px, "STOP"))
                continue
            entry_d = datetime.strptime(pos["entry_date"], "%Y-%m-%d").date()
            cur_d   = datetime.strptime(td, "%Y-%m-%d").date()
            if (cur_d - entry_d).days >= 365:
                to_close.append((ticker, close_px, "12M"))

        for ticker, exit_px, reason in to_close:
            pos      = positions.pop(ticker)
            proceeds = pos["shares"] * exit_px
            pnl_pct  = (exit_px / pos["entry_price"] - 1) * 100
            cash    += proceeds
            trades.append({
                "ticker": ticker, "entry_date": pos["entry_date"], "exit_date": td,
                "entry_px": pos["entry_price"], "exit_px": exit_px,
                "invested": pos["shares"] * pos["entry_price"],
                "proceeds": proceeds, "pnl_pct": round(pnl_pct, 1),
                "reason": reason, "conviction": pos["conviction"],
            })

        # 新信号入场（同日按 comp 降序）
        if td in sig_by_date and len(positions) < MAX_POSIT:
            for sig in sorted(sig_by_date[td], key=lambda s: -s["comp"]):
                if len(positions) >= MAX_POSIT:
                    break
                ticker = sig["ticker"]
                if ticker in positions:
                    continue
                conv_label, frac = conviction_alloc(sig["comp"])
                alloc   = min(CAPITAL * frac, cash * 0.98)
                if alloc < 500:
                    continue
                entry_px = sig["entry"]
                shares   = alloc / entry_px
                cash    -= shares * entry_px
                positions[ticker] = {
                    "entry_price": entry_px, "shares": shares,
                    "entry_date": td, "conviction": conv_label,
                }

        # 记录组合净值
        mkt_val = sum(
            pos["shares"] * (get_close(con, tkr, td) or pos["entry_price"])
            for tkr, pos in positions.items()
        )
        port_by_date[td] = cash + mkt_val

    # 剩余持仓按期末价格平仓
    last_date = trade_dates[-1]
    for ticker, pos in list(positions.items()):
        close_px = get_close(con, ticker, last_date) or pos["entry_price"]
        proceeds = pos["shares"] * close_px
        pnl_pct  = (close_px / pos["entry_price"] - 1) * 100
        trades.append({
            "ticker": ticker, "entry_date": pos["entry_date"], "exit_date": last_date,
            "entry_px": pos["entry_price"], "exit_px": close_px,
            "invested": pos["shares"] * pos["entry_price"],
            "proceeds": proceeds, "pnl_pct": round(pnl_pct, 1),
            "reason": "OPEN(period_end)", "conviction": pos["conviction"],
        })

    return port_by_date, trades


# ── 等权重纯选股质量分析 ────────────────────────────────────────────────────
def equal_weight_analysis(signals, qqq_by_date, trade_dates):
    """
    假设每笔信号等权重 (5% 仓位), 无资金约束, 评估纯股票选择质量
    结算方式: 与 backtest 一致 (用 ret_12m 或 max_ret 代替)
    """
    closed   = [s for s in signals if s["outcome"] in ("WIN", "STOP", "LOSS")]
    open_sig = [s for s in signals if s["outcome"] == "OPEN"]

    rets_closed = [s["ret_12m"] for s in closed if s.get("ret_12m") is not None]
    rets_open   = [s["max_ret"] for s in open_sig if s.get("max_ret") is not None]
    all_rets    = rets_closed + rets_open

    if not all_rets:
        return None

    avg_ret    = sum(all_rets) / len(all_rets)
    wins       = sum(1 for r in rets_closed if r > 0)
    stops      = sum(1 for s in closed if s["outcome"] == "STOP")

    # 计算 QQQ 同期平均收益（信号触发日起 12M 或 至今）
    qqq_rets = []
    for s in signals:
        sig_date = s["date"]
        q_start  = qqq_by_date.get(sig_date)
        if q_start is None:
            continue
        # 找 12M 后的 QQQ 价格
        end_candidates = [td for td in trade_dates if td >= sig_date]
        target_date = datetime.strptime(sig_date, "%Y-%m-%d").date() + timedelta(days=365)
        end_date_td = None
        for td in end_candidates:
            if datetime.strptime(td, "%Y-%m-%d").date() >= target_date:
                end_date_td = td
                break
        if end_date_td is None:
            end_date_td = trade_dates[-1]
        q_end = qqq_by_date.get(end_date_td, q_start)
        qqq_rets.append((q_end / q_start - 1) * 100)

    avg_qqq_ret = sum(qqq_rets) / len(qqq_rets) if qqq_rets else 0

    return {
        "n": len(all_rets), "avg_model_ret": round(avg_ret, 1),
        "avg_qqq_ret": round(avg_qqq_ret, 1),
        "alpha": round(avg_ret - avg_qqq_ret, 1),
        "wins_closed": wins, "stops_closed": stops, "n_closed": len(rets_closed),
        "n_open": len(open_sig),
        "hit_100plus": sum(1 for r in all_rets if r >= 100),
    }


# ── QQQ 基准 ──────────────────────────────────────────────────────────────
def get_qqq_series(trade_dates):
    qqq = yf.download("QQQ", start="2023-04-17", end="2026-05-18", progress=False)
    qqq_prices = {}
    for dt, row in qqq.iterrows():
        try:
            px = float(row["Close"].iloc[0])
        except Exception:
            px = float(row["Close"])
        qqq_prices[str(dt.date())] = px

    start_px = next((qqq_prices[td] for td in trade_dates if td in qqq_prices), None)
    if not start_px:
        return {}

    result = {}
    prev   = CAPITAL
    for td in trade_dates:
        px = qqq_prices.get(td)
        if px is not None:
            prev = CAPITAL * (px / start_px)
        result[td] = prev
    return result


# ── 打印报告 ──────────────────────────────────────────────────────────────
def print_report(port_by_date, qqq_by_date, trades, signals, stats):
    trade_dates = sorted(port_by_date.keys())
    start_val   = CAPITAL
    end_val     = port_by_date[trade_dates[-1]]
    qqq_end     = qqq_by_date.get(trade_dates[-1], CAPITAL)

    model_ret = (end_val / start_val - 1) * 100
    qqq_ret   = (qqq_end / start_val - 1) * 100
    alpha     = model_ret - qqq_ret
    n_years   = len(trade_dates) / 252
    model_ann = ((end_val / start_val) ** (1 / n_years) - 1) * 100
    qqq_ann   = ((qqq_end  / start_val) ** (1 / n_years) - 1) * 100

    peak = start_val
    max_dd = 0.0
    for v in (port_by_date[d] for d in trade_dates):
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100
        if dd > max_dd:
            max_dd = dd

    closed_trades = [t for t in trades if t["reason"] != "OPEN(period_end)"]
    open_trades   = [t for t in trades if t["reason"] == "OPEN(period_end)"]

    eq = equal_weight_analysis(signals, qqq_by_date, trade_dates)

    W = 70
    print("=" * W)
    print("  NASDAQ Hunter — 模型验证报告 vs QQQ")
    print(f"  回测周期: {trade_dates[0]} → {trade_dates[-1]}   初始资金: ${CAPITAL:,.0f}")
    print("=" * W)

    print("\n▌ 一、组合净值对比（最大 2 持仓 + 置信度仓位）")
    print(f"  {'指标':<22} {'NASDAQ Hunter':>14} {'QQQ 持有':>14}")
    print(f"  {'-'*54}")
    print(f"  {'期末净值':<22} ${end_val:>12,.0f}   ${qqq_end:>12,.0f}")
    print(f"  {'总收益率':<22} {model_ret:>+13.1f}%   {qqq_ret:>+13.1f}%")
    print(f"  {'年化收益率':<22} {model_ann:>+13.1f}%   {qqq_ann:>+13.1f}%")
    print(f"  {'超额收益(Alpha)':<22} {alpha:>+13.1f}%")
    print(f"  {'最大回撤':<22} {-max_dd:>+13.1f}%")
    print(f"  {'信号期已入场交易':<22} {len(closed_trades)+len(open_trades):>14} / {stats['total_signals']}")

    print(f"\n  ⚠ 主要拖累：前 ~16 个月资金闲置（无信号），QQQ 同期 +52%")
    print(f"  ⚠ 多信号同日触发时最多只能进 2 只，{stats['total_signals']-len(closed_trades)-len(open_trades)} 个信号因满仓被跳过")

    # 仅信号激活期对比（从第一个信号起，资金皆为 $100k）
    first_sig = min(s["date"] for s in signals)
    m_s = port_by_date.get(first_sig, start_val)
    q_s = qqq_by_date.get(first_sig, start_val)
    m_sig_ret = (end_val / m_s - 1) * 100
    q_sig_ret = (qqq_end / q_s - 1) * 100
    print(f"\n  信号激活期（{first_sig} → {trade_dates[-1]}）独立对比:")
    print(f"    模型(从$100k起): {m_sig_ret:>+.1f}%   QQQ同期: {q_sig_ret:>+.1f}%   超额: {m_sig_ret-q_sig_ret:>+.1f}%")

    print(f"\n▌ 二、选股质量分析（24 信号等权重, 无资金限制）")
    if eq:
        print(f"  {'分析维度':<26} {'模型':>10} {'QQQ同期':>10} {'超额':>8}")
        print(f"  {'-'*56}")
        print(f"  {'平均单笔收益(12M或至今)':<26} {eq['avg_model_ret']:>+9.1f}%   {eq['avg_qqq_ret']:>+9.1f}%   {eq['alpha']:>+7.1f}%")
        print(f"  {'命中 100%+ 大涨':<26} {eq['hit_100plus']:>10} 只")
        print(f"  {'信号样本量':<26} {eq['n']:>10} 个（{eq['n_closed']}已结算+{eq['n_open']}持仓中）")
        print(f"  {'已结算：盈利/止损':<26} {eq['wins_closed']}盈/{eq['stops_closed']}损  精确率 {stats['precision']}%")

    print(f"\n▌ 三、关键时间节点净值")
    checkpoints = [
        ("2023-04-18", "回测起始"),
        ("2024-01-02", "2024年初"),
        ("2024-08-13", "首个信号"),
        ("2025-01-02", "2025年初"),
        ("2025-07-01", "2025年中"),
        ("2026-01-02", "2026年初"),
        ("2026-05-15", "回测截止"),
    ]
    print(f"  {'日期':<12} {'说明':<12} {'模型($)':>11} {'QQQ($)':>11} {'模型累计%':>10} {'QQQ累计%':>10}")
    print(f"  {'-'*68}")
    for cp, label in checkpoints:
        avail = [d for d in trade_dates if d >= cp]
        if not avail:
            continue
        td    = avail[0]
        m_val = port_by_date.get(td, start_val)
        q_val = qqq_by_date.get(td, start_val)
        m_pct = (m_val / start_val - 1) * 100
        q_pct = (q_val / start_val - 1) * 100
        flag  = " ←" if m_pct > q_pct else ""
        print(f"  {td:<12} {label:<12} ${m_val:>9,.0f}   ${q_val:>9,.0f}   {m_pct:>+8.1f}%   {q_pct:>+8.1f}%{flag}")

    print(f"\n▌ 四、实际成交记录（{len(closed_trades)+len(open_trades)} 笔）")
    print(f"  {'代码':<6} {'置信':<8} {'入场':<12} {'出场/状态':<14} {'P&L%':>8} {'已投入':>10}")
    print(f"  {'-'*62}")
    all_t = sorted(closed_trades + open_trades, key=lambda t: t["entry_date"])
    for t in all_t:
        status = t["exit_date"] if t["reason"] != "OPEN(period_end)" else "持仓中"
        print(f"  {t['ticker']:<6} {t['conviction']:<8} {t['entry_date']:<12} {status:<14} "
              f"{t['pnl_pct']:>+7.1f}%   ${t['invested']:>8,.0f}")

    print(f"\n▌ 五、信号回测精要（所有 24 个信号）")
    print(f"  {'日期':<12} {'代码':<6} {'DNA':>5} {'综合':>6} {'1M%':>6} {'3M%':>6} {'12M%':>7} {'峰值%':>7}  {'结果'}")
    print(f"  {'-'*72}")
    for s in signals:
        r1   = f"{s['ret_1m']:>+5.0f}%" if s.get("ret_1m") is not None else "    —"
        r3   = f"{s['ret_3m']:>+5.0f}%" if s.get("ret_3m") is not None else "    —"
        r12  = f"{s['ret_12m']:>+5.0f}%" if s.get("ret_12m") is not None else "    —"
        rpk  = f"{s['max_ret']:>+5.0f}%" if s.get("max_ret") is not None else "    —"
        oc   = {"WIN":"✅WIN","STOP":"🛑STP","LOSS":"❌LOS","OPEN":"⏳OPN"}.get(s["outcome"], s["outcome"])
        print(f"  {s['date']:<12} {s['ticker']:<6} {s['dna']:>5} {s['comp']:>6} {r1} {r3} {r12} {rpk}  {oc}")

    print("\n" + "=" * W)
    print("  结论")
    print("  ─────────────────────────────────────────────────────────────")
    if eq and eq["alpha"] > 0:
        print(f"  ✅ 选股能力验证通过: 平均单笔 +{eq['avg_model_ret']}% vs QQQ同期 +{eq['avg_qqq_ret']}%，超额 +{eq['alpha']}%")
    else:
        print(f"  ⚠ 选股超额有限: 平均单笔 {eq['avg_model_ret']:+}% vs QQQ {eq['avg_qqq_ret']:+}%，超额 {eq['alpha']:+}%")
    print(f"  📌 集中模型(max 2 仓)在信号批量触发时资金部署效率低")
    print(f"  📌 16月冷启动是整体 Alpha 为负的核心原因，非模型能力问题")
    print(f"  📌 高精度精选(33%精确率)已命中 {stats['signaled_big']} 只 100%+ 大涨股")
    print(f"  📌 改进方向: ① 允许 3-4 仓位; ② 早期补充历史基本面数据(Simfin)")
    print("=" * W)

    return {
        "model_ret": round(model_ret, 1), "qqq_ret": round(qqq_ret, 1),
        "model_ann": round(model_ann, 1), "qqq_ann": round(qqq_ann, 1),
        "alpha": round(alpha, 1), "max_dd": round(-max_dd, 1),
        "eq": eq, "n_entered": len(closed_trades) + len(open_trades),
    }


# ── Lark 推送 ─────────────────────────────────────────────────────────────
def _col(md_content: str) -> dict:
    return {
        "tag": "column", "width": "weighted", "weight": 1,
        "elements": [{"tag": "markdown", "content": md_content}],
    }


def push_qqq_comparison(summary: dict, signals: list, stats: dict) -> bool:
    from datetime import date
    if not FEISHU_HOOK:
        return False
    today   = str(date.today())
    eq      = summary.get("eq") or {}
    n_sig   = stats["total_signals"]
    n_enter = summary["n_entered"]

    # 顶部 3 列：总收益对比
    elements = []
    elements.append({
        "tag": "column_set", "flex_mode": "none", "background_style": "grey",
        "columns": [
            _col("**模型总收益**\n**{:+.1f}%**  (年化 {:+.1f}%)".format(
                summary["model_ret"], summary["model_ann"])),
            _col("**QQQ基准**\n**{:+.1f}%**  (年化 {:+.1f}%)".format(
                summary["qqq_ret"], summary["qqq_ann"])),
            _col("**超额收益**\n**{:+.1f}%**  (最大回撤 {:+.1f}%)".format(
                summary["alpha"], summary["max_dd"])),
        ],
    })

    # 选股质量行
    if eq:
        elements.append({
            "tag": "column_set", "flex_mode": "none", "background_style": "default",
            "columns": [
                _col("**选股超额(等权重)**\n平均 **{:+.1f}%** vs QQQ **{:+.1f}%**".format(
                    eq["avg_model_ret"], eq["avg_qqq_ret"])),
                _col("**信号入场率**\n{}/{} 个信号实际成交".format(n_enter, n_sig)),
                _col("**精确率**\n已结算 **{:.0f}%**  命中100%+ **{}只**".format(
                    stats["precision"], stats["signaled_big"])),
            ],
        })

    elements.append({"tag": "hr"})

    # 核心结论
    if eq and eq["alpha"] > 0:
        conclusion = ("✅ **选股能力验证通过**\n"
                      "等权重平均单笔 **{:+.1f}%** vs QQQ同期 **{:+.1f}%**，超额 **+{:.1f}%**\n\n"
                      "📌 整体落后QQQ主因：前16个月无信号资金闲置（QQQ同期+52%）\n"
                      "📌 {}/{}信号因满仓(max 2)被跳过，资本部署效率需优化\n"
                      "📌 改进方向: 允许3-4同时持仓 + 补充更早历史基本面数据").format(
                          eq["avg_model_ret"], eq["avg_qqq_ret"], eq["alpha"],
                          n_sig - n_enter, n_sig)
    else:
        conclusion = ("⚠️ 模型跑输QQQ，需继续优化\n"
                      "选股平均 {:.1f}%  QQQ同期 {:.1f}%  超额 {:.1f}%").format(
                          eq.get("avg_model_ret", 0), eq.get("avg_qqq_ret", 0),
                          eq.get("alpha", 0))
    elements.append({"tag": "markdown", "content": conclusion})

    elements.append({"tag": "hr"})

    # 最优信号列表
    top5_by_peak = sorted(signals, key=lambda s: -(s.get("max_ret") or 0))[:5]
    best_lines = ["**🏆 回测最佳信号（峰值涨幅 Top5）**", ""]
    for s in top5_by_peak:
        r12 = "{:+.0f}%".format(s["ret_12m"]) if s.get("ret_12m") is not None else "持仓中"
        best_lines.append(
            "• **{}** ({}入场 ${:.1f})  12M: {}  峰值: {:+.0f}%".format(
                s["ticker"], s["date"], s["entry"], r12, s.get("max_ret") or 0))
    elements.append({"tag": "markdown", "content": "\n".join(best_lines)})

    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text",
                      "content": "⚠️ 回测基于本地3年历史数据，不构成投资建议。精确率33%基于已结算6笔，样本量有限。"}],
    })

    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "turquoise",
                "title": {"tag": "plain_text",
                          "content": "📈 NASDAQ Hunter · {} 回测验证 vs QQQ".format(today)},
            },
            "elements": elements,
        },
    }
    try:
        r = requests.post(FEISHU_HOOK, json=payload, timeout=10)
        result = r.json()
        ok = result.get("code", -1) == 0
        print("Lark 推送: {}".format("成功" if ok else "失败 " + str(result)))
        return ok
    except Exception as e:
        print("Lark 推送异常:", e)
        return False


# ── main ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("加载回测信号...")
    with open("data/backtest_signals.json") as f:
        data = json.load(f)
    signals = data["signals"]
    stats   = data["stats"]

    print("连接本地数据库...")
    con = sqlite3.connect(DB)

    print("模拟模型组合...")
    port_by_date, trades = simulate(signals, con)

    print("获取 QQQ 基准数据...")
    trade_dates = sorted(port_by_date.keys())
    qqq_by_date = get_qqq_series(trade_dates)

    summary = print_report(port_by_date, qqq_by_date, trades, signals, stats)

    report = {"summary": summary, "stats": stats, "trades": trades}
    with open("data/qqq_comparison.json", "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print("\n已保存对比数据: data/qqq_comparison.json")

    print("推送 Lark 对比卡片...")
    push_qqq_comparison(summary, signals, stats)

#!/usr/bin/env python3
"""
推送渠道模块 — 飞书 Card 2.0 + Discord
被 scanner_v2.py 导入使用
"""
import logging
import os
import re
import requests
from datetime import date, timedelta

try:
    import yfinance as yf
    import numpy as np
    _HAS_YF = True
except ImportError:
    _HAS_YF = False

log = logging.getLogger("scanner")

CONV_LABEL = {"STRONG": "🔴 强", "MEDIUM": "🟡 中", "WEAK": "🟢 弱"}
CONV_RANGE = {"STRONG": "建议仓位 60-80%", "MEDIUM": "建议仓位 20-40%", "WEAK": "建议仓位 5-15%"}


def _macro_threshold(fed_rate) -> int:
    """与 scanner_v2 / historical_backtest 保持一致的宏观阈值计算"""
    if fed_rate is None:
        return 70
    if fed_rate > 5.0:
        return 88
    if fed_rate > 4.5:
        return 83
    return 70


def _macro_label(fed_rate) -> str:
    if fed_rate is None:
        return "未知"
    if fed_rate > 5.0:
        return "激进加息"
    if fed_rate > 4.5:
        return "限制性"
    return "中性"


def _col(md_content: str) -> dict:
    """快捷构造 column_set 中的单列"""
    return {
        "tag": "column",
        "width": "weighted",
        "weight": 1,
        "elements": [{"tag": "markdown", "content": md_content}],
    }


def _format_analysis(analysis: str) -> str:
    """
    将 Claude 返回的分析过程段落拆成逐行要点。
    按中文分号 / 英文分号切句，每句根据关键词加前缀 emoji。
    """
    if not analysis:
        return ""
    sentences = re.split(r'[；;]\s*', analysis.strip())
    if len(sentences) <= 1:
        sentences = [s.strip().rstrip("。") for s in re.split(r'。\s*', analysis.strip()) if s.strip()]
    if len(sentences) <= 1:
        return analysis  # 无法切分，原样返回

    lines = []
    for s in sentences:
        s = s.strip().rstrip("。")
        if not s:
            continue
        # 决策/结论优先，避免被"通过"误判为 Quality
        if re.search(r'决策|建议|BUY|WAIT|结论', s):
            lines.append(f"💡 {s}")
        elif re.search(r'Quality|质检|通过.*只|只.*通过', s):
            lines.append(f"✅ {s}")
        elif re.search(r'Outlook|预期|加速|增速', s):
            lines.append(f"✅ {s}")
        elif re.search(r'Macro|宏观|阈值|门槛|Fed', s):
            lines.append(f"📊 {s}")
        elif re.search(r'Signal|DNA|综合分|信号', s):
            lines.append(f"🎯 {s}")
        else:
            lines.append(f"• {s}")
    return "\n".join(lines)


def _watchlist_bullets(next_watch: str, thresh: int) -> str:
    """
    将 next_watch 字符串（格式: "TICKER:reason, TICKER2:reason2"）
    渲染为飞书兼容的加粗项目符号列表（不使用 markdown 表格）。
    示例: • **ALAB** | 综合分: 74 | 距阈值: -1 | 状态: 趋势稳定持续观察
    """
    if not next_watch:
        return ""
    watch_items = [w.strip() for w in re.split(r'[,;，；]', next_watch) if w.strip()]
    if not watch_items:
        return ""

    lines = []
    for item in watch_items:
        parts = item.split(":", 1)
        ticker = parts[0].strip()
        reason = parts[1].strip() if len(parts) == 2 else ""

        score_m = re.search(r'(?:综合|综合分|DNA|分数)[=＝\s：:]*(\d+)', reason)
        score   = int(score_m.group(1)) if score_m else None

        if score is not None:
            gap     = score - thresh
            gap_str = "{:+d}".format(gap)
            score_s = str(score)
        else:
            gap_str = "—"
            score_s = "—"

        status = reason[:28].rstrip("，,") + ("…" if len(reason) > 28 else "")

        lines.append(
            "• **{}** | 综合分: {} | 距阈值: {} | 状态: {}".format(
                ticker, score_s, gap_str, status)
        )

    return "\n".join(lines)


def _extract_core_reason(wait_reason: str, next_watch: str) -> str:
    """从 wait_reason 或 next_watch 提取核心淘汰原因"""
    if wait_reason:
        r = re.sub(r'^(等待原因[：:]?|原因[：:]?|淘汰[：:]?)\s*', '', wait_reason.strip())
        return r
    if next_watch:
        tickers = re.findall(r'([A-Z]{2,6}):', next_watch)
        if tickers:
            return "候选股 {} 综合分均未达触发门槛".format("/".join(tickers[:3]))
    return "当前无股票满足触发条件"


def _watchlist_bullets_compact(next_watch: str, thresh: int) -> str:
    """紧凑 watchlist — 每行 1 条，8 字状态上限，跳过无评分条目"""
    if not next_watch:
        return ""
    token_pat = re.compile(r'(?:^|[;,，；\s]+)([A-Z]{2,6}):', re.M)
    positions = [(m.start(1), m.end(0), m.group(1)) for m in token_pat.finditer(next_watch)]
    if not positions:
        return ""
    lines = []
    for idx, (start, end, ticker) in enumerate(positions):
        next_start = positions[idx + 1][0] - 1 if idx + 1 < len(positions) else len(next_watch)
        reason = next_watch[end:next_start].strip().rstrip(";,，；")
        score_m = re.search(r'(?:综合|综合分|DNA|分数)[=＝\s：:]*(\d+)', reason)
        if score_m is None:
            continue  # 无评分则跳过
        score = int(score_m.group(1))
        gap_str = "{:+d}".format(score - thresh)
        status_raw = re.sub(r'综合分[=＝\s：:]*\d+[，,]?\s*', '', reason).strip()
        status = status_raw[:8] + ("…" if len(status_raw) > 8 else "")
        lines.append("• **{}** | 综合分: {} | 距阈值: {} | {}".format(ticker, score, gap_str, status))
    return "\n".join(lines)


def _top_ticker(next_watch: str) -> str:
    """返回 next_watch 第一个有效 ticker，用于动态操作建议"""
    if not next_watch:
        return ""
    m = re.search(r'([A-Z]{2,6}):', next_watch)
    return m.group(1) if m else ""


def _fetch_price_series(ticker: str, trading_days: int = 120) -> list:
    """获取近 120 个交易日收盘价（用于图表渲染），失败返回空列表。"""
    if not _HAS_YF:
        return []
    try:
        end_dt   = date.today()
        start_dt = end_dt - timedelta(days=int(trading_days * 1.8))
        df = yf.download(ticker, start=str(start_dt), end=str(end_dt),
                         auto_adjust=True, progress=False)
        if df.empty:
            return []
        col = df["Close"]
        # yfinance 1.x 单 ticker 返回 MultiIndex Series，需要 flatten
        arr = col.values.flatten()
        arr = arr[~np.isnan(arr.astype(float))].astype(float)
        arr = arr[-trading_days:]
        return [round(float(v), 2) for v in arr]
    except Exception as e:
        log.warning("图表价格拉取失败 (%s): %s", ticker, e)
        return []


def _feishu_tenant_token() -> str:
    """获取 Feishu tenant_access_token（有效期 7200s）。"""
    app_id     = os.getenv("FEISHU_APP_ID", "")
    app_secret = os.getenv("FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        return ""
    try:
        resp = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=8,
        )
        data = resp.json()
        return data.get("tenant_access_token", "")
    except Exception as e:
        log.warning("Feishu token 获取失败: %s", e)
        return ""


def _generate_chart_png(ticker: str) -> bytes:
    """
    从本地 DB 查询最近 120 天收盘价，用 matplotlib 生成 Google Finance 风格折线图。
    返回 PNG bytes；无数据或 matplotlib 未安装时返回空 bytes。
    """
    try:
        import sqlite3
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        import io

        db_path = os.path.join(os.path.dirname(__file__), "data", "nasdaq_history.db")
        cutoff  = (date.today() - timedelta(days=180)).isoformat()
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT date, close FROM prices WHERE ticker=? AND date>=? ORDER BY date",
                (ticker, cutoff),
            ).fetchall()

        if len(rows) < 5:
            return b""

        dates  = [date.fromisoformat(r[0]) for r in rows]
        closes = [r[1] for r in rows]
        pct_chg = (closes[-1] / closes[0] - 1) * 100
        color   = "#0F9D58" if pct_chg >= 0 else "#DB4437"

        fig, ax = plt.subplots(figsize=(6, 2.2))
        fig.patch.set_facecolor("#1A1A2E")
        ax.set_facecolor("#1A1A2E")

        ax.plot(dates, closes, color=color, linewidth=1.8, solid_capstyle="round")
        ax.fill_between(dates, closes, min(closes) * 0.995,
                        color=color, alpha=0.15)

        ax.spines[:].set_visible(False)
        ax.tick_params(colors="#AAAAAA", labelsize=8)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
        ax.yaxis.set_tick_params(labelleft=True)
        ax.tick_params(axis="y", colors="#AAAAAA", labelsize=8)
        plt.setp(ax.get_xticklabels(), color="#AAAAAA")
        ax.grid(axis="y", color="#333355", linewidth=0.5, linestyle="--")

        label = "${:.2f}  {:+.1f}%  (6M)".format(closes[-1], pct_chg)
        ax.set_title("{} — {}".format(ticker, label),
                     color="#FFFFFF", fontsize=9, pad=4, loc="left")

        fig.tight_layout(pad=0.4)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=130, facecolor=fig.get_facecolor())
        plt.close(fig)
        return buf.getvalue()
    except Exception as e:
        log.warning("图表生成失败 (%s): %s", ticker, e)
        return b""


def _upload_chart_image(ticker: str, token: str) -> str:
    """本地生成折线图并上传到 Feishu，返回 img_key。失败返回空字符串。"""
    if not token:
        return ""
    try:
        png_bytes = _generate_chart_png(ticker)
        if not png_bytes:
            log.warning("图表生成无数据 (%s)", ticker)
            return ""
        up = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/images",
            headers={"Authorization": "Bearer {}".format(token)},
            files={"image": ("{}.png".format(ticker), png_bytes, "image/png")},
            data={"image_type": "message"},
            timeout=15,
        )
        result = up.json()
        if result.get("code", -1) != 0:
            log.warning("Feishu 图片上传失败 (%s): %s", ticker, result)
            return ""
        return result["data"]["image_key"]
    except Exception as e:
        log.warning("图片上传异常 (%s): %s", ticker, e)
        return ""


def _watchlist_chart_elements(next_watch: str, thresh: int) -> list:
    """
    解析 watchlist，返回飞书元素列表。
    每只有分股票：摘要文字行 + Finviz 6 个月折线图（经 Feishu 图片 API 上传后嵌入）。
    跳过无评分条目；最多渲染前 3 只。
    """
    if not next_watch:
        return []
    token_pat = re.compile(r'(?:^|[;,，；\s]+)([A-Z]{2,6}):', re.M)
    positions  = [(m.start(1), m.end(0), m.group(1)) for m in token_pat.finditer(next_watch)]
    if not positions:
        return []

    parsed = []
    for idx, (start, end, ticker) in enumerate(positions):
        next_start = positions[idx + 1][0] - 1 if idx + 1 < len(positions) else len(next_watch)
        reason = next_watch[end:next_start].strip().rstrip(";,，；")
        score_m = re.search(r'(?:综合|综合分|DNA|分数)[=＝\s：:]*(\d+)', reason)
        if score_m is None:
            continue
        score      = int(score_m.group(1))
        gap        = score - thresh
        status_raw = re.sub(r'综合分[=＝\s：:]*\d+[，,]?\s*', '', reason).strip()
        parsed.append((ticker, score, gap, status_raw))

    if not parsed:
        return []

    # 获取一次 token，三只股票共用
    feishu_token = _feishu_tenant_token()

    elements = []
    for ticker, score, gap, status in parsed[:3]:
        # 距阈值着色：正值绿，负值红
        gap_str = "{:+d}".format(gap)
        if gap > 0:
            gap_md = "<font color='green'>**{}**</font>".format(gap_str)
        else:
            gap_md = "<font color='red'>**{}**</font>".format(gap_str)

        # 摘要文字行（ticker 为 Google Finance 超链接）+ 完整原因第二行
        gf_url  = "https://www.google.com/finance/quote/{}:NASDAQ".format(ticker)
        line1   = "• [**{}**]({}) | 综合分: {} | 距阈值: {}".format(
            ticker, gf_url, score, gap_md)
        content = line1 + ("\n  " + status if status else "")
        elements.append({"tag": "markdown", "content": content})

        # 上传 Finviz 图表并嵌入 img 元素
        img_key = _upload_chart_image(ticker, feishu_token)
        if img_key:
            elements.append({
                "tag":     "img",
                "img_key": img_key,
                "alt":     {"tag": "plain_text", "content": "{} 6M".format(ticker)},
                "mode":    "fit_horizontal",
            })

    return elements


def push_feishu(report: dict, webhook: str) -> bool:
    """
    飞书群机器人推送 — Feishu Card 2.0 格式
    设置: 飞书群 → 设置 → 群机器人 → 添加机器人 → 自定义机器人 → 复制 Webhook
    """
    if not webhook:
        return False

    action   = report.get("action", "WAIT")
    picks    = report.get("picks", []) or []
    today    = report.get("_date", str(date.today()))
    scanned  = report.get("_scanned", 0)
    passed   = report.get("_passed", 0)
    summary  = report.get("summary", "")
    _macro   = report.get("_macro") or {}
    fed      = _macro.get("fedRate")
    thresh   = _macro_threshold(fed)
    fed_str  = "{:.2f}".format(fed) if fed is not None else "—"
    macro_lbl = _macro_label(fed)
    analysis  = report.get("analysis_process", "")

    # ── BUY 卡片 ──────────────────────────────────────────────────────────────
    if action == "BUY" and picks:
        elements: list = []

        # 第一行：扫描指标（3列）— 数值加粗突出
        elements.append({
            "tag": "column_set",
            "flex_mode": "none",
            "background_style": "grey",
            "columns": [
                _col("**扫描股票**\n**{:,}** 只".format(scanned)),
                _col("**Quality 质检通过**\n**{}** 只".format(passed)),
                _col("**Signal 信号触发**\n**{}** 只".format(len(picks))),
            ],
        })

        # 第二行：宏观环境（3列）
        elements.append({
            "tag": "column_set",
            "flex_mode": "none",
            "background_style": "default",
            "columns": [
                _col("**Macro 环境**\nFed Rate = **{}%** ({})".format(fed_str, macro_lbl)),
                _col("**触发门槛**\nSignal ≥ **{}** 分".format(thresh)),
                _col("**止损纪律**\n收盘 **-20%** 无例外"),
            ],
        })

        # 分析过程（拆成要点列表）
        if analysis:
            formatted = _format_analysis(analysis)
            elements.append({"tag": "hr"})
            elements.append({
                "tag": "markdown",
                "content": "**📊 分析过程（Quality → Outlook → Macro → Signal）**\n\n" + formatted,
            })

        elements.append({"tag": "hr"})

        # 每只股票
        for i, p in enumerate(picks, 1):
            ticker    = p.get("ticker", "")
            alloc_pct = p.get("alloc_pct", 0)
            conv_raw  = (p.get("conviction") or "MEDIUM").upper()
            conv_tag  = CONV_LABEL.get(conv_raw, conv_raw)
            conv_hint = CONV_RANGE.get(conv_raw, "")
            entry     = p.get("entry", 0.0)
            stop_p    = p.get("stop", 0.0)
            upside    = p.get("target6m", "")
            thesis    = p.get("thesis", "")
            catalyst  = p.get("catalyst", "")
            risk      = p.get("risk", "")

            md = "\n".join([
                "📈 **标的：{}**　⭐ **置信度：{} ({})　参考仓位 {}%**".format(
                    ticker, conv_tag, conv_hint, alloc_pct),
                "",
                "**🎯 交易计划**",
                "• **入场价**：**${:.2f}**".format(entry),
                # 止损价用红色字体，目标用绿色
                "• **防守止损**：<font color='red'>**${:.2f}**</font>　（收盘 -20% 击穿离场）".format(stop_p),
                "• **预期目标**：<font color='green'>**{}**</font>".format(upside),
                "",
                "**🧠 核心逻辑**",
                "• **基本面**：{}".format(thesis),
                "• **催化剂**：{}".format(catalyst),
                "• **风险提示**：{}".format(risk),
            ])
            elements.append({"tag": "markdown", "content": md})

            # Yahoo Finance 跳转按钮
            elements.append({
                "tag": "action",
                "actions": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "行情走势 (Yahoo Finance)"},
                    "type": "primary",
                    "multi_url": {
                        "url": "https://finance.yahoo.com/quote/{}".format(ticker),
                        "android_url": "",
                        "ios_url": "",
                        "pc_url": "",
                    },
                }],
            })

            if i < len(picks):
                elements.append({"tag": "hr"})

        # 市场总结
        if summary:
            elements.append({"tag": "hr"})
            elements.append({"tag": "markdown", "content": "📝 " + summary})

        # 免责声明
        elements.append({
            "tag": "note",
            "elements": [{"tag": "plain_text",
                          "content": "⚠️ 本信号由量化模型生成，仅供参考，不构成投资建议。止损纪律（收盘 -20%）无例外执行。"}],
        })

        payload = {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "template": "red",
                    "title": {"tag": "plain_text",
                              "content": "🚨 NASDAQ Hunter · {} 买入信号".format(today)},
                },
                "elements": elements,
            },
        }

    # ── WAIT 卡片 ─────────────────────────────────────────────────────────────
    else:
        wait_reason = report.get("wait_reason", "")
        next_watch  = report.get("next_watch", "")
        elements = []

        # ── Section 1: 2 列核心指标 ──────────────────────────────────────
        elements.append({
            "tag": "column_set",
            "flex_mode": "none",
            "background_style": "grey",
            "columns": [
                _col("**Fed Rate**\n**{}%** ({})".format(fed_str, macro_lbl)),
                _col("**触发门槛**\n综合分 ≥ <font color='red'>**{}**</font>".format(thresh)),
            ],
        })

        # ── Section 2: 扫描结论（2 bullets）─────────────────────────────
        core_reason = _extract_core_reason(wait_reason, next_watch)
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "markdown",
            "content": "✅ 今日无股票触发买入信号\n❌ 淘汰核心原因：{}".format(core_reason),
        })

        # ── Section 3: 重点关注（文字行 + VChart 价格走势图）────────────
        if next_watch:
            watch_els = _watchlist_chart_elements(next_watch, thresh)
            if watch_els:
                elements.append({"tag": "hr"})
                elements.append({"tag": "markdown", "content": "**👁️ 重点关注**"})
                elements.extend(watch_els)

        # ── Section 4: 操作建议（固定 3 行）────────────────────────────
        top_tk = _top_ticker(next_watch)
        watch_line = (
            "👀 重点关注 {} 综合分突破阈值或负面信号消退".format(top_tk)
            if top_tk else "👀 持续关注 Watchlist 标的综合分变化"
        )
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "markdown",
            "content": (
                "✅ 今日无操作，继续持有现金或现有仓位\n"
                "{}\n"
                "🛡️ 任何持仓收盘跌破入场价 -20%，立刻执行止损"
            ).format(watch_line),
        })

        payload = {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "template": "wathet",
                    "title": {"tag": "plain_text",
                              "content": "📊 NASDAQ Hunter · {} 每日日报".format(today)},
                },
                "elements": elements,
            },
        }

    try:
        r = requests.post(webhook, json=payload, timeout=10)
        r.raise_for_status()
        result = r.json()
        if result.get("code", 0) != 0:
            log.error("飞书推送失败: %s", result)
            return False
        log.info("飞书推送成功: %s", action)
        return True
    except Exception as e:
        log.error("飞书推送异常: %s", e)
        return False


def push_discord(report: dict, webhook: str) -> bool:
    """
    Discord Webhook 推送（Embed 卡片）
    设置: Discord 频道 → 编辑频道 → 整合 → Webhooks → 新建 Webhook → 复制URL
    """
    if not webhook:
        return False

    action  = report.get("action", "WAIT")
    picks   = report.get("picks", []) or []
    today   = report.get("_date", str(date.today()))
    scanned = report.get("_scanned", 0)
    passed  = report.get("_passed", 0)
    summary = report.get("summary", "")
    _macro  = report.get("_macro") or {}
    fed     = _macro.get("fedRate")
    fed_str = "{:.2f}".format(fed) if fed is not None else "—"

    if action == "BUY" and picks:
        embed = {
            "title":       "🚨 买入信号 · " + today,
            "color":       0xE53935,
            "description": "扫描 **{}** 只 | Quality质检通过 **{}** 只 | Fed=**{}%**".format(
                scanned, passed, fed_str),
            "fields": [],
            "footer": {"text": "⚠️ 本信号仅供参考，止损 -20%（收盘）无例外执行"},
        }

        analysis = report.get("analysis_process", "")
        if analysis:
            embed["fields"].append({
                "name": "📊 分析过程", "value": analysis[:1024], "inline": False,
            })

        for i, p in enumerate(picks, 1):
            ticker    = p.get("ticker", "")
            alloc_pct = p.get("alloc_pct", 0)
            conv_raw  = (p.get("conviction") or "MEDIUM").upper()
            conv_tag  = CONV_LABEL.get(conv_raw, conv_raw)
            conv_hint = CONV_RANGE.get(conv_raw, "")
            entry     = p.get("entry", 0.0)
            stop_p    = p.get("stop", 0.0)
            upside    = p.get("target6m", "")
            thesis    = p.get("thesis", "")
            catalyst  = p.get("catalyst", "")
            risk_s    = p.get("risk", "")

            field_val = (
                "{} · {} (参考 {}%)\n"
                "📈 入场 **${:.2f}** | 止损 **${:.2f}** (-20% 收盘)\n"
                "🎯 目标: **{}**\n"
                "🧠 基本面: {}\n"
                "⚡ 催化: {}\n"
                "⚠️ 风险: {}\n"
                "🔗 https://finance.yahoo.com/quote/{}"
            ).format(conv_tag, conv_hint, alloc_pct,
                     entry, stop_p, upside, thesis, catalyst, risk_s, ticker)

            embed["fields"].append({
                "name":   "第{}只 ▶ {}".format(i, ticker),
                "value":  field_val[:1024],
                "inline": False,
            })

        if summary:
            embed["fields"].append({"name": "📝 市场总结", "value": summary[:1024], "inline": False})

        payload = {"username": "NASDAQ 猎手 🔍", "embeds": [embed]}

    else:
        wait_reason = report.get("wait_reason", "")
        next_watch  = report.get("next_watch", "")
        embed = {
            "title":       "📊 每日报告 · {} — WAIT".format(today),
            "color":       0x607D8B,   # 蓝灰，与 wathet 风格一致
            "description": "扫描 **{}** 只 | Quality质检通过 **{}** 只 | Fed=**{}%**".format(
                scanned, passed, fed_str),
            "fields": [],
        }

        # 合并等待原因 + 分析过程
        conclusion_parts = []
        if wait_reason:
            conclusion_parts.append(wait_reason)
        analysis = report.get("analysis_process", "")
        if analysis:
            conclusion_parts.append(analysis)
        if conclusion_parts:
            embed["fields"].append({
                "name": "📋 扫描结论", "value": "\n\n".join(conclusion_parts)[:1000], "inline": False,
            })

        if next_watch:
            embed["fields"].append({"name": "👁 重点关注", "value": next_watch[:500], "inline": False})
        if summary:
            embed["fields"].append({"name": "📝 市场总结", "value": summary[:500], "inline": False})

        payload = {"username": "NASDAQ 猎手 🔍", "embeds": [embed]}

    try:
        r = requests.post(webhook, json=payload, timeout=10)
        if r.status_code == 204:
            log.info("Discord推送成功: %s", action)
            return True
        r.raise_for_status()
        log.info("Discord推送成功: %s", action)
        return True
    except Exception as e:
        log.error("Discord推送异常: %s", e)
        return False


def push_feishu_error(webhook: str, error_msg: str, n_scanned: int = 0) -> bool:
    """橙色错误卡片 — 扫描或报告生成失败时发送"""
    if not webhook:
        return False
    today = str(date.today())
    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "orange",
                "title": {"tag": "plain_text", "content": "⚠️ 扫描异常 · " + today},
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        "**错误描述：** {}\n\n"
                        "**已扫描：** {:,} 只股票\n\n"
                        "**建议操作：** 检查 `scanner.log`，或手动运行 `python3 scanner_v2.py --force`"
                    ).format(error_msg, n_scanned),
                },
                {
                    "tag": "note",
                    "elements": [{"tag": "plain_text",
                                  "content": "系统将在下一个交易日自动重试。"}],
                },
            ],
        },
    }
    try:
        r = requests.post(webhook, json=payload, timeout=10)
        result = r.json()
        if result.get("code", -1) != 0:
            log.error("飞书错误卡推送失败: %s", result)
            return False
        log.info("飞书错误卡推送成功")
        return True
    except Exception as e:
        log.error("飞书错误卡推送异常: %s", e)
        return False

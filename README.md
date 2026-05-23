# NASDAQ Hunter 🎯

**Automated NASDAQ growth-stock scanner powered by a custom DNA scoring model and Claude AI.**

Every US market close, it scans ~750 NASDAQ stocks, scores them across 7 fundamental dimensions, and delivers a daily BUY / WAIT report to Feishu (Lark) at Beijing noon.

> 5-year backtest (2021–2026): **+23.9% annualized / Alpha +7.7% vs QQQ / 71.4% precision**

---

**全自动 NASDAQ 成长股猎手，基于自研 DNA 评分模型 + Claude AI 分析。**

每个美股收盘后自动扫描约 750 只 NASDAQ 股票，7 维基本面评分，每个北京工作日中午 12:00 推送 BUY / WAIT 报告到飞书。

> 5 年回测（2021–2026）：**年化 +23.9% / Alpha +7.7% vs QQQ / 精确率 71.4%**

---

## How It Works / 工作原理

```
US Close (UTC 21:30, Mon–Fri)          Beijing Noon (UTC 04:00, Tue–Sat)
         ↓                                          ↓
   daily_scan.yml                          daily_push.yml
   Download DB from R2                     Download DB from R2
   Refresh prices & fundamentals           Push pending report → Feishu
   Phase 1: DB filter  (~1,600 stocks)     Upload DB → R2
   Phase 2a: DB prefilter (skip ~850)
   Phase 2b: Finnhub deep score (750)
   Claude AI → BUY / WAIT report
   Upload DB → R2
```

### Scoring Model / 评分模型

| Layer | Gate | Description |
|-------|------|-------------|
| Quality | `quality_gate` | Rev YoY ≥ 50%, GM ≥ 40%, price ≥ $5, daily volume ≥ $1M |
| DNA | 7-dimension | Margin inflection · Revenue acceleration · Beat streak · Spin-off · Narrative · Supply cycle |
| Trend | `trend_score` | DNA slope + streak + delta over last 10 trading days |
| Composite | `composite` | DNA × 50% + Trend × 50%, threshold 70–88 (macro-adjusted) |

Epic Turnaround exemption (SNDK-type): single-quarter GM jump ≥ 20pp or revenue acceleration ≥ 80pp bypasses `MIN_HIST` and `REQUIRE_ACCEL` gates.

---

## Architecture / 架构

```
GitHub Actions (Ubuntu)
├── daily_scan.yml   — market close scan + DB upload to R2
├── daily_push.yml   — Beijing noon Feishu push from R2
└── keepalive.yml    — monthly commit to prevent workflow deactivation

Cloudflare R2         — SQLite DB persistence between runs
Finnhub API           — live price + fundamental data (Phase 2)
Anthropic Claude      — natural-language BUY/WAIT report generation
Feishu Webhooks       — dual-group push with retry
```

---

## Setup / 配置

### 1. Clone & install

```bash
git clone https://github.com/your-username/nasdaq-hunter.git
cd nasdaq-hunter
pip install -r requirements.txt boto3
```

### 2. Environment variables

Create `.env` (never commit this file):

```env
ANTHROPIC_KEY=sk-ant-...
FINNHUB_KEY=your_finnhub_key
FEISHU_WEBHOOK=https://open.larkoffice.com/open-apis/bot/v2/hook/...
FEISHU_WEBHOOK_2=https://open.larkoffice.com/open-apis/bot/v2/hook/...
FEISHU_APP_ID=cli_...
FEISHU_APP_SECRET=...
R2_ACCOUNT_ID=...
R2_ACCESS_KEY=...
R2_SECRET_KEY=...
```

### 3. GitHub Secrets

Add the same keys as repository secrets for Actions to use.

### 4. Initialize DB

```bash
python3 nasdaq_downloader.py   # first-time price + fundamentals download (~30 min)
```

---

## Usage / 用法

```bash
# Run scan once (generates today's report)
python3 scanner_v2.py

# Force re-scan (ignore today-already-scanned guard)
python3 scanner_v2.py --force

# Push latest pending report to Feishu now
python3 scanner_v2.py --push-pending

# Test Feishu push with a mock report
python3 scanner_v2.py --push-test

# Run 5-year walk-forward backtest
python3 historical_backtest.py
```

---

## Key Files / 核心文件

| File | Purpose |
|------|---------|
| `scanner_v2.py` | Main scanner: Phase 1/2 filtering, DNA scoring, Claude report, Feishu push |
| `push_channels.py` | Feishu Card 2.0 renderer: watchlist charts, score trends, image upload |
| `nasdaq_downloader.py` | Daily price + fundamentals refresh into SQLite |
| `historical_backtest.py` | 5-year walk-forward backtest engine |
| `db_sync.py` | Cloudflare R2 upload / download |
| `.github/workflows/` | GitHub Actions: daily scan, daily push, keepalive |

---

## Backtest Results / 回测结果

Walk-forward simulation, Apr 2021 – May 2026 (1,852 days). Initial capital $100,000. Point-in-time fundamentals only — no look-ahead bias.

走步回测，2021年4月 – 2026年5月（1852天）。初始资金 $10 万，全程使用当时已公开的基本面数据，无未来数据泄露。

### Portfolio vs QQQ / 策略 vs QQQ 对比

|  | **NASDAQ Hunter** | **QQQ (benchmark)** |
|--|:-----------------:|:-------------------:|
| Total return / 总收益 | **+196.7%** | +114.6% |
| Annualized / 年化收益 | **+23.9%** | +16.2% |
| Alpha (annualized) | **+7.7%** | — |
| Starting value | $100,000 | $328.86/share |
| Ending value | **$296,655** | $705.88/share |

### Signal Quality / 信号质量

| Metric / 指标 | Value / 数值 |
|---|---|
| Total signals / 总信号数 | 37 |
| Deployed positions / 实际建仓 | 31 |
| Precision (closed) / 精确率 | **71.4%** |
| True error rate / 真实误判率 | 21.4% |
| Stop-loss triggers / 触发止损 | 4 (1 market crash) |
| Avg 12-month return / 平均12月收益 | +25.3% |
| Avg max return / 平均最大浮盈 | +77.3% |
| Avg lead time to +50% / 平均提前量 | 146 days |

### Notable Signals / 代表性信号

| Date | Ticker | 12-month Return | Peak Return |
|------|--------|:--------------:|:-----------:|
| 2022-11-15 | DUOL | +197.1% | +226.8% |
| 2026-02-17 | SNDK | — | +164.5% |
| 2026-03-19 | LOVE | — | +64.8% |  
| 2024-12-04 | CRDO | +162.2% | +174.1% |
| 2024-05-14 | DCTH | +118.8% | +156.0% |
| 2025-03-31 | NUTX | +93.3% | +305.0% |
| 2024-12-01 | CRWD | +91.0% | +142.0% |
| 2022-12-09 | MDB | +99.1% | +127.4% |
| 2022-11-08 | ACMR | +87.2% | +137.8% |

### Signals per Year / 逐年信号数

| Year | Signals |
|------|---------|
| 2022 | 15 |
| 2023 | 1 |
| 2024 | 3 |
| 2025 | 3 |
| 2026 (YTD) | 15 |

Signal frequency is intentionally low — the model only fires on high-conviction setups. In bear years (2022), signals cluster near market bottoms when fundamentals diverge from price.

信号频率有意压低，只在高确信度机会才发信号。熊市年份（2022）信号集中于市场底部附近，即基本面与价格明显背离时。

> **Backtest methodology:** Walk-forward with point-in-time fundamentals, 180-day signal deduplication, 20% trailing stop-loss, macro-adjusted thresholds (Fed rate → composite score threshold 70–88).
>
> **回测方法说明：** 走步验证，PIT 基本面数据，180 天去重窗口，20% 追踪止损，宏观联动阈值（联储利率 → 综合分阈值 70–88）。

---

## Disclaimer / 免责声明

For research and educational purposes only. Not financial advice. Past backtest performance does not guarantee future results.

仅供研究学习使用，不构成任何投资建议。历史回测表现不代表未来收益。

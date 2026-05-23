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

| Metric | Value |
|--------|-------|
| Period | Apr 2021 – May 2026 |
| Annualized return | +23.9% |
| QQQ annualized | +16.2% |
| **Alpha** | **+7.7%** |
| Precision | 71.4% |
| Avg lead time to +50% | 146 days |
| Stop-loss | 20% trailing |

Backtest uses point-in-time fundamentals to avoid look-ahead bias. Signals are deduplicated within 180-day windows.

---

## Disclaimer / 免责声明

For research and educational purposes only. Not financial advice. Past backtest performance does not guarantee future results.

仅供研究学习使用，不构成任何投资建议。历史回测表现不代表未来收益。

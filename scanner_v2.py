#!/usr/bin/env python3
"""
NASDAQ 爆发股猎手 v2
- SNDK DNA 模型 + 四层门控 + 趋势评分
- 两阶段扫描（Phase1 快速初筛 → Phase2 并发深度评分）
- SQLite 持久化历史
- Pushover 手机推送（BUY高优先级 / WAIT静默）
- 每日去重保护（同一天不重复扫描）

运行方式:
  python scanner_v2.py            # 立即扫描一次
  python scanner_v2.py --force    # 强制扫描（即使今天已扫过）
  python scanner_v2.py --push-test # 只测试推送，不扫描
"""

import os, sys, json, time, re, sqlite3, logging, argparse
import concurrent.futures
from datetime import datetime, date, timezone, timedelta
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests
try:
    from anthropic import Anthropic
except ImportError:
    print("请先安装: pip install anthropic requests")
    sys.exit(1)

# 飞书 & Discord 推送模块
try:
    from push_channels import push_feishu, push_discord, push_feishu_error
except ImportError:
    def push_feishu(r, w): return False
    def push_discord(r, w): return False
    def push_feishu_error(w, msg, n=0): return False

# ══════════════════════════════════════════════════════════════
#  配置（从 .env 读取，无硬编码）
# ══════════════════════════════════════════════════════════════
CFG = {
    "FINNHUB_KEY":    os.getenv("FINNHUB_KEY",    ""),
    "ANTHROPIC_KEY":  os.getenv("ANTHROPIC_KEY",  ""),
    "PUSHOVER_TOKEN": os.getenv("PUSHOVER_TOKEN", ""),
    "PUSHOVER_USER":  os.getenv("PUSHOVER_USER",  ""),
    "TG_BOT_TOKEN":   os.getenv("TG_BOT_TOKEN",   ""),
    "TG_CHAT_ID":     os.getenv("TG_CHAT_ID",     ""),
    "SENDGRID_KEY":   os.getenv("SENDGRID_KEY",   ""),
    "REPORT_EMAIL":   os.getenv("REPORT_EMAIL",   ""),
    "FEISHU_WEBHOOK":   os.getenv("FEISHU_WEBHOOK",   ""),
    "FEISHU_WEBHOOK_2": os.getenv("FEISHU_WEBHOOK_2", ""),
    "DISCORD_WEBHOOK":  os.getenv("DISCORD_WEBHOOK",  ""),
}

STOP_LOSS     = 0.20     # 高增速成长股波动大，止损用20%（避免被市场噪音振出）
MIN_DNA       = 72
MIN_TREND     = 45       # V6.0 Production：回测确认最优值
MIN_HIST      = 3
BASE_THRESH   = 70       # V6.0 Production：宏观中性时的综合分阈值
REQUIRE_ACCEL = True     # 要求当季收入增速 > 上季×1.15（防止在增速顶峰买入）
MIN_REV_YOY   = 50.0     # Quality：最低收入增速（%）
MIN_GROSS_MARGIN = 40.0  # Quality：最低毛利率（正常门槛；超高速增长豁免见下方）
HYPER_GROWTH_YOY    = 80.0   # 超高速增长豁免：收入 YoY > 80% 时毛利下限降至 20%
HYPER_GROWTH_MIN_GM = 20.0   # 超高速增长豁免最低毛利率
MAX_REV_YOY   = 800.0    # Quality：增速上限，过滤低基数周转型（非有机增长）
MIN_PRICE_SIG = 5.0      # Quality：最低股价（过滤便士股）
MIN_AVG_DV_M  = 1.0      # Quality：10日平均日成交额下限（$1M，过滤微市值流动性陷阱）
MIN_AVG_VOL_K = 10.0     # Quality：10日平均日成交量下限（1万股/日，过滤高价薄盘股）
# 史诗级跃升豁免（SNDK型困境反转冷启动）
EPIC_GM_JUMP  = 20.0     # 毛利率单季跳升阈值（百分点，+20pp才算量级改变）
EPIC_YOY_JUMP = 80.0     # 收入增速环比跳升阈值（百分点，+80pp才算爆炸性加速）
EPIC_MIN_DNA  = 75       # 触发豁免所需最低DNA分（75允许无EPS数据的新上市公司触发）
EPIC_MAX_GM   = 200.0    # 毛利率上限（过滤数据错误）
MAX_WORKERS  = 4         # Phase2 并发线程数（Finnhub 免费 60req/min，4线程安全）
P1_DELAY     = 0.38      # Phase1 请求间隔（秒）
P2_DELAY     = 1.6       # Phase2 每股延迟（并发下每线程）

DATA_DIR = Path(__file__).parent / "data"
DB_PATH  = DATA_DIR / "nasdaq_history.db"
LOG_PATH = DATA_DIR / "scanner.log"

# ══════════════════════════════════════════════════════════════
#  扫描宇宙（500只）
#  覆盖策略：过去5年所有爆发股所在赛道 + 拆分/新股 + 中小盘成长
#  Phase1快速初筛后实际深度评分约200-250只
# ══════════════════════════════════════════════════════════════
_UNIVERSE_RAW = [
    # ── 半导体 & 存储（SNDK母赛道，最高优先级）─────────────────
    "NVDA","AMD","MRVL","CRDO","ALAB","CAMT","SMCI","ARM","AVGO","QCOM",
    "MU","WDC","KLAC","LRCX","AMAT","ONTO","FORM","WOLF","MPWR","SLAB",
    "SWKS","QRVO","NXPI","TXN","ADI","MCHP","MTSI","DIOD","AMBA","SIGI",
    "COHU","ICHR","ACMR","NVMI","POWI","SITM","ALGM","OLED","LAZR","MRAM",
    "IMOS","PDFS","AXTI","RMBS","SLAB","SKWD","VERX","RELY","PRCT","TFIN",

    # ── AI 基础设施 & 数据中心 ─────────────────────────────────
    "NBIS","IONQ","SOUN","PLTR","APP","TTD","BBAI","ARQT","MSFT","GOOGL",
    "META","AMZN","ORCL","IBM","HPE","DELL","NTAP","PSTG","PFLT","BAND",
    "ADBE","CRM","WDAY","NOW","VEEV","HUBS","DDOG","SNOW","NET","MDB",
    "CFLT","ESTC","GTLB","FROG","SUMO","BRZE","ASAN","MNDY","SMAR","PLAN",

    # ── 云/SaaS 成长 ─────────────────────────────────────────
    "ZS","PANW","CRWD","S","FTNT","CYBR","QLYS","TENB","RPD","VRNS",
    "SENT","RBRK","SAIC","LDOS","CACI","BAH","MANT","IC","KTOS","HII",
    "ZM","DOCU","BOX","AVLR","PCTY","PAYC","PAYX","ADP","INTU","ADSK",
    "ANSS","CDNS","SNPS","PTC","AZPN","DSGX","NCNO","UPLD","TASK","INTA",

    # ── 消费科技 & 平台 ──────────────────────────────────────
    "CELH","CAVA","DUOL","HIMS","WING","BROS","ELF","RDDT","HOOD","COIN",
    "DKNG","PENN","MGM","WYNN","LVS","VICI","GLPI","CHDN","FLUT","RSI",
    "UBER","LYFT","ABNB","DASH","TPST","SEAT","EVGO","BLNK","CHPT","PTRA",
    "RIVN","LCID","GOEV","FFIE","SOLO","AYRO","IDEX","MULN","WKHS","NKLA",

    # ── 医疗科技 & 生物科技 ──────────────────────────────────
    "TMDX","GKOS","NARI","ACLS","AXNX","INSP","SWAV","SHOC","NVST","OMCL",
    "ALGN","ISRG","MASI","MMSI","ITGR","NVRO","NVCR","TELA","ATRC","SWAV",
    "RXST","IRTC","BLFS","LMAT","PODD","DXCM","TDOC","AMWL","OTRK","DOCS",
    "ARES","ACAD","RARE","SRPT","PTCT","KRYS","PCVX","FATE","BCAB","RCKT",
    "MRNA","BNTX","NVAX","VRTX","REGN","BIIB","ALNY","BMRN","EXEL","ONCE",

    # ── 工业科技 & 电气化 ────────────────────────────────────
    "POWL","GTLS","ARIS","ACHR","GNRC","ENS","HUBB","REXN","ATKR","FIX",
    "IESC","MYRG","DY","PIKE","PRIM","MTZ","PWR","WLDN","STRL","KBR",
    "ENPH","FSLR","ARRY","SHLS","RUN","NOVA","STEM","BE","HYLN","PLUG",
    "CSIQ","DQ","JKS","MAXN","SPWR","SEDG","GNRC","REGI","REX","AMRC",

    # ── 金融科技 & 保险科技 ──────────────────────────────────
    "SOFI","AFRM","UPST","NU","CWAN","FLYW","OPEN","KLAR","RELY","DAVE",
    "LPRO","TREE","RATE","UWMC","PFSI","RMAX","EXPI","COOP","RKT","LDI",
    "LMND","ROOT","HIPO","KINS","ACIC","METC","AMSF","KINGSF","RYAN","AON",
    "HOOD","FUTU","TIGR","IBKR","MKTX","LPLA","RJF","SF","SNEX","PIPR",

    # ── 中小盘成长（最容易出现SNDK类爆发）──────────────────────
    "AXON","GLBE","ALKT","JAMF","CERT","ALRM","QTWO","WEAVE","EVER","KPLT",
    "RSKD","AMSB","AORT","HCMA","HWKN","MGY","CRK","CIVI","VRN","REPX",
    "TPVG","FDUS","CSWC","BXSL","ARCC","MAIN","KCAL","GAIN","HTGC","PFLT",
    "INMD","XMTR","OPAD","OPA","WEAV","RAMP","AMBA","BAND","LQDT","GFAI",
    "BLND","REAX","SFIX","POSH","REAL","GENI","MAPS","ACVA","AIOT","NRDS",
    "COOK","BFLY","PRAX","SAGE","ACRS","VNET","WB","TIGR","JMM","CLFD",

    # ── 拆分/重组/新股（SNDK原型，最值得关注）────────────────────
    "SOLV","KVYO","CART","GEV","RXO","KNTK","VNT","ESAB","DOOR","WFRD",
    "BETR","FNVT","LFST","RXRX","SDCL","EVTL","JOBY","LILM","ARCHER","BLADE",
    "SKYH","RKLB","PL","ASTS","LUNR","MNTS","SPIR","BKSY","ORBT","SATL",

    # ── 能源科技 & 大宗商品科技 ──────────────────────────────
    "DINO","CRK","MRO","CIVI","MGY","SM","ESTE","GRNT","NRGV","SOC",
    "VET","RIG","BORR","NE","DO","VAL","PAAS","GOLD","AEM","WPM",
    "MP","LITE","IIVI","COHR","VIAVI","AAON","OSIS","POWL","AGX","ARTW",

    # ── ADR / 国际成长股 ─────────────────────────────────────
    "TSM","ASML","SE","MELI","PDD","BEKE","LI","GRAB","DIDI","BZUN",
    "LEGN","ZLAB","BZ","YMM","MPNGY","KC","QFIN","FINV","LXFR","GBIO",
    "SHOP","DOCS","LSPD","NVEI","DND","CRWD","MNDY","GTLB","MON","GOTU",
]
TICKERS = list(dict.fromkeys(_UNIVERSE_RAW))  # 去重

# ══════════════════════════════════════════════════════════════
#  日志
# ══════════════════════════════════════════════════════════════
DATA_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
    ],
)
log = logging.getLogger("scanner")

# ══════════════════════════════════════════════════════════════
#  数据库
# ══════════════════════════════════════════════════════════════
def init_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
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
        CREATE TABLE IF NOT EXISTS reports (
            report_date TEXT PRIMARY KEY,
            action      TEXT,
            json        TEXT,
            pushed      INTEGER DEFAULT 0,
            created_at  TEXT
        );
        CREATE TABLE IF NOT EXISTS scan_log (
            scan_date   TEXT PRIMARY KEY,
            started_at  TEXT,
            finished_at TEXT,
            status      TEXT,
            n_scanned   INTEGER DEFAULT 0,
            n_passed    INTEGER DEFAULT 0
        );
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
    con.commit()
    return con


def already_scanned_today(con: sqlite3.Connection) -> bool:
    today = str(date.today())
    row = con.execute(
        "SELECT status FROM scan_log WHERE scan_date=?", (today,)
    ).fetchone()
    return row is not None and row[0] == "done"


def mark_scan_start(con, today):
    con.execute(
        "INSERT OR REPLACE INTO scan_log VALUES (?,?,NULL,'running',0,0)",
        (today, datetime.utcnow().isoformat())
    )
    con.commit()


def mark_scan_done(con, today, n_scanned, n_passed):
    con.execute(
        "UPDATE scan_log SET finished_at=?, status='done', n_scanned=?, n_passed=? "
        "WHERE scan_date=?",
        (datetime.utcnow().isoformat(), n_scanned, n_passed, today)
    )
    con.commit()


def save_score(con, ticker, today, dna, trend, comp, threshold, triggered,
               price, gm, rev_yoy, eps_sur, beats_4q, fail_reason, fed_rate=None):
    stop_price = round(price * (1 - STOP_LOSS), 2) if price else None
    con.execute(
        "INSERT OR REPLACE INTO daily_signals "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ticker, today, dna, trend, comp, threshold, int(bool(triggered)),
         price, stop_price, fed_rate, gm, rev_yoy, eps_sur, beats_4q,
         fail_reason, "scanner")
    )
    con.commit()


def load_history(con, ticker, days=60):
    rows = con.execute(
        "SELECT scan_date, dna FROM daily_signals "
        "WHERE ticker=? ORDER BY scan_date DESC LIMIT ?",
        (ticker, days)
    ).fetchall()
    return [{"date": r[0], "dna": r[1]} for r in reversed(rows)]


def save_report(con, today, action, report):
    con.execute(
        "INSERT OR REPLACE INTO reports VALUES (?,?,?,0,?)",
        (today, action, json.dumps(report, ensure_ascii=False),
         datetime.utcnow().isoformat())
    )
    con.commit()


def mark_pushed(con, today):
    con.execute("UPDATE reports SET pushed=1 WHERE report_date=?", (today,))


def push_pending_report() -> bool:
    """
    北京时间 12:00 定时推送：取最近一条 pushed=0 的报告推送到所有渠道。
    如果已全部推送过，则重推最新一条（补发容错）。
    """
    con = init_db()
    row = con.execute(
        "SELECT report_date, json FROM reports WHERE pushed=0 ORDER BY report_date DESC LIMIT 1"
    ).fetchone()
    if not row:
        # 没有未推送的报告，取最近一条重推（防止漏发）
        row = con.execute(
            "SELECT report_date, json FROM reports ORDER BY report_date DESC LIMIT 1"
        ).fetchone()
        if not row:
            log.warning("push_pending_report: 数据库中无任何报告，跳过")
            con.close()
            return False
        log.info("push_pending_report: 今日报告已推送过，重推最新 %s", row[0])
    else:
        log.info("push_pending_report: 推送待发报告 %s", row[0])

    report_date, report_json = row
    try:
        report = json.loads(report_json)
    except Exception as e:
        log.error("push_pending_report: JSON 解析失败: %s", e)
        con.close()
        return False

    ok = push_all(report)
    if ok:
        mark_pushed(con, report_date)
        con.commit()
        log.info("push_pending_report: 推送成功 %s", report_date)
    else:
        log.error("push_pending_report: 推送失败 %s", report_date)
    con.close()
    return ok
    con.commit()


# ══════════════════════════════════════════════════════════════
#  Finnhub API
# ══════════════════════════════════════════════════════════════
FH = "https://finnhub.io/api/v1"
_session = requests.Session()
_session.headers.update({"User-Agent": "nasdaq-scanner/2.0"})


def fh_get(path: str, params: Optional[dict] = None, retries: int = 3) -> dict:
    p = dict(params or {})
    p["token"] = CFG["FINNHUB_KEY"]
    for attempt in range(retries):
        try:
            r = _session.get(f"{FH}{path}", params=p, timeout=12)
            if r.status_code == 429:
                wait = 6 * (attempt + 1)
                log.warning(f"Rate limit on {path}, wait {wait}s")
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                time.sleep(3)
                continue
            r.raise_for_status()
            return r.json() or {}
        except Exception as e:
            if attempt == retries - 1:
                log.debug(f"fh_get {path} failed: {e}")
                return {}
            time.sleep(2)
    return {}


def fetch_phase1(ticker: str) -> dict:
    return fh_get("/quote", {"symbol": ticker})


def fetch_phase2(ticker: str) -> dict:
    """并发拉取 8 个端点"""
    endpoints = [
        ("/quote",                      {"symbol": ticker}),
        ("/stock/profile2",             {"symbol": ticker}),
        ("/stock/metric",               {"symbol": ticker, "metric": "all"}),
        ("/stock/insider-transactions", {"symbol": ticker}),
        ("/stock/recommendation",       {"symbol": ticker}),
        ("/stock/earnings",             {"symbol": ticker, "limit": 8}),
        ("/stock/eps-estimates",        {"symbol": ticker, "freq": "quarterly"}),
        ("/stock/revenue-estimates",    {"symbol": ticker, "freq": "quarterly"}),
    ]
    keys = ["quote","profile","_mraw","insider","reco","earnings","eps_est","rev_est"]

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(fh_get, ep, params): k
                for (ep, params), k in zip(endpoints, keys)}
        for fut, k in futs.items():
            try:
                results[k] = fut.result(timeout=15)
            except Exception:
                results[k] = {}

    results["metrics"] = (results.pop("_mraw", {}) or {}).get("metric", {}) or {}
    return results


# ══════════════════════════════════════════════════════════════
#  宏观环境
# ══════════════════════════════════════════════════════════════
def get_macro() -> dict:
    try:
        data = fh_get("/economic", {"code": "FEDFUNDS"})
        pts  = data.get("data", []) or []
        if len(pts) >= 12:
            latest   = float(pts[-1]["v"])
            prev12   = float(pts[-12]["v"])
            yoy      = round(latest - prev12, 2)
            regime   = ("restrictive" if latest > 5 else
                        "neutral"     if latest > 3 else "accommodative")
            return {"fedRate": latest, "yoyChange": yoy, "regime": regime}
    except Exception as e:
        log.warning(f"Macro fetch failed: {e}")
    return {"fedRate": 4.5, "yoyChange": 0.0, "regime": "neutral"}


def macro_threshold(macro: dict) -> dict:
    rate = macro.get("fedRate", 4.5)
    if rate > 5.0:
        return {"threshold": 88, "note": f"高利率限制期 {rate}%"}
    if rate > 4.5:
        return {"threshold": 83, "note": f"偏紧利率 {rate}%"}
    return {"threshold": BASE_THRESH, "note": f"中性环境 {rate}%"}


# ══════════════════════════════════════════════════════════════
#  四层门控
# ══════════════════════════════════════════════════════════════
def quality_gate(raw: dict) -> dict:
    m = raw.get("metrics", {}) or {}
    q = raw.get("quote",   {}) or {}
    p = raw.get("profile", {}) or {}
    reasons = []

    rev_q  = float(m.get("revenueGrowthQuarterlyYoy") or 0)
    min_gm = HYPER_GROWTH_MIN_GM if rev_q >= HYPER_GROWTH_YOY else MIN_GROSS_MARGIN
    gm = float(m.get("grossMarginTTM") or 0)
    if gm < min_gm:
        reasons.append(f"毛利率不足({gm:.1f}%，需>{min_gm:.0f}%)")

    # 内部人净卖出比（近90天，仅计公开市场交易）
    txns = (raw.get("insider") or {}).get("data", []) or []
    recent = [t for t in txns
              if _days_ago(t.get("transactionDate","2000-01-01")) <= 90
              and t.get("transactionCode") in ("P","S")]
    buys  = sum(abs(t.get("change", 0)) for t in recent if t.get("transactionCode") == "P")
    sells = sum(abs(t.get("change", 0)) for t in recent if t.get("transactionCode") == "S")
    sell_r = sells / (buys + sells) if (buys + sells) > 50_000 else 0.0
    if sell_r > 0.70:
        reasons.append(f"高管大量抛售({sell_r*100:.0f}%)")

    if rev_q < MIN_REV_YOY:
        reasons.append(f"收入增速不足(+{rev_q:.0f}%，需>{MIN_REV_YOY:.0f}%)")
    elif rev_q > MAX_REV_YOY:
        reasons.append(f"增速异常({rev_q:.0f}%，疑低基数周转型)")

    mkt = float(p.get("marketCapitalization") or 0)
    if mkt < 100:
        reasons.append(f"市值过小(${mkt:.0f}M)")

    price = float(q.get("c") or 0)
    if price < MIN_PRICE_SIG or price > 3000:
        reasons.append(f"价格无效(${price}，需≥${MIN_PRICE_SIG:.0f})")

    avg_vol_m = float(m.get("10DayAverageTradingVolume") or 0)  # millions of shares
    avg_vol_shares = avg_vol_m * 1_000_000
    avg_dv_m = avg_vol_shares * price / 1_000_000  # daily dollar volume in $M
    if avg_vol_shares < MIN_AVG_VOL_K * 1_000:
        reasons.append(f"成交量不足({avg_vol_m*1000:.0f}K股/日，需>{MIN_AVG_VOL_K:.0f}K)")
    elif avg_dv_m < MIN_AVG_DV_M:
        reasons.append(f"日成交额不足(${avg_dv_m:.2f}M，需>${MIN_AVG_DV_M:.0f}M)")

    return {"pass": not reasons, "reasons": reasons, "sell_ratio": sell_r, "gm": gm,
            "avg_dv_m": round(avg_dv_m, 2), "avg_vol_k": round(avg_vol_m * 1000, 1)}


def _days_ago(date_str: str) -> float:
    try:
        dt = datetime.fromisoformat(date_str)
        return (datetime.utcnow() - dt).days
    except Exception:
        return 9999


def forward_gate(raw: dict) -> dict:
    rev_est  = (raw.get("rev_est")  or {}).get("data", []) or []
    eps_est  = (raw.get("eps_est")  or {}).get("data", []) or []
    earnings = raw.get("earnings") or []
    m        = raw.get("metrics",  {}) or {}
    now      = datetime.utcnow()

    result = {"pass": True, "reason": "", "decel_score": 0.0,
              "next_q_rev": None, "next_q_eps": None}

    future_rev = sorted(
        [e for e in rev_est
         if e.get("period") and _parse_date(e["period"]) > now],
        key=lambda x: x["period"]
    )

    if future_rev and earnings:
        est_rev = float(future_rev[0].get("revenueAvg") or 0)
        act_rev = float((earnings[0] or {}).get("revenue") or 0)
        if est_rev > 0 and act_rev > 0:
            rev_q  = float(m.get("revenueGrowthQuarterlyYoy") or 0)
            next_g = ((est_rev / act_rev) - 1) * 100
            result["next_q_rev"] = round(next_g, 1)
            decel = rev_q - next_g
            if decel > 40:
                result.update({"pass": False, "decel_score": decel,
                    "reason": f"增速顶峰：当季+{rev_q:.0f}%→预期+{next_g:.0f}%，减速{decel:.0f}ppt"})
            elif decel > 25:
                result.update({"decel_score": decel,
                    "reason": f"增速放缓{decel:.0f}ppt，谨慎"})

    future_eps = sorted(
        [e for e in eps_est
         if e.get("period") and _parse_date(e["period"]) > now],
        key=lambda x: x["period"]
    )
    if future_eps:
        result["next_q_eps"] = future_eps[0].get("epsAvg")

    return result


def _parse_date(s: str) -> datetime:
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return datetime(1970, 1, 1)


# ══════════════════════════════════════════════════════════════
#  SNDK DNA 评分（6维）
# ══════════════════════════════════════════════════════════════
_DNA_DIMS = [
    {"id":"margin",    "name":"毛利率拐点",  "w":24, "crit":True,  "min":40},
    {"id":"rev_accel", "name":"收入加速",    "w":26, "crit":True,  "min":45},
    {"id":"beats",     "name":"连续超预期",  "w":22, "crit":True,  "min":35},
    {"id":"spinoff",   "name":"拆分/重组",   "w":10, "crit":False, "min":0},
    {"id":"narrative", "name":"叙事重定价",  "w":10, "crit":False, "min":0},
    {"id":"supply",    "name":"供需周期",    "w":8,  "crit":False, "min":0},
]


def _dim_score(dim_id: str, raw: dict) -> int:
    m = raw.get("metrics", {}) or {}
    q = raw.get("quote",   {}) or {}
    p = raw.get("profile", {}) or {}
    reco     = raw.get("reco",     []) or []
    earnings = raw.get("earnings", []) or []

    if dim_id == "margin":
        gm = float(m.get("grossMarginTTM") or 0)
        if gm <= 0: return 0
        return (100 if gm>70 else 82 if gm>55 else 62 if gm>40
                else 38 if gm>25 else 18 if gm>15 else 5)

    if dim_id == "rev_accel":
        qg  = float(m.get("revenueGrowthQuarterlyYoy") or 0)
        ttm = float(m.get("revenueGrowthTTM") or 0)
        if qg <= 0: return 0
        accel = qg > ttm * 1.15
        base  = (100 if qg>200 else 88 if qg>100 else 68 if qg>50
                 else 45 if qg>30 else 28 if qg>20 else 10)
        return min(100, base + (12 if accel else 0))

    if dim_id == "beats":
        recent = (earnings or [])[:4]
        n = sum(1 for e in recent
                if e.get("actual") is not None and e.get("estimate") is not None
                and float(e["actual"]) > float(e["estimate"]) * 1.05)
        last = recent[0] if recent else {}
        mag  = 0.0
        if (last.get("actual") and last.get("estimate")
                and float(last["estimate"]) != 0):
            mag = (float(last["actual"]) - float(last["estimate"])) / abs(float(last["estimate"]))
        base = (100 if n>=4 else 80 if n==3 else 55 if n==2 else 25 if n==1 else 0)
        return min(100, base + (20 if mag>0.5 else 12 if mag>0.25 else 5 if mag>0.1 else 0))

    if dim_id == "spinoff":
        ipo_str = p.get("ipo") or ""
        age_m   = _days_ago(ipo_str) / 30 if ipo_str else 999
        hi = float(m.get("52WeekHigh") or 1)
        lo = float(m.get("52WeekLow")  or 1)
        hl = hi / lo if lo > 0 else 1
        if age_m < 18 and hl > 2:   return 90
        if age_m < 24 and hl > 1.5: return 65
        if age_m < 36:               return 35
        return 8

    if dim_id == "narrative":
        r0 = reco[0] if reco else None
        r1 = reco[1] if len(reco) > 1 else None
        if not r0: return 15
        t0 = sum([r0.get(k,0) for k in ("strongBuy","buy","hold","sell","strongSell")]) or 1
        b0 = (r0.get("strongBuy",0) + r0.get("buy",0)) / t0
        t1 = sum([r1.get(k,0) for k in ("strongBuy","buy","hold","sell","strongSell")]) or 1 if r1 else t0
        b1 = (r1.get("strongBuy",0) + r1.get("buy",0)) / t1 if r1 else b0
        vel = b0 - b1
        base = (80 if b0>0.8 else 60 if b0>0.65 else 40 if b0>0.5 else 15)
        return min(100, base + int(vel * 150))

    if dim_id == "supply":
        avg_vol = float(m.get("10DayAverageTradingVolume") or 0) * 1_000_000
        vm      = float(q.get("v") or 0) / avg_vol if avg_vol > 1000 else 1.0
        hi52    = float(m.get("52WeekHigh") or 0)
        price   = float(q.get("c") or 0)
        ph      = price / hi52 if hi52 > 0 else 0
        vs = (80 if vm>3 else 60 if vm>2 else 40 if vm>1.5 else 15)
        hs = (90 if ph>0.95 else 65 if ph>0.85 else 40 if ph>0.7 else 20)
        return round(vs * 0.4 + hs * 0.6)

    return 0


def score_dna(raw: dict) -> dict:
    dims, wsum, wtot, failed = {}, 0, 0, []
    for d in _DNA_DIMS:
        s = max(0, min(100, round(_dim_score(d["id"], raw))))
        dims[d["id"]] = s
        wsum += s * d["w"]; wtot += d["w"]
        if d["crit"] and s < d["min"]:
            failed.append(f"{d['name']}({s}/{d['min']})")

    dna = round(wsum / wtot)
    if failed:
        dna = round(dna * 0.5)

    m = raw.get("metrics", {}) or {}
    q = raw.get("quote",   {}) or {}
    p = raw.get("profile", {}) or {}

    rev_q = float(m.get("revenueGrowthQuarterlyYoy") or 0)
    beats = sum(1 for e in (raw.get("earnings") or [])[:4]
                if e.get("actual") is not None and e.get("estimate") is not None
                and float(e["actual"]) > float(e["estimate"]) * 1.05)

    return {
        "dna": dna, "dims": dims, "failed": failed,
        "signals": [d["name"] for d in _DNA_DIMS if dims[d["id"]] >= 55],
        "meta": {
            "name":    p.get("name", ""),
            "sector":  p.get("finnhubIndustry", ""),
            "mktCapB": round(float(p.get("marketCapitalization") or 0) / 1000, 1),
            "price":   float(q.get("c") or 0),
            "priceChg":float(q.get("dp") or 0),
            "rev":     rev_q,
            "gm":      float(m.get("grossMarginTTM") or 0),
            "pe":      float(m.get("peTTM") or 0),
            "beta":    float(m.get("beta") or 1),
            "ret52":   float(m.get("52WeekPriceReturnDaily") or 0),
            "beats":   beats,
        },
    }


# ══════════════════════════════════════════════════════════════
#  趋势评分
# ══════════════════════════════════════════════════════════════
def trend_score(history: list) -> dict:
    if not history or len(history) < 2:
        return {"score": 0, "slope": 0.0, "streak": 0,
                "label": "积累中", "delta": 0.0, "days": len(history)}
    recent = history[-10:]
    scores = [h["dna"] for h in recent]
    n  = len(scores); xm = (n-1)/2; ym = sum(scores)/n
    num = sum((x-xm)*(y-ym) for x,y in enumerate(scores))
    den = sum((x-xm)**2 for x in range(n))
    slope = num/den if den else 0.0

    streak = 0
    for h in reversed(recent):
        if h["dna"] >= 55: streak += 1
        else: break

    delta = scores[-1] - scores[0]
    ts = min(100, max(0,
        (35 if slope>4 else 22 if slope>2 else 12 if slope>.5 else 0) +
        (30 if streak>=7 else 20 if streak>=5 else 10 if streak>=3 else 0) +
        (25 if delta>25 else 15 if delta>12 else 8 if delta>5 else 0)
    ))
    label = ("🔥爆发加速" if slope>3 and delta>15 else
             "📈持续上升" if slope>1.5 else
             "↗缓慢走强" if slope>0 else
             "→横盘整理" if slope>-1 else "↘走弱")
    return {"score": ts, "slope": round(slope,2), "streak": streak,
            "delta": round(delta,1), "label": label, "days": len(history)}


def composite(dna: int, trend: int, hist_len: int, fwd: dict,
              is_epic: bool = False) -> int:
    if is_epic:
        # 史诗跃升：基本面突变即最强趋势，直接取DNA分跳过历史降权和减速惩罚
        return dna
    if hist_len < 2:   base = round(dna * 0.45)
    elif hist_len < 5: base = round(dna * 0.6 + trend * 0.4)
    else:              base = round(dna * 0.5 + trend * 0.5)
    decel = (fwd or {}).get("decel_score", 0) or 0
    if decel > 25:
        base = round(base * (1 - decel / 200))
    return base


def check_epic_turnaround(con, ticker: str, today: str, dna: int) -> bool:
    """从 DB 取最近两个不同财季的基本面，判断是否史诗级跃升（SNDK型困境反转）。
    通过跳过同季度重复记录（period 相差 <45 天），确保比较的是相邻两个财季。
    """
    if dna < EPIC_MIN_DNA:
        return False
    rows = con.execute("""
        SELECT gross_margin, revenue_yoy, period FROM fundamentals
        WHERE ticker=? AND filing_date <= ?
        ORDER BY filing_date DESC LIMIT 6
    """, (ticker, today)).fetchall()
    if len(rows) < 2:
        return False
    # 去重：跳过与上一条 period 相差 <45 天的同季度重复记录
    deduped = [rows[0]]
    for r in rows[1:]:
        try:
            from datetime import datetime as _dt
            d1 = _dt.strptime(deduped[-1][2][:10], "%Y-%m-%d")
            d2 = _dt.strptime(r[2][:10], "%Y-%m-%d")
            if abs((d1 - d2).days) >= 45:
                deduped.append(r)
        except Exception:
            deduped.append(r)
        if len(deduped) == 2:
            break
    if len(deduped) < 2:
        return False
    cur_gm, cur_yoy, _   = deduped[0]
    prev_gm, prev_yoy, _ = deduped[1]
    if not all(v is not None for v in [cur_gm, cur_yoy, prev_gm, prev_yoy]):
        return False
    if cur_gm > EPIC_MAX_GM:   # 数据错误保护
        return False
    gm_jump  = cur_gm - prev_gm
    yoy_jump = cur_yoy - prev_yoy
    sndk_path = prev_gm < 40 and cur_gm >= 40 and gm_jump >= EPIC_GM_JUMP
    alab_path = yoy_jump >= EPIC_YOY_JUMP and cur_yoy >= 200 and cur_gm >= 50
    return sndk_path or alab_path


# ══════════════════════════════════════════════════════════════
#  Claude 报告生成
# ══════════════════════════════════════════════════════════════
def _enrich_next_watch(report: dict, all_results: list) -> None:
    """
    将扫描结果的综合分注入 next_watch 字符串，
    格式: "ALAB:综合分=48，关注高管..." → _watchlist_bullets() 可正确提取数字。
    原地修改 report dict。
    """
    nw = report.get("next_watch", "")
    if not nw:
        return
    comp_map = {r["ticker"]: r.get("comp", 0) for r in all_results}
    enriched = []
    # 按 "TICKER:" 模式切分 —— 不依赖标点，兼容 Claude 任意分隔方式
    # 先找所有 TICKER: 的位置，再按位置切出每段
    token_pat = re.compile(r'(?:^|[;,，；\s]+)([A-Z]{2,6}):', re.M)
    positions = [(m.start(1), m.end(0), m.group(1)) for m in token_pat.finditer(nw)]
    if not positions:
        return  # 无法解析，保留原文
    for idx, (start, end, ticker) in enumerate(positions):
        next_start = positions[idx + 1][0] - 1 if idx + 1 < len(positions) else len(nw)
        reason = nw[end:next_start].strip().rstrip(";,，；")
        comp = comp_map.get(ticker)
        if comp and not re.search(r'(?:综合|综合分|DNA|分数)[=＝\s：:]*\d+', reason):
            reason = f"综合分={comp}，" + reason
        enriched.append(f"{ticker}:{reason}")
    report["next_watch"] = ", ".join(enriched)


def generate_report(passed, all_stocks, macro, mthresh, today):
    if not CFG["ANTHROPIC_KEY"]:
        raise ValueError("ANTHROPIC_KEY 未设置")

    client = Anthropic(api_key=CFG["ANTHROPIC_KEY"])

    lines = [
        "ROLE: Concentrated quant PM. Max 2 positions. Precision > recall.",
        f"DATE: {today}",
        f"MACRO: Fed={macro['fedRate']}% regime={macro['regime']} YoY={macro['yoyChange']:+.2f}%",
        f"THRESHOLD: {mthresh['threshold']} ({mthresh['note']})",
        f"MODEL: SNDK DNA v7 — 4-layer gate. History required: {MIN_HIST}d",
        "",
        f"PASSED ALL GATES ({len(passed)} stocks):",
    ]

    for s in (passed[:3] if passed else []):
        fwd  = s.get("fwd_gate", {}) or {}
        td   = s.get("trend",    {}) or {}
        meta = s.get("meta",     {}) or {}
        lines += [
            "---",
            f"{s['ticker']} | {meta.get('name','')} | {meta.get('sector','')}",
            f"DNA:{s['dna']} Trend:{td.get('score',0)} Comp:{s['comp']} Days:{td.get('days',0)}",
            f"RevQoQ:+{meta.get('rev',0):.0f}% GM:{meta.get('gm',0):.1f}% Beats:{meta.get('beats',0)}/4",
            f"FwdNextQ:{fwd.get('next_q_rev')}% FwdEPS:{fwd.get('next_q_eps')}",
            f"Price:${meta.get('price',0):.2f} Beta:{meta.get('beta',1):.2f} MCap:${meta.get('mktCapB',0):.1f}B",
            f"Signals:{', '.join(s.get('signals',[]))}",
            f"Trend:{td.get('label','')} slope={td.get('slope',0)}/d streak={td.get('streak',0)}d",
            f"FailedDims:{'; '.join(s.get('failed',[])) or 'none'}",
        ]
        if fwd.get("reason"):
            lines.append(f"FwdWarning:{fwd['reason']}")

    # 最接近触发的3只
    non_passed = [s for s in all_stocks
                  if not any(p["ticker"]==s["ticker"] for p in passed)][:3]
    lines.append("\nCLOSEST_MISS:")
    for s in non_passed:
        lines.append(f"  {s['ticker']} comp={s.get('comp',0)} fail=[{s.get('gate_fail','')}]")

    lines += [
        "",
        "STRICT: action=WAIT unless STRONG conviction AND all gates pass.",
        f"If BUY: max 2 picks, stop=entry*{1-STOP_LOSS:.2f}, no exceptions.",
        "CONVICTION: STRONG=60-80% of capital (must-not-miss, near-certain); MEDIUM=20-40% (good odds); WEAK=5-15% (speculative). alloc_pct=recommended center of range.",
        f"analysis_process REQUIRED: in Chinese, ~3-5 sentences covering Quality gate results (rev≥{MIN_REV_YOY:.0f}% gm≥{MIN_GROSS_MARGIN:.0f}%), Outlook/Macro/Signal gate results, top candidate scores, and why BUY or WAIT.",
        f"NEVER recommend: forward decel>{20 if REQUIRE_ACCEL else 40}ppt stocks, price<${MIN_PRICE_SIG:.0f}, rev>{MAX_REV_YOY:.0f}%.",
        "",
        'JSON only (no markdown):',
        '{"action":"BUY or WAIT","picks":[{"ticker":"","conviction":"STRONG or MEDIUM or WEAK","alloc_pct":60,"entry":0.0,"stop":0.0,"target6m":"","thesis":"","catalyst":"","risk":""}],"analysis_process":"","summary":"","wait_reason":"","next_watch":"ticker:reason"}',
    ]

    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1200,
        system="Quant PM. Reply ONLY valid JSON. Be conservative — silence beats a bad trade.",
        messages=[{"role": "user", "content": "\n".join(lines)}],
    )
    text = "".join(getattr(b, "text", "") for b in msg.content)
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError(f"No JSON in Claude response: {text[:200]}")
    rep = json.loads(m.group(0))
    rep.update({
        "_date": today, "_passed": len(passed),
        "_scanned": len(all_stocks), "_macro": macro,
    })
    return rep


# ══════════════════════════════════════════════════════════════
#  推送（Pushover + Telegram）
# ══════════════════════════════════════════════════════════════
def _fmt_report_text(report: dict) -> tuple:
    """返回 (title, message, priority, sound)"""
    action  = report.get("action", "WAIT")
    picks   = report.get("picks", []) or []
    today   = report.get("_date", str(date.today()))
    scanned = report.get("_scanned", 0)
    passed  = report.get("_passed", 0)
    summary = report.get("summary", "")
    macro   = report.get("_macro", {}) or {}

    if action == "BUY" and picks:
        title = "🚨 买入信号 · " + today
        body  = ["📊 扫描{}只 | 通过{}只门控 | Fed={}%\n".format(
                 scanned, passed, macro.get("fedRate", "-"))]
        for i, p in enumerate(picks, 1):
            alloc_pct = p.get("alloc_pct", 0)
            conv      = (p.get("conviction") or "MEDIUM").upper()
            entry     = p.get("entry", 0.0)
            stop_p    = p.get("stop", 0.0)
            upside    = p.get("target6m", "")
            thesis    = p.get("thesis", "")
            catalyst  = p.get("catalyst", "")
            risk_s    = p.get("risk", "")
            body += [
                "━"*32,
                "第{}只 ▶ {}  [{}] 建议仓位{}%".format(i, p.get("ticker",""), conv, alloc_pct),
                "📈 入场价 ${:.2f}  止损价 ${:.2f}".format(entry, stop_p),
                "🎯 6月目标 {}".format(upside),
                "📋 " + thesis,
                "⚡ 催化: " + catalyst,
                "⚠️ 风险: " + risk_s,
                "",
            ]
        if summary:
            body.append("📝 " + summary)
        body.append("⚠️ 不构成投资建议 | 止损-15%无例外执行")
        return title, "\n".join(body), 1, "cashregister"
    else:
        title  = "📊 每日报告 · {} — WAIT".format(today)
        wait_r = report.get("wait_reason", "")
        next_w = report.get("next_watch",  "")
        body   = [
            "扫描{}只 | 通过门控{}只 | Fed={}%".format(
                scanned, passed, macro.get("fedRate", "-")),
            "",
            "⏳ 等待: " + wait_r[:200],
        ]
        if next_w:
            body += ["", "👁 关注: " + next_w]
        if summary:
            body += ["", "📝 " + summary]
        return title, "\n".join(body), -1, "none"


def push_pushover(report: dict) -> bool:
    if not CFG["PUSHOVER_TOKEN"] or not CFG["PUSHOVER_USER"]:
        log.info("Pushover未配置，跳过")
        return False
    title, message, priority, sound = _fmt_report_text(report)
    payload = {
        "token":    CFG["PUSHOVER_TOKEN"],
        "user":     CFG["PUSHOVER_USER"],
        "title":    title,
        "message":  message[:1024],
        "priority": priority,
        "sound":    sound,
    }
    # 高优先级需要 retry + expire
    if priority == 1:
        payload.update({"retry": 60, "expire": 3600})
    try:
        r = requests.post("https://api.pushover.net/1/messages.json",
                          data=payload, timeout=10)
        r.raise_for_status()
        log.info(f"Pushover推送成功: {report.get('action')}")
        return True
    except Exception as e:
        log.error(f"Pushover推送失败: {e}")
        return False


def push_telegram(report: dict) -> bool:
    if not CFG["TG_BOT_TOKEN"] or not CFG["TG_CHAT_ID"]:
        return False
    title, message, _, _ = _fmt_report_text(report)
    text = f"*{title}*\n\n{message}"
    url  = f"https://api.telegram.org/bot{CFG['TG_BOT_TOKEN']}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id":    CFG["TG_CHAT_ID"],
            "text":       text[:4096],
            "parse_mode": "Markdown",
        }, timeout=10)
        r.raise_for_status()
        log.info("Telegram推送成功")
        return True
    except Exception as e:
        log.error(f"Telegram推送失败: {e}")
        return False


def push_all(report: dict) -> bool:
    ok1 = push_pushover(report)
    ok2 = push_telegram(report)
    ok3 = push_feishu(report, CFG.get("FEISHU_WEBHOOK",   ""))
    ok3b = push_feishu(report, CFG.get("FEISHU_WEBHOOK_2", ""))
    ok4 = push_discord(report, CFG.get("DISCORD_WEBHOOK",  ""))
    if not any([ok1, ok2, ok3, ok3b, ok4]):
        log.warning("所有推送渠道均未配置或失败")
    return ok1 or ok2 or ok3 or ok3b or ok4


# ══════════════════════════════════════════════════════════════
#  Phase2 单只处理（抽出来方便并发调用）
# ══════════════════════════════════════════════════════════════
def _process_one(ticker: str, db_path: Path,
                 today: str, mthresh: dict, fed_rate: float = 0.0) -> Optional[dict]:
    # 每只股票用独立的 DB 连接，彻底避免多线程 SQLite 冲突
    try:
        raw    = fetch_phase2(ticker)
        qg     = quality_gate(raw)
        fg     = forward_gate(raw)
        scored = score_dna(raw)

        # 独立连接读写历史
        con = sqlite3.connect(db_path)
        hist = load_history(con, ticker)
        if not hist or hist[-1]["date"] != today:
            hist.append({"date": today, "dna": scored["dna"]})

        td   = trend_score(hist)

        # 史诗跃升检测：从 DB 取近两季基本面对比，豁免 MIN_HIST / REQUIRE_ACCEL
        epic = check_epic_turnaround(con, ticker, today, scored["dna"])
        if epic and td["score"] < MIN_TREND:
            td = dict(td); td["score"] = 65   # 基本面突变赋予初始趋势分

        comp = composite(scored["dna"], td["score"], len(hist), fg, is_epic=epic)

        # REQUIRE_ACCEL：严格模式减速阈值；史诗跃升豁免
        accel_thresh = 20 if REQUIRE_ACCEL else 40
        hist_ok = epic or len(hist) >= MIN_HIST
        accel_ok = epic or fg.get("decel_score", 0) <= accel_thresh

        # 门控失败原因
        gate_fail = ""
        if not qg["pass"]:
            gate_fail = qg["reasons"][0]
        elif not accel_ok:
            gate_fail = f"增速减速({fg.get('decel_score',0):.0f}ppt):" + fg.get("reason", "")
        elif not hist_ok:
            gate_fail = "历史不足(" + str(len(hist)) + "天)"
        elif scored["dna"] < MIN_DNA:
            gate_fail = "DNA" + str(scored["dna"]) + "<" + str(MIN_DNA)
        elif td["score"] < MIN_TREND:
            gate_fail = "趋势" + str(td["score"]) + "<" + str(MIN_TREND)
        elif comp < mthresh["threshold"]:
            gate_fail = "综合" + str(comp) + "<" + str(mthresh["threshold"])

        all_pass = (qg["pass"] and accel_ok and hist_ok
                    and scored["dna"] >= MIN_DNA
                    and td["score"] >= MIN_TREND
                    and comp >= mthresh["threshold"])

        # Extract extra gate values for daily_signals
        gm      = qg.get("gm", 0)
        rev_yoy = scored["meta"]["rev"]
        b4q     = scored["meta"]["beats"]
        eps_items = raw.get("earnings") or []
        eps_sur = None
        if eps_items:
            e = eps_items[0]
            a, est = e.get("actual"), e.get("estimate")
            if a is not None and est and abs(float(est)) > 0.001:
                eps_sur = round((float(a) - float(est)) / abs(float(est)) * 100, 1)

        save_score(con, ticker, today,
                   scored["dna"], td["score"], comp,
                   mthresh["threshold"], all_pass,
                   scored["meta"]["price"], gm, rev_yoy, eps_sur, b4q,
                   gate_fail, fed_rate)
        con.close()

        return {
            "ticker":    ticker,
            "dna":       scored["dna"],
            "dims":      scored["dims"],
            "failed":    scored["failed"],
            "signals":   scored["signals"],
            "trend":     td,
            "comp":      comp,
            "meta":      scored["meta"],
            "fwd_gate":  fg,
            "q_gate":    qg,
            "all_gates": all_pass,
            "gate_fail": gate_fail,
            "epic":      epic,
        }
    except Exception as e:
        log.warning("P2 %s: %s", ticker, e)
        return None


# ══════════════════════════════════════════════════════════════
#  主扫描流程
# ══════════════════════════════════════════════════════════════
def run_scan(force: bool = False) -> dict:
    today = str(date.today())
    log.info(f"{'='*55}")
    log.info(f"开始扫描 {today}")

    con = init_db()

    # 每日去重保护
    if not force and already_scanned_today(con):
        log.info(f"{today} 已扫描完成，跳过（使用 --force 强制重跑）")
        row = con.execute("SELECT json FROM reports WHERE report_date=?", (today,)).fetchone()
        con.close()
        return json.loads(row[0]) if row else {"action":"WAIT","_date":today}

    mark_scan_start(con, today)

    # ── 宏观 ──────────────────────────────────────────────────
    log.info("获取宏观数据…")
    macro   = get_macro()
    mthresh = macro_threshold(macro)
    log.info(f"Fed={macro['fedRate']}% | {macro['regime']} | 阈值={mthresh['threshold']}")

    # ── Phase 1: 快速初筛 ─────────────────────────────────────
    log.info(f"Phase1: 初筛 {len(TICKERS)} 只…")
    p1_pass = []
    for i, t in enumerate(TICKERS):
        q = fetch_phase1(t)
        price = float(q.get("c") or 0)
        if 2 <= price <= 3000 and q.get("dp") is not None:
            p1_pass.append(t)
        time.sleep(P1_DELAY)
        if (i+1) % 25 == 0:
            log.info(f"  P1 {i+1}/{len(TICKERS)} → 通过{len(p1_pass)}")

    log.info(f"Phase1完成: {len(p1_pass)}/{len(TICKERS)} 通过")

    # ── Phase 2: 顺序深度评分（避免并发导致的SQLite冲突和Finnhub限流）──
    log.info("Phase2: 深度评分 %d 只（顺序执行，稳定可靠）…", len(p1_pass))
    all_results = []

    for j, ticker in enumerate(p1_pass):
        result = _process_one(ticker, DB_PATH, today, mthresh, macro.get("fedRate", 0.0))
        if result:
            all_results.append(result)

        done = j + 1
        passed_so_far = sum(1 for r in all_results if r["all_gates"])
        if done % 10 == 0 or done == len(p1_pass):
            log.info("  P2 %d/%d | 通过门控: %d", done, len(p1_pass), passed_so_far)

        # 顺序请求：8个接口/只，间隔10秒 → 约48 req/min，安全在60限制内
        if j < len(p1_pass) - 1:
            time.sleep(10)

    all_results.sort(key=lambda x: x["comp"], reverse=True)
    passed = [r for r in all_results if r["all_gates"]]
    log.info(f"Phase2完成: {len(passed)} 只通过全部门控")

    # ── Claude 报告 ───────────────────────────────────────────
    log.info("生成 Claude 报告…")
    try:
        report = generate_report(passed, all_results, macro, mthresh, today)
        _enrich_next_watch(report, all_results)
        save_report(con, today, report.get("action","WAIT"), report)
        log.info(f"报告: {report.get('action')} | {report.get('summary','')[:80]}")
    except Exception as e:
        log.error(f"报告生成失败: {e}")
        report = {
            "action":      "WAIT",
            "wait_reason": f"报告生成失败: {e}",
            "summary":     f"扫描{len(all_results)}只，{len(passed)}只通过门控",
            "_date":       today,
            "_scanned":    len(all_results),
            "_passed":     len(passed),
        }
        save_report(con, today, "ERROR", report)

    # ── 扫描完成，报告已存 DB；推送由 scheduler 在北京时间 12:00 统一触发 ──
    mark_scan_done(con, today, len(all_results), len(passed))
    con.close()
    log.info(f"扫描完成 {today} | 用时见日志")
    log.info(f"{'='*55}\n")
    return report


# ══════════════════════════════════════════════════════════════
#  CLI 入口
# ══════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="NASDAQ 爆发股扫描器 v2")
    parser.add_argument("--force",        action="store_true", help="强制重跑（忽略今日已扫标记）")
    parser.add_argument("--push-test",    action="store_true", help="测试推送（不扫描）")
    parser.add_argument("--push-pending", action="store_true", help="立即推送 DB 中最近一条未推报告")
    args = parser.parse_args()

    if args.push_pending:
        ok = push_pending_report()
        print("推送:", "成功" if ok else "失败")
        return

    if args.push_test:
        log.info("推送测试中…")
        test_report = {
            "action": "BUY",
            "picks": [{
                "ticker": "TEST",
                "conviction": "STRONG",
                "alloc_pct": 70,
                "entry": 100.0, "stop": 85.0,
                "target6m": "+150%",
                "thesis": "这是一条推送测试消息",
                "catalyst": "测试催化剂",
                "risk": "测试风险",
            }],
            "analysis_process": "Quality质检：89只→28只通过；Outlook预期：28只→12只；Macro环境阈值=78（中性环境）；Signal信号最高：TEST DNA=84 趋势=67 综合=79 ✅；决策：1只完全通过，置信度强，建议重仓介入。",
            "summary": "推送功能测试正常",
            "_date": str(date.today()),
            "_scanned": 89,
            "_passed": 1,
            "_macro": {"fedRate": 4.50, "regime": "NEUTRAL", "yoyChange": 0.0},
        }
        ok = push_all(test_report)
        print("推送测试:", "成功" if ok else "失败（检查 PUSHOVER_TOKEN/USER、TG_BOT_TOKEN/CHAT_ID、FEISHU_WEBHOOK 或 DISCORD_WEBHOOK）")
        return

    if not CFG["ANTHROPIC_KEY"]:
        print("错误: 请设置 ANTHROPIC_KEY 环境变量")
        print("  export ANTHROPIC_KEY=sk-ant-...")
        sys.exit(1)

    report = run_scan(force=args.force)
    action = report.get("action", "WAIT")

    print(f"\n{'='*50}")
    print(f"日期: {report.get('_date')}  结果: {action}")
    print(f"扫描: {report.get('_scanned',0)}只  通过门控: {report.get('_passed',0)}只")
    if action == "BUY":
        for p in (report.get("picks") or []):
            print(f"  ▶ {p.get('ticker')} [{p.get('conviction','?')}] {p.get('alloc_pct',0)}% "
                  f"@${p.get('entry',0):.2f} 止损${p.get('stop',0):.2f} {p.get('target6m','')}")
    else:
        print(f"  等待: {report.get('wait_reason','')[:100]}")
        print(f"  关注: {report.get('next_watch','')}")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    main()

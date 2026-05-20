#!/usr/bin/env python3
"""
历史数据初始化脚本（只需运行一次）
从 Finnhub 拉取433只股票过去5年的历史季报数据
写入 SQLite，之后每天增量更新

预计运行时间: 约60-90分钟（433只 × 3个接口 × 限速）
运行方式: python3 init_history.py
完成后运行: python3 scanner_v2.py --force
"""

import os, sys, json, time, sqlite3, requests, logging
from datetime import datetime, date, timedelta
from pathlib import Path

# ── 配置 ──────────────────────────────────────────────────────
FINNHUB_KEY = os.getenv("FINNHUB_KEY", "d826l09r01qtiq8ur880d826l09r01qtiq8ur88g")
DATA_DIR    = Path(__file__).parent / "data"
DB_PATH     = DATA_DIR / "scanner.db"
FH_BASE     = "https://finnhub.io/api/v1"
YEARS_BACK  = 5   # 拉取过去5年数据

DATA_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(DATA_DIR / "init_history.log", encoding="utf-8"),
    ]
)
log = logging.getLogger("init")

# ── 433只扫描宇宙（与scanner_v2.py一致）─────────────────────
TICKERS = list(dict.fromkeys([
    "NVDA","AMD","MRVL","CRDO","ALAB","CAMT","SMCI","ARM","AVGO","QCOM",
    "MU","WDC","KLAC","LRCX","AMAT","ONTO","FORM","WOLF","MPWR","SLAB",
    "SWKS","QRVO","NXPI","TXN","ADI","MCHP","MTSI","DIOD","AMBA","SIGI",
    "COHU","ICHR","ACMR","NVMI","POWI","SITM","ALGM","OLED","LAZR","RMBS",
    "NBIS","IONQ","SOUN","PLTR","APP","TTD","BBAI","MSFT","GOOGL","META",
    "AMZN","ORCL","IBM","DELL","NTAP","PSTG","ADBE","CRM","WDAY","NOW",
    "VEEV","HUBS","DDOG","SNOW","NET","MDB","CFLT","ESTC","GTLB","FROG",
    "ZS","PANW","CRWD","S","FTNT","CYBR","QLYS","TENB","RPD","VRNS",
    "BRZE","ASAN","MNDY","SMAR","PLAN","SUMO","PCTY","PAYC","PAYX","ADSK",
    "ANSS","CDNS","SNPS","PTC","AZPN","NCNO","UPLD","TASK","INTA","BILL",
    "CELH","CAVA","DUOL","HIMS","WING","BROS","ELF","RDDT","HOOD","COIN",
    "DKNG","PENN","UBER","LYFT","ABNB","DASH","RIVN","LCID","EVGO","CHPT",
    "TMDX","GKOS","NARI","ACLS","AXNX","INSP","SWAV","SHOC","NVST","OMCL",
    "ALGN","ISRG","MASI","MMSI","ITGR","NVCR","TELA","ATRC","PODD","DXCM",
    "TDOC","AMWL","DOCS","ACAD","RARE","SRPT","PTCT","KRYS","PCVX","FATE",
    "MRNA","BNTX","NVAX","VRTX","REGN","BIIB","ALNY","BMRN","EXEL",
    "POWL","GTLS","ARIS","ACHR","GNRC","ENS","HUBB","ATKR","FIX","IESC",
    "MYRG","DY","PIKE","PRIM","MTZ","PWR","STRL","KBR","ENPH","FSLR",
    "ARRY","SHLS","RUN","NOVA","STEM","BE","PLUG","SEDG",
    "SOFI","AFRM","UPST","NU","CWAN","FLYW","OPEN","DAVE","LPRO","TREE",
    "UWMC","PFSI","LMND","ROOT","RYAN","IBKR","MKTX","LPLA","RJF","SF",
    "AXON","GLBE","ALKT","JAMF","CERT","ALRM","QTWO","WEAVE","EVER",
    "HWKN","MGY","CRK","CIVI","VRN","INMD","XMTR","RAMP","BLND",
    "SFIX","GENI","ACVA","NRDS","BFLY","PRAX","SAGE","VNET","CLFD",
    "SOLV","KVYO","CART","GEV","RXO","KNTK","VNT","ESAB","DOOR","WFRD",
    "JOBY","RKLB","PL","ASTS","LUNR","MNTS","SPIR",
    "DINO","MRO","SM","ESTE","RIG","BORR","NE","DO","PAAS","GOLD",
    "AEM","WPM","MP","LITE","IIVI","COHR","VIAVI","AGX","POWL",
    "TSM","SE","MELI","LI","GRAB","LEGN","ZLAB","BZ","YMM","QFIN",
    "SHOP","LSPD","NVEI","DND","GOTU",
]))

# ── 数据库 ────────────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
        -- 历史季报快照（每季度一行）
        CREATE TABLE IF NOT EXISTS quarterly_history (
            ticker      TEXT NOT NULL,
            period      TEXT NOT NULL,    -- 季报日期 YYYY-MM-DD
            rev_yoy     REAL,             -- 收入YoY增速 %
            gm          REAL,             -- 毛利率 %
            eps_beat    REAL,             -- EPS超预期幅度 %
            eps_actual  REAL,
            eps_est     REAL,
            price_close REAL,             -- 季报日收盘价（近似）
            dna_score   INTEGER,          -- 计算的DNA分
            PRIMARY KEY (ticker, period)
        );

        -- 每日扫描分（从scanner_v2.py写入）
        CREATE TABLE IF NOT EXISTS daily_scores (
            ticker    TEXT NOT NULL,
            scan_date TEXT NOT NULL,
            dna       INTEGER,
            comp      INTEGER,
            price     REAL,
            meta_json TEXT,
            PRIMARY KEY (ticker, scan_date)
        );

        -- 每日报告
        CREATE TABLE IF NOT EXISTS reports (
            report_date TEXT PRIMARY KEY,
            action      TEXT,
            json        TEXT,
            pushed      INTEGER DEFAULT 0,
            created_at  TEXT
        );

        -- 扫描日志
        CREATE TABLE IF NOT EXISTS scan_log (
            scan_date   TEXT PRIMARY KEY,
            started_at  TEXT,
            finished_at TEXT,
            status      TEXT,
            n_scanned   INTEGER DEFAULT 0,
            n_passed    INTEGER DEFAULT 0
        );

        -- 历史导入进度（断点续传）
        CREATE TABLE IF NOT EXISTS init_progress (
            ticker      TEXT PRIMARY KEY,
            status      TEXT,    -- 'done' / 'failed' / 'skip'
            n_quarters  INTEGER DEFAULT 0,
            updated_at  TEXT
        );
    """)
    con.commit()
    return con

# ── Finnhub API ───────────────────────────────────────────────
_sess = requests.Session()
_sess.headers.update({"User-Agent": "nasdaq-scanner/2.0"})

def fh(path, params=None, retries=3):
    p = dict(params or {})
    p["token"] = FINNHUB_KEY
    for i in range(retries):
        try:
            r = _sess.get(f"{FH_BASE}{path}", params=p, timeout=15)
            if r.status_code == 429:
                log.warning("Rate limit, wait 10s")
                time.sleep(10); continue
            if r.status_code >= 500:
                time.sleep(3); continue
            if r.status_code == 404:
                return {}
            r.raise_for_status()
            return r.json() or {}
        except Exception as e:
            if i == retries-1:
                log.debug(f"fh {path} failed: {e}")
                return {}
            time.sleep(2)
    return {}

# ── DNA 评分（与 scanner_v2.py 完全一致的逻辑）─────────────────
def calc_dna(quarters_history):
    """
    quarters_history: 按时间顺序排列的列表
    每项: {"period", "rev_yoy", "gm", "eps_beat", "eps_actual", "eps_est"}
    返回: 对最新季报的DNA分
    """
    if not quarters_history:
        return 0

    recent4 = quarters_history[-4:]
    curr    = quarters_history[-1]
    rev_yoy = curr.get("rev_yoy", 0) or 0
    gm      = curr.get("gm", 0) or 0
    eps_beat= curr.get("eps_beat", 0) or 0

    dims = {}

    # 毛利率
    if gm <= 0:
        dims["margin"] = 0
    else:
        dims["margin"] = (100 if gm>70 else 82 if gm>55 else 62 if gm>40
                          else 38 if gm>25 else 18 if gm>15 else 5)
        if len(quarters_history) >= 2:
            prev_gm = quarters_history[-2].get("gm", 0) or 0
            if gm > prev_gm + 3:
                dims["margin"] = min(100, dims["margin"] + 10)

    # 收入加速
    if rev_yoy <= 0:
        dims["rev_accel"] = 0
    else:
        base = (100 if rev_yoy>200 else 88 if rev_yoy>100 else 68 if rev_yoy>50
                else 45 if rev_yoy>30 else 28 if rev_yoy>20 else 10)
        accel = 0
        if len(quarters_history) >= 2:
            prev_rev = quarters_history[-2].get("rev_yoy", 0) or 0
            if prev_rev > 0 and rev_yoy > prev_rev * 1.1:
                accel = 15
        dims["rev_accel"] = min(100, base + accel)

    # 连续超预期
    beats = sum(1 for q in recent4 if (q.get("eps_beat") or 0) > 5)
    base_b = (100 if beats>=4 else 80 if beats==3 else 55 if beats==2
              else 25 if beats==1 else 0)
    mag = (20 if eps_beat>50 else 12 if eps_beat>25 else 5 if eps_beat>10 else 0)
    dims["beats"] = min(100, base_b + mag)

    # 拆分（用公司年龄代理）
    dims["spinoff"] = 15  # 历史数据无法精确判断，保守给默认值

    # 叙事
    if rev_yoy > 100 and beats >= 3:   dims["narrative"] = 85
    elif rev_yoy > 50 and beats >= 2:  dims["narrative"] = 65
    elif rev_yoy > 20 and beats >= 1:  dims["narrative"] = 45
    else:                               dims["narrative"] = 20

    # 供需
    if len(quarters_history) >= 2:
        prev_rev2 = quarters_history[-2].get("rev_yoy", 0) or 0
        if prev_rev2 > 0 and rev_yoy > prev_rev2 * 1.5 and rev_yoy > 50:
            dims["supply"] = 85
        elif prev_rev2 > 0 and rev_yoy > prev_rev2 * 1.2:
            dims["supply"] = 60
        else:
            dims["supply"] = 35
    else:
        dims["supply"] = 30

    W = {"margin":24,"rev_accel":26,"beats":22,"spinoff":10,"narrative":10,"supply":8}
    dna = round(sum(dims[k]*W[k] for k in W) / sum(W.values()))

    # 关键维度惩罚
    failed = []
    if dims["margin"]    < 40: failed.append("margin")
    if dims["rev_accel"] < 45: failed.append("rev_accel")
    if dims["beats"]     < 35: failed.append("beats")
    if failed:
        dna = round(dna * 0.5)

    return max(0, min(100, dna))

# ── 拉取单只股票历史数据 ───────────────────────────────────────
def fetch_and_store(ticker, con):
    """
    拉取并存储一只股票的历史季报数据
    使用3个Finnhub端点:
      1. /stock/earnings        - EPS历史超预期（免费，回溯20季）
      2. /stock/metric          - 当前财务指标（含TTM收入增速/毛利率）
      3. /stock/financials      - 历史季报收入（免费版支持quarterly）
    """
    # 检查是否已处理过（断点续传）
    row = con.execute(
        "SELECT status FROM init_progress WHERE ticker=?", (ticker,)
    ).fetchone()
    if row and row[0] == "done":
        return "skip"

    quarters_data = {}  # period -> dict

    # ── 接口1: 历史EPS超预期（最关键，有20季历史）────────────
    eps_raw = fh("/stock/earnings", {"symbol": ticker, "limit": 20})
    time.sleep(0.5)

    if isinstance(eps_raw, list):
        for e in eps_raw:
            period = e.get("period") or e.get("date", "")
            if not period:
                continue
            actual = e.get("actual")
            est    = e.get("estimate")
            if actual is not None and est is not None and est != 0:
                beat_pct = round((actual - est) / abs(est) * 100, 1)
            else:
                beat_pct = 0

            # 过滤5年内的数据
            try:
                q_date = datetime.strptime(period[:10], "%Y-%m-%d")
                if q_date < datetime.now() - timedelta(days=365*YEARS_BACK):
                    continue
            except Exception:
                continue

            quarters_data[period[:10]] = {
                "period":     period[:10],
                "eps_actual": actual,
                "eps_est":    est,
                "eps_beat":   beat_pct,
                "rev_yoy":    0,
                "gm":         0,
            }

    # ── 接口2: 当前基础指标（TTM收入增速、毛利率）──────────────
    metrics_raw = fh("/stock/metric", {"symbol": ticker, "metric": "all"})
    metrics     = (metrics_raw or {}).get("metric", {}) or {}
    time.sleep(0.5)

    curr_rev_q  = float(metrics.get("revenueGrowthQuarterlyYoy") or 0)
    curr_gm     = float(metrics.get("grossMarginTTM") or 0)
    curr_price  = 0  # 将由quote补充

    # ── 接口3: 历史季度财报（收入、毛利）────────────────────────
    fin_raw = fh("/stock/financials", {
        "symbol":    ticker,
        "statement": "ic",      # income statement
        "freq":      "quarterly"
    })
    time.sleep(0.5)

    fin_data = (fin_raw or {}).get("data", []) or []
    fin_data = [f for f in fin_data if isinstance(f, dict)]

    # 从历史财报提取收入数据，计算YoY增速
    rev_by_period = {}
    for f in fin_data:
        period = (f.get("period") or "")[:10]
        if not period:
            continue
        # 尝试各种字段名
        rev = (f.get("revenue") or f.get("totalRevenue") or
               f.get("sales") or 0)
        if rev:
            rev_by_period[period] = float(rev)

    # 计算YoY
    sorted_periods = sorted(rev_by_period.keys())
    for i, period in enumerate(sorted_periods):
        # 找大约1年前的数据（4个季度前）
        yoy_rev = 0
        if i >= 4:
            prev_period = sorted_periods[i-4]
            prev_rev    = rev_by_period.get(prev_period, 0)
            curr_rev    = rev_by_period.get(period, 0)
            if prev_rev > 0 and curr_rev > 0:
                yoy_rev = round((curr_rev - prev_rev) / prev_rev * 100, 1)

        if period in quarters_data:
            quarters_data[period]["rev_yoy"] = yoy_rev
        else:
            try:
                q_date = datetime.strptime(period, "%Y-%m-%d")
                if q_date >= datetime.now() - timedelta(days=365*YEARS_BACK):
                    quarters_data[period] = {
                        "period": period, "eps_actual": 0, "eps_est": 0,
                        "eps_beat": 0, "rev_yoy": yoy_rev, "gm": curr_gm,
                    }
            except Exception:
                pass

    # 对最近的季报补充当前毛利率（历史毛利率Finnhub免费版无法准确获取）
    if quarters_data:
        latest_period = max(quarters_data.keys())
        quarters_data[latest_period]["gm"] = curr_gm

        # 对其他历史季报，用当前毛利率的衰减版本作为近似
        sorted_qs = sorted(quarters_data.keys(), reverse=True)
        for idx, p in enumerate(sorted_qs):
            if idx == 0:
                quarters_data[p]["gm"] = curr_gm
            else:
                # 简单线性衰减（越久远越不准确）
                quarters_data[p]["gm"] = max(0, curr_gm - idx * 2)

    if not quarters_data:
        con.execute(
            "INSERT OR REPLACE INTO init_progress VALUES (?,?,0,?)",
            (ticker, "failed", datetime.utcnow().isoformat())
        )
        con.commit()
        return "failed"

    # ── 计算每季的DNA分并写入数据库 ─────────────────────────────
    sorted_qs = sorted(quarters_data.values(), key=lambda x: x["period"])
    n_saved   = 0

    for i, q in enumerate(sorted_qs):
        history_so_far = sorted_qs[:i+1]
        dna = calc_dna(history_so_far)

        try:
            con.execute(
                "INSERT OR REPLACE INTO quarterly_history VALUES (?,?,?,?,?,?,?,?,?)",
                (ticker, q["period"], q.get("rev_yoy",0), q.get("gm",0),
                 q.get("eps_beat",0), q.get("eps_actual"), q.get("eps_est"),
                 q.get("price_close",0), dna)
            )
            n_saved += 1

            # 同时写入 daily_scores 表（让scanner_v2.py能读到历史趋势）
            # 用季报日期作为"扫描日期"
            con.execute(
                "INSERT OR IGNORE INTO daily_scores VALUES (?,?,?,?,?,?)",
                (ticker, q["period"], dna,
                 round(dna * 0.85),  # 初始综合分（无趋势分时）
                 q.get("price_close", 0),
                 json.dumps({"name":"","sector":"","source":"history"}))
            )
        except Exception as e:
            log.debug(f"Save {ticker}/{q['period']}: {e}")

    con.commit()

    con.execute(
        "INSERT OR REPLACE INTO init_progress VALUES (?,?,?,?)",
        (ticker, "done", n_saved, datetime.utcnow().isoformat())
    )
    con.commit()

    return n_saved

# ── 主流程 ────────────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info(f"历史数据初始化开始")
    log.info(f"股票数量: {len(TICKERS)} 只")
    log.info(f"数据范围: 过去 {YEARS_BACK} 年")
    log.info(f"数据库: {DB_PATH}")
    log.info("=" * 60)

    con = init_db()

    # 检查已完成数量
    done_count = con.execute(
        "SELECT COUNT(*) FROM init_progress WHERE status='done'"
    ).fetchone()[0]
    log.info(f"已完成: {done_count}/{len(TICKERS)} 只（支持断点续传）")

    success, failed, skipped = 0, 0, 0
    total = len(TICKERS)

    for i, ticker in enumerate(TICKERS):
        log.info(f"[{i+1}/{total}] {ticker}")
        try:
            result = fetch_and_store(ticker, con)
            if result == "skip":
                skipped += 1
                log.info(f"  → 跳过（已完成）")
            elif result == "failed":
                failed += 1
                log.warning(f"  → 失败（无数据）")
            else:
                success += 1
                log.info(f"  → 完成，保存 {result} 个季度")
        except Exception as e:
            failed += 1
            log.error(f"  → 错误: {e}")
            con.execute(
                "INSERT OR REPLACE INTO init_progress VALUES (?,?,0,?)",
                (ticker, "failed", datetime.utcnow().isoformat())
            )
            con.commit()

        # 进度报告（每50只）
        if (i+1) % 50 == 0:
            log.info(f"\n  进度: {i+1}/{total} | 成功:{success} 失败:{failed} 跳过:{skipped}\n")

        # 限速：每只3个接口，间隔1秒，约3秒/只 → ~20只/分钟 → 安全在60req/min内
        if result != "skip":
            time.sleep(1.5)

    con.close()

    log.info("\n" + "="*60)
    log.info(f"初始化完成!")
    log.info(f"  成功: {success} 只")
    log.info(f"  失败: {failed} 只（无数据或API错误）")
    log.info(f"  跳过: {skipped} 只（断点续传）")
    log.info(f"  数据库: {DB_PATH}")
    log.info("="*60)
    log.info("\n下一步: python3 scanner_v2.py --force")

if __name__ == "__main__":
    main()

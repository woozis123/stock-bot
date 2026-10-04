import os, json, sqlite3, asyncio, logging, datetime as dt, urllib.parse
from zoneinfo import ZoneInfo
import feedparser, yfinance as yf
from anthropic import Anthropic
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()
logging.basicConfig(level=logging.INFO)
TOKEN = os.environ["TELEGRAM_TOKEN"]
OWNER = int(os.environ["TELEGRAM_CHAT_ID"])
MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
KST = ZoneInfo("Asia/Seoul")
llm = Anthropic()
DB = "stock.db"

# ---------------- DB ----------------
def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

def init_db():
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS sectors(name TEXT PRIMARY KEY)")
        c.execute("""CREATE TABLE IF NOT EXISTS holdings(
            ticker TEXT PRIMARY KEY, name TEXT, qty REAL, avg REAL, bought_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS trades(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, side TEXT,
            price REAL, qty REAL, at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS recs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, action TEXT,
            reason TEXT, at TEXT)""")

# ---------------- 데이터 수집 ----------------
def get_price_info(ticker):
    h = yf.Ticker(ticker).history(period="3mo")
    if h.empty: return None
    close = h["Close"]; last = float(close.iloc[-1])
    def chg(n): return round((last / float(close.iloc[-n-1]) - 1) * 100, 2) if len(close) > n else None
    return {"last": round(last, 2), "chg_1d%": chg(1), "chg_5d%": chg(5), "chg_20d%": chg(20),
            "high_3m": round(float(close.max()), 2), "low_3m": round(float(close.min()), 2),
            "vol_ratio_5d_vs_20d": round(float(h["Volume"].tail(5).mean() / h["Volume"].tail(20).mean()), 2)}

def get_news(ticker, name, n=8):
    items, seen = [], set()
    try:
        for it in (yf.Ticker(ticker).news or [])[:n]:
            ct = it.get("content", it)
            title = ct.get("title"); 
            url = (ct.get("canonicalUrl") or {}).get("url") or ct.get("link")
            if title and title not in seen:
                seen.add(title); items.append({"title": title, "url": url, "date": ct.get("pubDate", "")})
    except Exception as e: logging.warning("yf news %s: %s", ticker, e)
    kr = ticker.endswith((".KS", ".KQ"))
    q = urllib.parse.quote(f"{name} 주가" if kr else f"{name} stock")
    url = (f"https://news.google.com/rss/search?q={q}+when:3d&hl=ko&gl=KR&ceid=KR:ko" if kr
           else f"https://news.google.com/rss/search?q={q}+when:3d&hl=en-US&gl=US&ceid=US:en")
    for e in feedparser.parse(url).entries[:n]:
        if e.title not in seen:
            seen.add(e.title); items.append({"title": e.title, "url": e.link, "date": e.get("published", "")})
    return items[:n * 2]

# ---------------- AI 판단 ----------------
def ask_json(system, user, max_tokens=1500):
    r = llm.messages.create(model=MODEL, max_tokens=max_tokens, system=system,
                            messages=[{"role": "user", "content": user}])
    t = r.content[0].text.strip()
    t = t[t.find("{") if t.find("{") != -1 and (t.find("[") == -1 or t.find("{") < t.find("[")) else t.find("["):]
    end = max(t.rfind("}"), t.rfind("]"))
    return json.loads(t[:end + 1])

JUDGE_SYS = """너는 신중한 주식 애널리스트다. 제공된 최근 뉴스와 주가 데이터만 근거로 판단한다.
수익을 보장하지 말고, 근거가 부족하면 '관망/보유'로 하고 확신도를 낮춰라. 뉴스에 없는 사실을 지어내지 마라.
JSON만 출력: {"action": "매수|관망|매도|보유", "confidence": 1-5,
"reasons": ["근거1(뉴스 제목 인용)","근거2"], "risks": ["리스크"], "key_news_idx": [근거가 된 뉴스 번호]}
보유 중이 아닌 종목은 매수/관망/회피(=매도 대신 '관망')로, 보유 중인 종목은 보유/매도로 판단하라."""

def judge(ticker, name, holding=None):
    price, news = get_price_info(ticker), get_news(ticker, name)
    ctx = {"ticker": ticker, "name": name, "price": price,
           "my_position": ({"avg_price": holding["avg"], "qty": holding["qty"],
                            "pnl%": round((price["last"] / holding["avg"] - 1) * 100, 2)}
                           if holding and price else None),
           "news": [{"i": i, "title": n["title"], "date": n["date"]} for i, n in enumerate(news)]}
    res = ask_json(JUDGE_SYS, json.dumps(ctx, ensure_ascii=False))
    res["links"] = [news[i]["url"] for i in res.get("key_news_idx", []) if isinstance(i, int) and i < len(news)][:3]
    res["price"] = price
    return res

ICON = {"매수": "🟢", "보유": "🟢", "관망": "🟡", "매도": "🔴"}
def fmt(ticker, name, r, tag=""):
    p = r.get("price") or {}
    s = f"{ICON.get(r['action'], '⚪')} {name} ({ticker}) {tag}\n판단: {r['action']} (확신도 {r.get('confidence', '?')}/5)\n"
    if p: s += f"현재가 {p['last']} | 1일 {p['chg_1d%']}% | 5일 {p['chg_5d%']}% | 20일 {p['chg_20d%']}%\n"
    s += "근거:\n" + "\n".join(f" ・{x}" for x in r.get("reasons", []))
    s += "\n리스크: " + " / ".join(r.get("risks", []))
    if r["links"]: s += "\n출처:\n" + "\n".join(r["links"])
    return s

def candidates(sector, n=5):
    return ask_json("너는 한국/미국 주식 전문가다. JSON 배열만 출력.",
        f"'{sector}' 섹터의 대표 상장 종목 {n}개(한국 {n//2+1}, 미국 {n-n//2-1}). "
        'Yahoo Finance 티커 형식(예: 005930.KS, 000660.KS, 코스닥은 .KQ, NVDA). '
        '[{"ticker":"","name":"영문 또는 한글 회사명"}]', 600)

# ---------------- 리포트 ----------------
def build_report():
    out = [f"📊 {dt.datetime.now(KST):%Y-%m-%d} 아침 리포트"]
    with db() as c:
        hold = c.execute("SELECT * FROM holdings").fetchall()
        sectors = [r["name"] for r in c.execute("SELECT name FROM sectors")]
    out.append("━━ 보유 종목 (팔지/유지) ━━" if hold else "보유 종목 없음 (/bought 로 등록)")
    for h in hold:
        try:
            r = judge(h["ticker"], h["name"], h)
            p = r["price"]; pnl = f"{(p['last']/h['avg']-1)*100:+.1f}%" if p else ""
            out.append(fmt(h["ticker"], h["name"], r, f"[보유 {pnl}]"))
            save_rec(h["ticker"], r)
        except Exception as e: out.append(f"⚠️ {h['name']} 분석 실패: {e}")
    held = {h["ticker"] for h in hold}
    for s in sectors:
        out.append(f"━━ [{s}] 오늘의 추천 ━━")
        try:
            for cd in candidates(s, 4):
                if cd["ticker"] in held: continue
                try:
                    r = judge(cd["ticker"], cd["name"]); save_rec(cd["ticker"], r)
                    out.append(fmt(cd["ticker"], cd["name"], r))
                except Exception as e: out.append(f"⚠️ {cd['name']} 실패: {e}")
        except Exception as e: out.append(f"⚠️ 섹터 후보 생성 실패: {e}")
    out.append("\n※ AI 참고 의견이며 투자 손익의 책임은 본인에게 있습니다.")
    return out

def save_rec(ticker, r):
    with db() as c:
        c.execute("INSERT INTO recs(ticker,action,reason,at) VALUES(?,?,?,?)",
                  (ticker, r["action"], json.dumps(r.get("reasons", []), ensure_ascii=False), dt.datetime.now(KST).isoformat()))

async def send_long(bot, chat_id, blocks):
    buf = ""
    for b in blocks:
        if len(buf) + len(b) > 3800:
            await bot.send_message(chat_id, buf, disable_web_page_preview=True); buf = ""
        buf += b + "\n\n"
    if buf.strip(): await bot.send_message(chat_id, buf, disable_web_page_preview=True)

async def daily(context: ContextTypes.DEFAULT_TYPE):
    blocks = await asyncio.to_thread(build_report)
    await send_long(context.bot, OWNER, blocks)

# ---------------- 명령어 ----------------
def owner_only(fn):
    async def w(u: Update, c: ContextTypes.DEFAULT_TYPE):
        if u.effective_chat.id != OWNER: return
        await fn(u, c)
    return w

@owner_only
async def start(u, c):
    await u.message.reply_text(
        "/sector 반도체 - 관심 섹터 등록(매일 추천)\n/unsector 반도체 - 섹터 삭제\n"
        "/recommend 반도체 - 지금 바로 추천\n/bought 티커 가격 수량 - 매수 기록 (예: /bought 000660.KS 190000 10)\n"
        "/sold 티커 [가격] - 매도 기록\n/holdings - 보유 목록\n/report - 지금 리포트 받기\n"
        "티커: 한국 005930.KS(코스닥 .KQ), 미국 NVDA")

@owner_only
async def sector(u, c):
    s = " ".join(c.args)
    if not s: return await u.message.reply_text("/sector 반도체")
    with db() as x: x.execute("INSERT OR IGNORE INTO sectors VALUES(?)", (s,))
    await u.message.reply_text(f"'{s}' 등록. 매일 아침 추천에 포함됩니다.")

@owner_only
async def unsector(u, c):
    with db() as x: x.execute("DELETE FROM sectors WHERE name=?", (" ".join(c.args),))
    await u.message.reply_text("삭제했습니다.")

@owner_only
async def recommend(u, c):
    s = " ".join(c.args)
    if not s: return await u.message.reply_text("/recommend 반도체")
    await u.message.reply_text("뉴스 수집·분석 중… (1~2분)")
    def run():
        out = []
        for cd in candidates(s, 5):
            try: out.append(fmt(cd["ticker"], cd["name"], judge(cd["ticker"], cd["name"])))
            except Exception as e: out.append(f"⚠️ {cd.get('name')} 실패: {e}")
        return out
    await send_long(c.bot, OWNER, await asyncio.to_thread(run))

@owner_only
async def bought(u, c):
    try:
        t, price, qty = c.args[0].upper(), float(c.args[1]), float(c.args[2])
    except Exception: return await u.message.reply_text("/bought 티커 가격 수량")
    name = await asyncio.to_thread(lambda: yf.Ticker(t).info.get("shortName") or t)
    now = dt.datetime.now(KST).isoformat()
    with db() as x:
        h = x.execute("SELECT * FROM holdings WHERE ticker=?", (t,)).fetchone()
        if h:
            tq = h["qty"] + qty; avg = (h["avg"] * h["qty"] + price * qty) / tq
            x.execute("UPDATE holdings SET qty=?, avg=? WHERE ticker=?", (tq, avg, t))
        else: x.execute("INSERT INTO holdings VALUES(?,?,?,?,?)", (t, name, qty, price, now))
        x.execute("INSERT INTO trades(ticker,side,price,qty,at) VALUES(?,?,?,?,?)", (t, "BUY", price, qty, now))
    await u.message.reply_text(f"✅ 매수 기록: {name} {qty}주 @ {price}")

@owner_only
async def sold(u, c):
    if not c.args: return await u.message.reply_text("/sold 티커 [가격]")
    t = c.args[0].upper(); price = float(c.args[1]) if len(c.args) > 1 else None
    with db() as x:
        h = x.execute("SELECT * FROM holdings WHERE ticker=?", (t,)).fetchone()
        if not h: return await u.message.reply_text("보유 중이 아닙니다.")
        x.execute("DELETE FROM holdings WHERE ticker=?", (t,))
        x.execute("INSERT INTO trades(ticker,side,price,qty,at) VALUES(?,?,?,?,?)",
                  (t, "SELL", price, h["qty"], dt.datetime.now(KST).isoformat()))
    pnl = f" (수익률 {(price/h['avg']-1)*100:+.1f}%)" if price else ""
    await u.message.reply_text(f"✅ 매도 기록: {h['name']}{pnl}")

@owner_only
async def holdings(u, c):
    with db() as x: rows = x.execute("SELECT * FROM holdings").fetchall()
    await u.message.reply_text("\n".join(f"{r['name']} ({r['ticker']}) {r['qty']:g}주 평단 {r['avg']:,.2f}" for r in rows) or "보유 종목 없음")

@owner_only
async def report(u, c):
    await u.message.reply_text("리포트 생성 중…")
    await send_long(c.bot, OWNER, await asyncio.to_thread(build_report))

def main():
    init_db()
    app = Application.builder().token(TOKEN).build()
    for n, f in [("start", start), ("sector", sector), ("unsector", unsector), ("recommend", recommend),
                 ("bought", bought), ("sold", sold), ("holdings", holdings), ("report", report)]:
        app.add_handler(CommandHandler(n, f))
    app.job_queue.run_daily(daily, time=dt.time(int(os.getenv("REPORT_HOUR", 8)), int(os.getenv("REPORT_MINUTE", 0)), tzinfo=KST))
    app.run_polling()

if __name__ == "__main__":
    main()

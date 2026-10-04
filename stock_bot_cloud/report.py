"""GitHub Actions용: 실행 -> 텔레그램 메시지 수집/반영 -> (report 모드면) 리포트 전송 -> 종료
사용: python report.py report   (매일 아침 리포트)
      python report.py sync     (메시지 반영 + 확인 답장만)"""
import os, sys, json, logging, datetime as dt, urllib.parse
from zoneinfo import ZoneInfo
import requests, feedparser, yfinance as yf
from anthropic import Anthropic

logging.basicConfig(level=logging.INFO)
TOKEN = os.environ["TELEGRAM_TOKEN"]
OWNER = int(os.environ["TELEGRAM_CHAT_ID"])
MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
KST = ZoneInfo("Asia/Seoul")
llm = Anthropic()
API = f"https://api.telegram.org/bot{TOKEN}"

def load(p, d):
    try: return json.load(open(p, encoding="utf-8"))
    except Exception: return d
def save(p, o): json.dump(o, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

# ---------- 텔레그램 ----------
def send(text):
    for i in range(0, len(text), 3800):
        r = requests.post(f"{API}/sendMessage", json={"chat_id": OWNER, "text": text[i:i+3800],
                          "disable_web_page_preview": True}, timeout=30)
        if not r.ok: logging.error("telegram: %s", r.text)

def send_blocks(blocks):
    buf = ""
    for b in blocks:
        if len(buf) + len(b) > 3500: send(buf); buf = ""
        buf += b + "\n\n"
    if buf.strip(): send(buf)

def fetch_messages(state):
    r = requests.get(f"{API}/getUpdates", params={"offset": state["offset"], "timeout": 0}, timeout=30).json()
    msgs = []
    for u in r.get("result", []):
        state["offset"] = u["update_id"] + 1
        m = u.get("message") or {}
        if m.get("chat", {}).get("id") == OWNER and m.get("text"):
            msgs.append(m["text"])
    return msgs

# ---------- LLM ----------
def ask_json(system, user, max_tokens=1500):
    r = llm.messages.create(model=MODEL, max_tokens=max_tokens, system=system,
                            messages=[{"role": "user", "content": user}])
    t = r.content[0].text.strip()
    a = [i for i in (t.find("{"), t.find("[")) if i != -1]
    t = t[min(a):]; t = t[:max(t.rfind("}"), t.rfind("]")) + 1]
    return json.loads(t)

PARSE_SYS = """사용자의 텔레그램 메시지들을 주식 기록 명령으로 변환한다. JSON 배열만 출력.
각 원소: {"type":"buy","ticker":"Yahoo티커","name":"종목명","price":숫자,"qty":숫자}
 | {"type":"sell","ticker":"Yahoo티커","price":숫자또는null}
 | {"type":"add_sector","name":"반도체"} | {"type":"remove_sector","name":"반도체"}
한국 코스피는 .KS, 코스닥은 .KQ (예: 삼성전자 005930.KS, SK하이닉스 000660.KS), 미국은 NVDA처럼.
주식 기록과 무관한 메시지는 무시. 불확실하면 {"type":"unclear","text":"원문"} 을 넣어라.
sell의 종목은 현재 보유 목록의 티커와 매칭하라."""

def apply_messages(msgs, cfg, hold):
    if not msgs: return []
    cmds = ask_json(PARSE_SYS, json.dumps({"messages": msgs, "current_holdings":
        {t: h["name"] for t, h in hold["holdings"].items()}}, ensure_ascii=False))
    now = dt.datetime.now(KST).isoformat(); log = []
    for c in cmds:
        try:
            if c["type"] == "buy":
                t, p, q = c["ticker"].upper(), float(c["price"]), float(c["qty"])
                h = hold["holdings"].get(t)
                if h:
                    tq = h["qty"] + q; h["avg"] = (h["avg"] * h["qty"] + p * q) / tq; h["qty"] = tq
                else: hold["holdings"][t] = {"name": c.get("name", t), "qty": q, "avg": p, "bought_at": now}
                log.append(f"✅ 매수 기록: {c.get('name', t)} {q:g}주 @ {p:,.2f}")
            elif c["type"] == "sell":
                t = c["ticker"].upper(); h = hold["holdings"].pop(t, None)
                if not h: log.append(f"⚠️ 보유 중이 아닌 종목: {t}"); continue
                pnl = f" (수익률 {(float(c['price'])/h['avg']-1)*100:+.1f}%)" if c.get("price") else ""
                log.append(f"✅ 매도 기록: {h['name']}{pnl}")
            elif c["type"] == "add_sector":
                if c["name"] not in cfg["sectors"]: cfg["sectors"].append(c["name"])
                log.append(f"✅ 섹터 추가: {c['name']}")
            elif c["type"] == "remove_sector":
                if c["name"] in cfg["sectors"]: cfg["sectors"].remove(c["name"])
                log.append(f"✅ 섹터 삭제: {c['name']}")
            elif c["type"] == "unclear":
                log.append(f"❓ 이해 못한 메시지: {c.get('text')} \n 예) 'SK하이닉스 190000원에 10주 샀음' / '하이닉스 팔았음 210000'")
        except Exception as e: log.append(f"⚠️ 처리 실패 {c}: {e}")
    return log

# ---------- 수집 ----------
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
            ct = it.get("content", it); title = ct.get("title")
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

JUDGE_SYS = """너는 신중한 주식 애널리스트다. 제공된 최근 뉴스와 주가 데이터만 근거로 판단한다.
수익을 보장하지 말고, 근거가 부족하면 '관망/보유'로 하고 확신도를 낮춰라. 뉴스에 없는 사실을 지어내지 마라.
JSON만 출력: {"action": "매수|관망|매도|보유", "confidence": 1-5,
"reasons": ["근거1(뉴스 제목 인용)","근거2"], "risks": ["리스크"], "key_news_idx": [근거가 된 뉴스 번호]}
보유 중이 아닌 종목은 매수/관망으로, 보유 중인 종목은 보유/매도로 판단하라."""

def judge(ticker, name, h=None):
    price, news = get_price_info(ticker), get_news(ticker, name)
    ctx = {"ticker": ticker, "name": name, "price": price,
           "my_position": ({"avg_price": h["avg"], "qty": h["qty"],
                            "pnl%": round((price["last"] / h["avg"] - 1) * 100, 2)} if h and price else None),
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

def candidates(sector, n=4):
    return ask_json("너는 한국/미국 주식 전문가다. JSON 배열만 출력.",
        f"'{sector}' 섹터의 대표 상장 종목 {n}개(한국과 미국 혼합). Yahoo Finance 티커 형식(005930.KS, 코스닥 .KQ, NVDA). "
        '[{"ticker":"","name":""}]', 600)

def build_report(cfg, hold):
    out = [f"📊 {dt.datetime.now(KST):%Y-%m-%d} 아침 리포트"]
    hs = hold["holdings"]
    out.append("━━ 보유 종목 (팔지/유지) ━━" if hs else "보유 종목 없음")
    for t, h in hs.items():
        try:
            r = judge(t, h["name"], h); p = r["price"]
            pnl = f"[보유 {(p['last']/h['avg']-1)*100:+.1f}%]" if p else ""
            out.append(fmt(t, h["name"], r, pnl))
        except Exception as e: out.append(f"⚠️ {h['name']} 분석 실패: {e}")
    for s in cfg["sectors"]:
        out.append(f"━━ [{s}] 오늘의 추천 ━━")
        try:
            for cd in candidates(s):
                if cd["ticker"] in hs: continue
                try: out.append(fmt(cd["ticker"], cd["name"], judge(cd["ticker"], cd["name"])))
                except Exception as e: out.append(f"⚠️ {cd['name']} 실패: {e}")
        except Exception as e: out.append(f"⚠️ 섹터 후보 생성 실패: {e}")
    out.append("추천 종목을 샀으면 이 봇에게 '종목명 가격 수량 샀음', 팔았으면 '종목명 팔았음 가격'이라고 보내세요.")
    out.append("※ AI 참고 의견이며 투자 손익의 책임은 본인에게 있습니다.")
    return out

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "report"
    cfg, hold, state = load("config.json", {"sectors": []}), load("holdings.json", {"holdings": {}}), load("state.json", {"offset": 0})
    msgs = fetch_messages(state)
    log = apply_messages(msgs, cfg, hold)
    save("config.json", cfg); save("holdings.json", hold); save("state.json", state)  # 먼저 저장
    if log: send("📝 기록 반영 결과\n" + "\n".join(log))
    if mode == "report": send_blocks(build_report(cfg, hold))

if __name__ == "__main__":
    main()

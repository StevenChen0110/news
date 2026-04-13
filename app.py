# -*- coding: utf-8 -*-
import os
import re
import html as html_lib
import sqlite3
import anthropic
import requests
from urllib.parse import quote

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
import feedparser
from flask import Flask, request, abort, session, redirect, url_for, render_template_string
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from linebot.v3.webhook import WebhookParser
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage,
    PushMessageRequest,
)

app = Flask(__name__)

# ── 環境變數 ──────────────────────────────────────────────────────────────
CHANNEL_SECRET       = os.environ["LINE_CHANNEL_SECRET"]
CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
USER_ID              = os.getenv("LINE_USER_ID")
PUSH_SECRET          = os.getenv("PUSH_SECRET", "change-me")
DB_PATH              = os.getenv("DB_PATH", "subscriptions.db")
ANTHROPIC_API_KEY    = os.getenv("ANTHROPIC_API_KEY")
REURL_API_KEY        = os.getenv("REURL_API_KEY")
ADMIN_PASSWORD       = os.getenv("ADMIN_PASSWORD", "admin123")
app.secret_key       = os.getenv("SECRET_KEY", os.urandom(24))

claude        = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
parser        = WebhookParser(CHANNEL_SECRET)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)

DEFAULT_TOPICS = ["科技", "AI", "台灣", "國際", "財經"]
DEFAULT_TIMES  = ["09:00", "13:00", "22:00"]

# ── SQLite ────────────────────────────────────────────────────────────────
def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS subscriptions (topic TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE IF NOT EXISTS push_times (time TEXT PRIMARY KEY)")
        for t in DEFAULT_TOPICS:
            conn.execute("INSERT OR IGNORE INTO subscriptions VALUES (?)", (t,))
        for t in DEFAULT_TIMES:
            conn.execute("INSERT OR IGNORE INTO push_times VALUES (?)", (t,))

def get_subscriptions() -> list[str]:
    with sqlite3.connect(DB_PATH) as conn:
        return [r[0] for r in conn.execute("SELECT topic FROM subscriptions ORDER BY topic")]

def add_subscription(topic: str) -> bool:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT INTO subscriptions VALUES (?)", (topic,))
        return True
    except sqlite3.IntegrityError:
        return False

def remove_subscription(topic: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("DELETE FROM subscriptions WHERE topic=?", (topic,)).rowcount > 0

def get_push_times() -> list[str]:
    with sqlite3.connect(DB_PATH) as conn:
        return sorted(r[0] for r in conn.execute("SELECT time FROM push_times"))

def add_push_time(t: str) -> bool:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT INTO push_times VALUES (?)", (t,))
        return True
    except sqlite3.IntegrityError:
        return False

def remove_push_time(t: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("DELETE FROM push_times WHERE time=?", (t,)).rowcount > 0

# ── 取得實際文章網址（跟隨 Google News 轉址）────────────────────────────
def resolve_url(url: str) -> str:
    try:
        r = requests.get(url, allow_redirects=True, timeout=5, stream=True)
        r.close()
        return r.url
    except Exception:
        return url

def shorten_url(url: str) -> str:
    if not REURL_API_KEY:
        return url
    try:
        r = requests.post(
            "https://api.reurl.cc/shorten",
            json={"url": url},
            headers={"Content-Type": "application/json", "reurl-api-key": REURL_API_KEY},
            timeout=5,
        )
        data = r.json()
        if data.get("res") == "success":
            return data["short_url"]
    except Exception:
        pass
    return url

def process_link(url: str) -> str:
    """解析 Google News 轉址 → 縮網址"""
    return shorten_url(resolve_url(url))

# ── 綜合摘要（所有文章）────────────────────────────────────────────────
def get_combined_summary(topic: str, titles: list[str]) -> str:
    """用 Claude 綜合摘要多篇新聞重點；無 API Key 回傳空字串"""
    if not claude:
        return ""
    try:
        news_text = "\n".join(f"- {t}" for t in titles)
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{
                "role": "user",
                "content": (
                    f"以下是今日「{topic}」相關新聞標題：\n{news_text}\n\n"
                    f"請用繁體中文寫 2～3 句話綜合說明這些新聞的重點，"
                    f"不要列點，直接寫成一段話。"
                )
            }]
        )
        return resp.content[0].text.strip()
    except Exception as e:
        print(f"[WARN] 摘要生成失敗：{e}")
    return ""

# ── 新聞抓取 ──────────────────────────────────────────────────────────────
def fetch_news(topic: str, count: int = 3) -> dict:
    """回傳 {"summary": str, "items": [{"title": str, "link": str}]}"""
    query = quote(topic)
    url = (
        f"https://news.google.com/rss/search"
        f"?q={query}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    )
    feed = feedparser.parse(url)

    now = datetime.now(timezone.utc)

    def clean_text(raw: str) -> str:
        text = re.sub(r"<[^>]+>", " ", raw)
        text = html_lib.unescape(text)
        return re.sub(r"\s+", " ", text).strip()

    def age_label(pub: datetime | None) -> str:
        if not pub:
            return "時間不明"
        diff = now - pub
        hours = int(diff.total_seconds() / 3600)
        if hours < 1:
            return "不到1小時前"
        if hours < 24:
            return f"{hours}小時前"
        return f"{diff.days}天前"

    def to_dict(e):
        pub = None
        if getattr(e, "published_parsed", None):
            pub = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
        raw_summary = getattr(e, "summary", "") or ""
        summary = clean_text(raw_summary)
        # Google News summary 有時只是標題重複，去掉
        if summary.lower().startswith(e.title[:20].lower()):
            summary = ""
        return {"title": e.title, "link": e.link, "published": pub, "summary": summary}

    all_entries = [to_dict(e) for e in feed.entries[:20]]

    # 優先抓一天內，不足則放寬到一個月
    day_ago   = now - timedelta(days=1)
    month_ago = now - timedelta(days=30)

    candidates = [e for e in all_entries if e["published"] and e["published"] >= day_ago]
    if len(candidates) < count:
        candidates = [e for e in all_entries if e["published"] and e["published"] >= month_ago]
    if not candidates:
        candidates = all_entries  # fallback：無日期資訊就全取

    candidates = candidates[:10]  # 最多 10 篇供 AI 挑選

    if not candidates:
        return {"summary": "", "items": []}

    # AI 重要性排序（DCEM 模型）
    if claude and len(candidates) > count:
        try:
            news_lines = []
            for i, n in enumerate(candidates):
                line = f"{i+1}. [{age_label(n['published'])}] {n['title']}"
                if n.get("summary"):
                    line += f"\n   摘錄：{n['summary'][:120]}"
                news_lines.append(line)
            news_text = "\n".join(news_lines)
            resp = claude.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=16,
                messages=[{
                    "role": "user",
                    "content": (
                        f"你是新聞重要性評估專家。以下是「{topic}」的新聞（含發布時間與摘錄），"
                        f"請依照下列標準選出最重要的 {count} 則：\n\n"
                        f"【評估標準（依優先順序）】\n"
                        f"1. 時效性（最高比重）：越新越優先；超過3天的舊聞需有極高價值才選\n"
                        f"2. 影響規模：涉及範圍廣（跨產業/多國/大量人口）優先\n"
                        f"3. 路徑決定性：結構性轉折、打破慣例、第一塊骨牌優先\n"
                        f"4. 局勢連動：前提在當前仍成立（非過時背景）優先\n"
                        f"5. 前瞻性：政策草案、技術突破等具先兆意義的優先\n"
                        f"6. 避免重複：同一事件只選一則\n\n"
                        f"只回傳編號，用逗號分隔，例如：2,5,7\n\n"
                        f"{news_text}"
                    )
                }]
            )
            picks = [
                int(x.strip()) - 1
                for x in resp.content[0].text.strip().split(",")
                if x.strip().isdigit()
            ]
            candidates = [candidates[i] for i in picks if i < len(candidates)][:count]
        except Exception as e:
            print(f"[WARN] AI 排序失敗：{e}")
            candidates = candidates[:count]
    else:
        candidates = candidates[:count]

    # 平行：解析 Google News 轉址 + 生成摘要
    titles = [c["title"] for c in candidates]
    with ThreadPoolExecutor(max_workers=len(candidates) + 1) as ex:
        f_urls    = [ex.submit(resolve_url, c["link"]) for c in candidates]
        f_summary = ex.submit(get_combined_summary, topic, titles)
        real_urls = [f.result() for f in f_urls]
        summary   = f_summary.result()

    # 依序縮網址（避免同時打 API 觸發速率限制）
    links = [shorten_url(url) for url in real_urls]

    return {
        "summary": summary,
        "items": [{"title": t, "link": l} for t, l in zip(titles, links)],
    }

# ── 格式化 ────────────────────────────────────────────────────────────────
def format_news(topic: str, result: dict) -> str:
    items = result.get("items", [])
    if not items:
        return f"「{topic}」目前找不到相關新聞"
    parts = [f"📰 {topic}"]
    if result.get("summary"):
        parts.append(f"\n{result['summary']}")
    for i, item in enumerate(items, 1):
        parts.append(f"\n{i}. {item['title']}\n🔗 {item['link']}")
    return "\n".join(parts)

# ── Line 推播 ─────────────────────────────────────────────────────────────
def push_to_user(messages: list[str]) -> None:
    if not USER_ID:
        print("[WARN] LINE_USER_ID 未設定")
        return
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        for msg in messages:
            api.push_message(PushMessageRequest(to=USER_ID, messages=[TextMessage(text=msg)]))

def do_scheduled_push() -> None:
    subs = get_subscriptions()
    if not subs:
        return
    push_to_user([format_news(t, fetch_news(t)) for t in subs])
    print(f"[INFO] 推播完成，{len(subs)} 個主題")

# ── APScheduler（動態管理推送時間）───────────────────────────────────────
scheduler = BackgroundScheduler(timezone="Asia/Taipei")

def register_push_jobs() -> None:
    for job in scheduler.get_jobs():
        if job.id.startswith("push_"):
            job.remove()
    for t in get_push_times():
        h, m = t.split(":")
        scheduler.add_job(
            do_scheduled_push,
            CronTrigger(hour=int(h), minute=int(m), timezone="Asia/Taipei"),
            id=f"push_{t}",
            replace_existing=True,
        )

# ── Webhook ───────────────────────────────────────────────────────────────
def _process_event(text: str, user_id: str) -> None:
    """在背景執行緒處理訊息，用 push_message 回傳結果（避免 reply token 超時）"""
    try:
        reply = _handle_command(text, user_id)
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).push_message(
                PushMessageRequest(to=user_id, messages=[TextMessage(text=reply)])
            )
    except Exception as e:
        print(f"[ERROR] 處理訊息失敗：{e}")

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        events = parser.parse(body, signature)
    except InvalidSignatureError:
        abort(400)

    for event in events:
        if not isinstance(event, MessageEvent) or not isinstance(event.message, TextMessageContent):
            continue
        # 立刻在背景處理，讓 webhook 馬上回傳 200
        threading.Thread(
            target=_process_event,
            args=(event.message.text.strip(), event.source.user_id),
            daemon=True,
        ).start()

    return "OK"

def _handle_command(text: str, sender_id: str) -> str:
    if not USER_ID:
        return f"你的 User ID 是：\n{sender_id}\n\n請設定環境變數 LINE_USER_ID 後重新部署。"

    # ── 訂閱管理 ──
    if text.startswith("訂閱 "):
        topic = text[3:].strip()
        return (f"✅ 已訂閱「{topic}」" if add_subscription(topic) else f"「{topic}」已在訂閱清單") if topic else "請輸入主題，例如：訂閱 科技"

    if text.startswith("取消訂閱 "):
        topic = text[5:].strip()
        return (f"✅ 已取消訂閱「{topic}」" if remove_subscription(topic) else f"「{topic}」不在訂閱清單") if topic else "請輸入主題"

    if text == "我的訂閱":
        subs = get_subscriptions()
        return ("📋 訂閱主題：\n" + "\n".join(f"• {t}" for t in subs)) if subs else "目前沒有訂閱主題"

    # ── 推送時間管理 ──
    if text == "推送時間":
        times = get_push_times()
        return ("⏰ 推送時間：\n" + "\n".join(f"• {t}" for t in times)) if times else "目前沒有設定推送時間"

    if text.startswith("新增推送時間 "):
        t = text[7:].strip()
        if not re.match(r"^\d{2}:\d{2}$", t):
            return "格式錯誤，請輸入 HH:MM\n例如：新增推送時間 08:00"
        if add_push_time(t):
            register_push_jobs()
            return f"✅ 已新增推送時間 {t}"
        return f"⏰ {t} 已在推送清單"

    if text.startswith("刪除推送時間 "):
        t = text[7:].strip()
        if remove_push_time(t):
            register_push_jobs()
            return f"✅ 已刪除推送時間 {t}"
        return f"❌ {t} 不在推送清單"

    # ── 說明 ──
    if text in ("說明", "help", "?", "？"):
        return (
            "📖 指令說明\n\n"
            "【即時查詢】\n"
            "直接輸入任何關鍵字\n"
            "→ 搜尋最重要 3 則新聞\n\n"
            "【訂閱管理】\n"
            "訂閱 <主題>\n"
            "取消訂閱 <主題>\n"
            "我的訂閱\n\n"
            "【推送時間管理】\n"
            "推送時間\n"
            "新增推送時間 HH:MM\n"
            "刪除推送時間 HH:MM"
        )

    # 預設：即時查詢
    return format_news(text, fetch_news(text))

# ── 備用推播 endpoint ─────────────────────────────────────────────────────
@app.route("/trigger-push", methods=["POST"])
def trigger_push():
    if request.headers.get("Authorization") != f"Bearer {PUSH_SECRET}":
        abort(401)
    do_scheduled_push()
    return "ok", 200

# ── Keep-alive（給 UptimeRobot ping 用）──────────────────────────────────
@app.route("/healthz")
def healthz():
    return "ok", 200

# ── 管理介面 ──────────────────────────────────────────────────────────────
ADMIN_HTML = """
<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>新聞 Bot 管理</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, sans-serif; background: #f5f5f5; color: #333; }
  .container { max-width: 600px; margin: 0 auto; padding: 20px; }
  h1 { font-size: 20px; margin-bottom: 24px; color: #111; }
  .card { background: #fff; border-radius: 12px; padding: 20px; margin-bottom: 20px;
          box-shadow: 0 1px 4px rgba(0,0,0,.08); }
  .card h2 { font-size: 15px; color: #666; margin-bottom: 14px; text-transform: uppercase;
              letter-spacing: .5px; }
  .tag-list { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 14px; }
  .tag { display: flex; align-items: center; gap: 6px; background: #f0f0f0;
         border-radius: 20px; padding: 6px 12px; font-size: 14px; }
  .tag button { background: none; border: none; color: #999; cursor: pointer;
                font-size: 16px; line-height: 1; padding: 0; }
  .tag button:hover { color: #e00; }
  .add-row { display: flex; gap: 8px; }
  .add-row input { flex: 1; border: 1px solid #ddd; border-radius: 8px;
                   padding: 10px 14px; font-size: 15px; outline: none; }
  .add-row input:focus { border-color: #06c755; }
  .add-row button { background: #06c755; color: #fff; border: none; border-radius: 8px;
                    padding: 10px 18px; font-size: 15px; cursor: pointer; font-weight: 600; }
  .add-row button:hover { background: #05b04c; }
  .empty { color: #aaa; font-size: 14px; margin-bottom: 14px; }
  .logout { font-size: 13px; color: #999; text-decoration: none; float: right; margin-top: 4px; }
  .logout:hover { color: #e00; }
</style>
</head>
<body>
<div class="container">
  <h1>📰 新聞 Bot 管理 <a href="/admin/logout" class="logout">登出</a></h1>

  <!-- 訂閱主題 -->
  <div class="card">
    <h2>訂閱主題</h2>
    <div class="tag-list">
      {% if subs %}
        {% for t in subs %}
        <span class="tag">
          {{ t }}
          <form method="post" action="/admin/subs/remove" style="display:inline">
            <input type="hidden" name="topic" value="{{ t }}">
            <button type="submit" title="刪除">×</button>
          </form>
        </span>
        {% endfor %}
      {% else %}
        <p class="empty">目前沒有訂閱主題</p>
      {% endif %}
    </div>
    <form method="post" action="/admin/subs/add" class="add-row">
      <input name="topic" placeholder="輸入新主題，例如：體育" required>
      <button type="submit">新增</button>
    </form>
  </div>

  <!-- 推送時間 -->
  <div class="card">
    <h2>推送時間</h2>
    <div class="tag-list">
      {% if times %}
        {% for t in times %}
        <span class="tag">
          ⏰ {{ t }}
          <form method="post" action="/admin/times/remove" style="display:inline">
            <input type="hidden" name="time" value="{{ t }}">
            <button type="submit" title="刪除">×</button>
          </form>
        </span>
        {% endfor %}
      {% else %}
        <p class="empty">目前沒有推送時間</p>
      {% endif %}
    </div>
    <form method="post" action="/admin/times/add" class="add-row">
      <input name="time" placeholder="HH:MM，例如：08:00" pattern="\\d{2}:\\d{2}" required>
      <button type="submit">新增</button>
    </form>
  </div>
</div>
</body>
</html>
"""

LOGIN_HTML = """
<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>登入</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, sans-serif; background: #f5f5f5;
         display: flex; align-items: center; justify-content: center; min-height: 100vh; }
  .card { background: #fff; border-radius: 12px; padding: 32px; width: 320px;
          box-shadow: 0 1px 4px rgba(0,0,0,.1); }
  h1 { font-size: 18px; margin-bottom: 20px; text-align: center; }
  input { width: 100%; border: 1px solid #ddd; border-radius: 8px;
          padding: 12px 14px; font-size: 15px; margin-bottom: 12px; outline: none; }
  input:focus { border-color: #06c755; }
  button { width: 100%; background: #06c755; color: #fff; border: none;
           border-radius: 8px; padding: 12px; font-size: 16px;
           font-weight: 600; cursor: pointer; }
  button:hover { background: #05b04c; }
  .error { color: #e00; font-size: 13px; margin-bottom: 10px; }
</style>
</head>
<body>
<div class="card">
  <h1>📰 新聞 Bot 管理</h1>
  {% if error %}<p class="error">{{ error }}</p>{% endif %}
  <form method="post">
    <input type="password" name="password" placeholder="請輸入密碼" autofocus>
    <button type="submit">登入</button>
  </form>
</div>
</body>
</html>
"""

def admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated

@app.route("/admin", methods=["GET"])
@admin_required
def admin_index():
    return render_template_string(
        ADMIN_HTML,
        subs=get_subscriptions(),
        times=get_push_times(),
    )

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(url_for("admin_index"))
        error = "密碼錯誤"
    return render_template_string(LOGIN_HTML, error=error)

@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))

@app.route("/admin/subs/add", methods=["POST"])
@admin_required
def admin_subs_add():
    topic = request.form.get("topic", "").strip()
    if topic:
        add_subscription(topic)
    return redirect(url_for("admin_index"))

@app.route("/admin/subs/remove", methods=["POST"])
@admin_required
def admin_subs_remove():
    topic = request.form.get("topic", "").strip()
    if topic:
        remove_subscription(topic)
    return redirect(url_for("admin_index"))

@app.route("/admin/times/add", methods=["POST"])
@admin_required
def admin_times_add():
    t = request.form.get("time", "").strip()
    if re.match(r"^\d{2}:\d{2}$", t):
        if add_push_time(t):
            register_push_jobs()
    return redirect(url_for("admin_index"))

@app.route("/admin/times/remove", methods=["POST"])
@admin_required
def admin_times_remove():
    t = request.form.get("time", "").strip()
    if t:
        remove_push_time(t)
        register_push_jobs()
    return redirect(url_for("admin_index"))

# ── 啟動 ──────────────────────────────────────────────────────────────────
init_db()
register_push_jobs()
scheduler.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)

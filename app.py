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
    PushMessageRequest, QuickReply, QuickReplyItem, MessageAction,
)

app = Flask(__name__)

# ── 環境變數 ──────────────────────────────────────────────────────────────
CHANNEL_SECRET       = os.environ["LINE_CHANNEL_SECRET"]
CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
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

# ── 新聞快取（15 分鐘 TTL）───────────────────────────────────────────────
_news_cache: dict[str, tuple[datetime, dict]] = {}
CACHE_TTL = timedelta(minutes=15)

def get_cached(topic: str) -> dict | None:
    entry = _news_cache.get(topic)
    if entry:
        cached_at, result = entry
        if datetime.now(timezone.utc) - cached_at < CACHE_TTL:
            return result
        del _news_cache[topic]
    return None

def set_cached(topic: str, result: dict) -> None:
    _news_cache[topic] = (datetime.now(timezone.utc), result)

# ── SQLite ────────────────────────────────────────────────────────────────
def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        # 用戶表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        # 每人訂閱
        conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id TEXT NOT NULL,
                topic   TEXT NOT NULL,
                PRIMARY KEY (user_id, topic)
            )
        """)
        # 每人推送時間
        conn.execute("""
            CREATE TABLE IF NOT EXISTS push_times (
                user_id TEXT NOT NULL,
                time    TEXT NOT NULL,
                PRIMARY KEY (user_id, time)
            )
        """)

def register_user(user_id: str) -> bool:
    """首次出現的用戶：自動設定預設訂閱與推送時間。回傳 True 表示新用戶。"""
    with sqlite3.connect(DB_PATH) as conn:
        if conn.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone():
            return False
        conn.execute("INSERT INTO users VALUES (?, datetime('now'))", (user_id,))
        for t in DEFAULT_TOPICS:
            conn.execute("INSERT OR IGNORE INTO subscriptions VALUES (?,?)", (user_id, t))
        for t in DEFAULT_TIMES:
            conn.execute("INSERT OR IGNORE INTO push_times VALUES (?,?)", (user_id, t))
    return True

def get_all_users() -> list[str]:
    with sqlite3.connect(DB_PATH) as conn:
        return [r[0] for r in conn.execute("SELECT user_id FROM users ORDER BY created_at")]

def get_subscriptions(user_id: str) -> list[str]:
    with sqlite3.connect(DB_PATH) as conn:
        return [r[0] for r in conn.execute(
            "SELECT topic FROM subscriptions WHERE user_id=? ORDER BY topic", (user_id,)
        )]

def get_all_subscriptions() -> list[str]:
    """取得所有用戶的不重複主題（供預抓快取用）"""
    with sqlite3.connect(DB_PATH) as conn:
        return [r[0] for r in conn.execute("SELECT DISTINCT topic FROM subscriptions")]

def add_subscription(user_id: str, topic: str) -> bool:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT INTO subscriptions VALUES (?,?)", (user_id, topic))
        return True
    except sqlite3.IntegrityError:
        return False

def remove_subscription(user_id: str, topic: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            "DELETE FROM subscriptions WHERE user_id=? AND topic=?", (user_id, topic)
        ).rowcount > 0

def get_push_times(user_id: str) -> list[str]:
    with sqlite3.connect(DB_PATH) as conn:
        return sorted(r[0] for r in conn.execute(
            "SELECT time FROM push_times WHERE user_id=?", (user_id,)
        ))

def get_all_push_times() -> list[str]:
    """取得所有用戶的不重複推送時間（供排程器用）"""
    with sqlite3.connect(DB_PATH) as conn:
        return sorted(set(r[0] for r in conn.execute("SELECT DISTINCT time FROM push_times")))

def get_users_for_time(time_str: str) -> list[str]:
    """取得設定了某個推送時間的所有用戶"""
    with sqlite3.connect(DB_PATH) as conn:
        return [r[0] for r in conn.execute(
            "SELECT user_id FROM push_times WHERE time=?", (time_str,)
        )]

def add_push_time(user_id: str, t: str) -> bool:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT INTO push_times VALUES (?,?)", (user_id, t))
        return True
    except sqlite3.IntegrityError:
        return False

def remove_push_time(user_id: str, t: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            "DELETE FROM push_times WHERE user_id=? AND time=?", (user_id, t)
        ).rowcount > 0

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

# ── 新聞抓取 ──────────────────────────────────────────────────────────────
def fetch_news(topic: str, count: int = 3) -> dict:
    """回傳 {"summary": str, "items": [{"title": str, "link": str}]}，有快取時直接回傳"""
    cached = get_cached(topic)
    if cached is not None:
        return cached
    result = _fetch_fresh(topic, count)
    set_cached(topic, result)
    return result

def _fetch_fresh(topic: str, count: int = 3) -> dict:
    """從網路抓取最新新聞（繞過快取）"""
    query = quote(topic)
    url = (
        f"https://news.google.com/rss/search"
        f"?q={query}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    )
    feed = feedparser.parse(url)
    now  = datetime.now(timezone.utc)

    def clean_text(raw: str) -> str:
        text = re.sub(r"<[^>]+>", " ", raw)
        text = html_lib.unescape(text)
        return re.sub(r"\s+", " ", text).strip()

    def age_label(pub: datetime | None) -> str:
        if not pub:
            return "時間不明"
        diff  = now - pub
        hours = int(diff.total_seconds() / 3600)
        if hours < 1:  return "不到1小時前"
        if hours < 24: return f"{hours}小時前"
        return f"{diff.days}天前"

    def to_dict(e):
        pub = None
        if getattr(e, "published_parsed", None):
            pub = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
        raw = getattr(e, "summary", "") or ""
        snippet = clean_text(raw)
        if snippet.lower().startswith(e.title[:20].lower()):
            snippet = ""
        return {"title": e.title, "link": e.link, "published": pub, "snippet": snippet}

    all_entries = [to_dict(e) for e in feed.entries[:30]]

    # 優先一天內，不足放寬到一個月
    day_ago   = now - timedelta(days=1)
    month_ago = now - timedelta(days=30)
    candidates = [e for e in all_entries if e["published"] and e["published"] >= day_ago]
    if len(candidates) < count:
        candidates = [e for e in all_entries if e["published"] and e["published"] >= month_ago]
    if not candidates:
        candidates = all_entries
    candidates = candidates[:20]  # 最多 20 篇供 AI 挑選

    if not candidates:
        return {"summary": "", "items": []}

    # ── 單次 AI 呼叫：排序 + 摘要同時完成 ──────────────────────────────
    selected = candidates[:count]
    summary  = ""
    if claude and len(candidates) > count:
        try:
            news_lines = []
            for i, n in enumerate(candidates):
                line = f"{i+1}. [{age_label(n['published'])}] {n['title']}"
                if n.get("snippet"):
                    line += f"\n   摘錄：{n['snippet'][:120]}"
                news_lines.append(line)
            news_text = "\n".join(news_lines)
            resp = claude.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=220,
                messages=[{
                    "role": "user",
                    "content": (
                        f"你是新聞重要性評估專家。以下是「{topic}」的新聞（含時間與摘錄）。\n\n"
                        f"【評估標準（依優先順序）】\n"
                        f"1. 時效性（最高比重 35%）：越新越優先；超過3天須有極高價值才選\n"
                        f"2. 影響規模（20%）：涉及範圍廣（跨產業/多國/大量人口）\n"
                        f"3. 路徑決定性（20%）：結構性轉折、打破慣例、第一塊骨牌\n"
                        f"4. 媒體共識（15%）：同一事件出現多篇不同來源報導\n"
                        f"5. 事實密度（10%）：有具體數字/數據，而非情緒評論\n"
                        f"6. 避免重複：同一事件只選一則\n\n"
                        f"請完成兩件事，嚴格按以下格式回覆：\n"
                        f"RANKS:編號,編號,編號\n"
                        f"SUMMARY:2-3句繁體中文綜合重點\n\n"
                        f"{news_text}"
                    )
                }]
            )
            raw = resp.content[0].text.strip()
            ranks_m   = re.search(r"RANKS:\s*([0-9,\s]+)", raw)
            summary_m = re.search(r"SUMMARY:\s*(.+)", raw, re.DOTALL)
            if ranks_m:
                picks = [int(x.strip()) - 1 for x in ranks_m.group(1).split(",") if x.strip().isdigit()]
                selected = [candidates[i] for i in picks if i < len(candidates)][:count]
            if summary_m:
                summary = summary_m.group(1).strip()
        except Exception as e:
            print(f"[WARN] AI 分析失敗：{e}")

    # 平行解析 URL，依序縮網址
    with ThreadPoolExecutor(max_workers=len(selected)) as ex:
        real_urls = list(ex.map(resolve_url, [c["link"] for c in selected]))
    links = [shorten_url(u) for u in real_urls]

    return {
        "summary": summary,
        "items": [{"title": c["title"], "link": l} for c, l in zip(selected, links)],
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

def format_news_message(topic: str, result: dict) -> TextMessage:
    text = format_news(topic, result)
    qr = _qr((f"📌 訂閱「{topic[:8]}」", f"訂閱 {topic}"), ("📋 我的訂閱", "我的訂閱"))
    return _msg(text, qr)

# ── Quick Reply 常用組合 ──────────────────────────────────────────────────
def _qr(*labels_and_texts: tuple[str, str]) -> QuickReply:
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label=label, text=text))
        for label, text in labels_and_texts
    ])

QR_MAIN     = _qr(("📋 我的訂閱", "我的訂閱"), ("⏰ 推送時間", "推送時間"), ("❓ 說明", "說明"))
QR_SUBS     = _qr(("⏰ 推送時間", "推送時間"), ("❓ 說明", "說明"))
QR_TIMES    = _qr(("📋 我的訂閱", "我的訂閱"), ("❓ 說明", "說明"))
QR_AFTER_SUB = _qr(("📋 我的訂閱", "我的訂閱"), ("⏰ 推送時間", "推送時間"))

def _msg(text: str, quick_reply: QuickReply | None = None) -> TextMessage:
    return TextMessage(text=text, quick_reply=quick_reply)

# ── LINE 推播 ─────────────────────────────────────────────────────────────
def _send(user_id: str, msg: str | TextMessage) -> None:
    if isinstance(msg, str):
        msg = TextMessage(text=msg)
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).push_message(
            PushMessageRequest(to=user_id, messages=[msg])
        )

def do_scheduled_push(time_str: str) -> None:
    """推送給所有設定了此時間的用戶（各自的訂閱主題）"""
    users = get_users_for_time(time_str)
    for user_id in users:
        subs = get_subscriptions(user_id)
        if not subs:
            continue
        for topic in subs:
            try:
                _send(user_id, format_news_message(topic, fetch_news(topic)))
            except Exception as e:
                print(f"[WARN] 推播失敗 {user_id}/{topic}：{e}")
    print(f"[INFO] {time_str} 推播完成，{len(users)} 位用戶")

# ── APScheduler（動態管理推送時間）───────────────────────────────────────
scheduler = BackgroundScheduler(timezone="Asia/Taipei")

def prefetch_subscriptions() -> None:
    """每 15 分鐘預抓所有用戶訂閱的主題，存入快取"""
    for topic in get_all_subscriptions():
        try:
            result = _fetch_fresh(topic)
            set_cached(topic, result)
            print(f"[INFO] 預抓完成：{topic}")
        except Exception as e:
            print(f"[WARN] 預抓失敗 {topic}：{e}")

def register_push_jobs() -> None:
    for job in scheduler.get_jobs():
        if job.id.startswith("push_"):
            job.remove()
    for t in get_all_push_times():
        h, m = t.split(":")
        scheduler.add_job(
            do_scheduled_push,
            CronTrigger(hour=int(h), minute=int(m), timezone="Asia/Taipei"),
            id=f"push_{t}",
            replace_existing=True,
            args=[t],
        )
    # 每 15 分鐘預抓訂閱主題
    scheduler.add_job(
        prefetch_subscriptions,
        CronTrigger(minute="*/15"),
        id="prefetch",
        replace_existing=True,
    )

# ── Webhook ───────────────────────────────────────────────────────────────
_COMMANDS = ("訂閱", "取消訂閱", "我的訂閱", "推送時間", "新增推送時間", "刪除推送時間", "說明", "help", "?", "？")

def _process_event(text: str, user_id: str) -> None:
    """在背景執行緒處理訊息，用 push_message 回傳結果（避免 reply token 超時）"""
    try:
        # 新用戶自動初始化
        is_new = register_user(user_id)
        if is_new:
            register_push_jobs()  # 有新用戶時更新排程
            _send(user_id, _msg(
                "👋 歡迎使用新聞 Bot！\n\n"
                f"已為你設定預設訂閱：{', '.join(DEFAULT_TOPICS)}\n"
                f"推送時間：{', '.join(DEFAULT_TIMES)}\n\n"
                "直接輸入關鍵字可即時查詢新聞\n"
                "點下方按鈕管理你的設定 👇",
                QR_MAIN
            ))
            return

        is_command = any(text.startswith(c) for c in _COMMANDS)
        # 快取 miss 且非指令 → 先送確認訊息
        if not is_command and get_cached(text) is None:
            _send(user_id, f"🔍 正在分析「{text}」的最新新聞，請稍候...")
        reply = _handle_command(text, user_id)
        _send(user_id, reply)
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

def _handle_command(text: str, user_id: str) -> TextMessage:
    # ── 訂閱管理 ──
    if text.startswith("訂閱 "):
        topic = text[3:].strip()
        if not topic:
            return _msg("請輸入主題，例如：訂閱 科技")
        if add_subscription(user_id, topic):
            return _msg(f"✅ 已訂閱「{topic}」\n\n之後每次推播都會包含此主題", QR_AFTER_SUB)
        return _msg(f"「{topic}」已在訂閱清單", QR_AFTER_SUB)

    if text.startswith("取消訂閱 "):
        topic = text[5:].strip()
        if not topic:
            return _msg("請輸入主題")
        if remove_subscription(user_id, topic):
            return _msg(f"✅ 已取消訂閱「{topic}」", _qr(("📋 我的訂閱", "我的訂閱")))
        return _msg(f"「{topic}」不在訂閱清單", _qr(("📋 我的訂閱", "我的訂閱")))

    if text == "我的訂閱":
        subs = get_subscriptions(user_id)
        if subs:
            body = "📋 訂閱主題：\n" + "\n".join(f"• {t}" for t in subs)
            body += "\n\n要取消請輸入：取消訂閱 <主題>"
        else:
            body = "目前沒有訂閱主題\n\n輸入「訂閱 <主題>」來新增"
        return _msg(body, QR_SUBS)

    # ── 推送時間管理 ──
    if text == "推送時間":
        times = get_push_times(user_id)
        if times:
            body = "⏰ 推送時間：\n" + "\n".join(f"• {t}" for t in times)
            body += "\n\n新增：新增推送時間 HH:MM\n刪除：刪除推送時間 HH:MM"
        else:
            body = "目前沒有設定推送時間\n\n輸入「新增推送時間 HH:MM」來新增"
        return _msg(body, QR_TIMES)

    if text.startswith("新增推送時間 "):
        t = text[7:].strip()
        if not re.match(r"^\d{2}:\d{2}$", t):
            return _msg("格式錯誤，請輸入 HH:MM\n例如：新增推送時間 08:00")
        if add_push_time(user_id, t):
            register_push_jobs()
            return _msg(f"✅ 已新增推送時間 {t}", QR_TIMES)
        return _msg(f"⏰ {t} 已在推送清單", QR_TIMES)

    if text.startswith("刪除推送時間 "):
        t = text[7:].strip()
        if remove_push_time(user_id, t):
            register_push_jobs()
            return _msg(f"✅ 已刪除推送時間 {t}", QR_TIMES)
        return _msg(f"❌ {t} 不在推送清單", QR_TIMES)

    # ── 說明 ──
    if text in ("說明", "help", "?", "？"):
        return _msg(
            "📖 使用說明\n\n"
            "【即時查詢】\n"
            "直接輸入任何關鍵字\n"
            "例：台積電、升息、世界盃\n\n"
            "【訂閱管理】\n"
            "訂閱 <主題>　　新增定時推播\n"
            "取消訂閱 <主題>　移除\n"
            "我的訂閱　　　查看清單\n\n"
            "【推送時間】\n"
            "推送時間　　　　查看設定\n"
            "新增推送時間 HH:MM\n"
            "刪除推送時間 HH:MM",
            QR_MAIN
        )

    # 預設：即時查詢
    return format_news_message(text, fetch_news(text))

# ── 備用推播 endpoint ─────────────────────────────────────────────────────
@app.route("/trigger-push", methods=["POST"])
def trigger_push():
    if request.headers.get("Authorization") != f"Bearer {PUSH_SECRET}":
        abort(401)
    # 推送所有用戶的所有主題
    for user_id in get_all_users():
        subs = get_subscriptions(user_id)
        for topic in subs:
            try:
                _send(user_id, format_news_message(topic, fetch_news(topic)))
            except Exception as e:
                print(f"[WARN] trigger-push 失敗 {user_id}/{topic}：{e}")
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
  .container { max-width: 700px; margin: 0 auto; padding: 20px; }
  h1 { font-size: 20px; margin-bottom: 24px; color: #111; }
  .card { background: #fff; border-radius: 12px; padding: 20px; margin-bottom: 20px;
          box-shadow: 0 1px 4px rgba(0,0,0,.08); }
  .card h2 { font-size: 15px; color: #666; margin-bottom: 14px; text-transform: uppercase;
              letter-spacing: .5px; }
  .user-row { display: flex; align-items: center; gap: 10px; padding: 10px 0;
              border-bottom: 1px solid #f0f0f0; }
  .user-row:last-child { border-bottom: none; }
  .user-id { font-family: monospace; font-size: 13px; color: #555; flex: 1; word-break: break-all; }
  .badge { background: #06c755; color: #fff; border-radius: 20px; padding: 2px 10px;
           font-size: 12px; white-space: nowrap; }
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
  .section-title { font-size: 13px; color: #888; margin: 16px 0 8px; font-weight: 600; }
  select { border: 1px solid #ddd; border-radius: 8px; padding: 10px 14px;
           font-size: 14px; background: #fff; cursor: pointer; }
</style>
</head>
<body>
<div class="container">
  <h1>📰 新聞 Bot 管理 <a href="/admin/logout" class="logout">登出</a></h1>

  <!-- 用戶列表 -->
  <div class="card">
    <h2>用戶列表（{{ users|length }} 人）</h2>
    {% if users %}
      {% for u in users %}
      <div class="user-row">
        <span class="user-id">{{ u.user_id }}</span>
        <span class="badge">{{ u.sub_count }} 訂閱</span>
        <a href="/admin/user/{{ u.user_id }}" style="font-size:13px; color:#06c755; text-decoration:none;">管理 →</a>
      </div>
      {% endfor %}
    {% else %}
      <p class="empty">目前沒有用戶（等待第一個人使用 Bot）</p>
    {% endif %}
  </div>
</div>
</body>
</html>
"""

USER_ADMIN_HTML = """
<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>管理用戶</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, sans-serif; background: #f5f5f5; color: #333; }
  .container { max-width: 600px; margin: 0 auto; padding: 20px; }
  h1 { font-size: 18px; margin-bottom: 6px; color: #111; }
  .uid { font-family: monospace; font-size: 12px; color: #888; margin-bottom: 20px; word-break: break-all; }
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
  .back { font-size: 13px; color: #06c755; text-decoration: none; }
</style>
</head>
<body>
<div class="container">
  <p style="margin-bottom:12px"><a href="/admin" class="back">← 返回用戶列表</a></p>
  <h1>用戶管理</h1>
  <p class="uid">{{ user_id }}</p>

  <!-- 訂閱主題 -->
  <div class="card">
    <h2>訂閱主題</h2>
    <div class="tag-list">
      {% if subs %}
        {% for t in subs %}
        <span class="tag">
          {{ t }}
          <form method="post" action="/admin/user/{{ user_id }}/subs/remove" style="display:inline">
            <input type="hidden" name="topic" value="{{ t }}">
            <button type="submit" title="刪除">×</button>
          </form>
        </span>
        {% endfor %}
      {% else %}
        <p class="empty">目前沒有訂閱主題</p>
      {% endif %}
    </div>
    <form method="post" action="/admin/user/{{ user_id }}/subs/add" class="add-row">
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
          <form method="post" action="/admin/user/{{ user_id }}/times/remove" style="display:inline">
            <input type="hidden" name="time" value="{{ t }}">
            <button type="submit" title="刪除">×</button>
          </form>
        </span>
        {% endfor %}
      {% else %}
        <p class="empty">目前沒有推送時間</p>
      {% endif %}
    </div>
    <form method="post" action="/admin/user/{{ user_id }}/times/add" class="add-row">
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
    all_users = get_all_users()
    users_data = []
    for uid in all_users:
        users_data.append({
            "user_id": uid,
            "sub_count": len(get_subscriptions(uid)),
        })
    return render_template_string(ADMIN_HTML, users=users_data)

@app.route("/admin/user/<user_id>", methods=["GET"])
@admin_required
def admin_user(user_id: str):
    return render_template_string(
        USER_ADMIN_HTML,
        user_id=user_id,
        subs=get_subscriptions(user_id),
        times=get_push_times(user_id),
    )

@app.route("/admin/user/<user_id>/subs/add", methods=["POST"])
@admin_required
def admin_user_subs_add(user_id: str):
    topic = request.form.get("topic", "").strip()
    if topic:
        add_subscription(user_id, topic)
    return redirect(url_for("admin_user", user_id=user_id))

@app.route("/admin/user/<user_id>/subs/remove", methods=["POST"])
@admin_required
def admin_user_subs_remove(user_id: str):
    topic = request.form.get("topic", "").strip()
    if topic:
        remove_subscription(user_id, topic)
    return redirect(url_for("admin_user", user_id=user_id))

@app.route("/admin/user/<user_id>/times/add", methods=["POST"])
@admin_required
def admin_user_times_add(user_id: str):
    t = request.form.get("time", "").strip()
    if re.match(r"^\d{2}:\d{2}$", t):
        if add_push_time(user_id, t):
            register_push_jobs()
    return redirect(url_for("admin_user", user_id=user_id))

@app.route("/admin/user/<user_id>/times/remove", methods=["POST"])
@admin_required
def admin_user_times_remove(user_id: str):
    t = request.form.get("time", "").strip()
    if t:
        remove_push_time(user_id, t)
        register_push_jobs()
    return redirect(url_for("admin_user", user_id=user_id))

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

# ── 啟動 ──────────────────────────────────────────────────────────────────
init_db()
register_push_jobs()
scheduler.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)

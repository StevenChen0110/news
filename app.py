# -*- coding: utf-8 -*-
import os
import re
import sqlite3
import anthropic
import requests
from urllib.parse import quote

import feedparser
from flask import Flask, request, abort
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

# ── URL 縮網址（is.gd 免費 API）──────────────────────────────────────────
def shorten_url(url: str) -> str:
    try:
        r = requests.get(
            "https://is.gd/create.php",
            params={"format": "simple", "url": url},
            timeout=5,
        )
        if r.status_code == 200 and r.text.startswith("https://is.gd/"):
            return r.text.strip()
    except Exception:
        pass
    return url  # fallback

# ── 新聞摘要 ──────────────────────────────────────────────────────────────
def get_summary(title: str) -> str:
    """約 20 字摘要：有 Claude 就用 AI，否則截取標題"""
    if claude:
        try:
            resp = claude.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{
                    "role": "user",
                    "content": (
                        f"根據以下新聞標題，用繁體中文寫一句約 20 字的重點摘要"
                        f"（不要重複標題原文，直接給摘要即可）：\n{title}"
                    )
                }]
            )
            return resp.content[0].text.strip()
        except Exception as e:
            print(f"[WARN] 摘要生成失敗：{e}")
    return title[:22] + ("…" if len(title) > 22 else "")

# ── 新聞抓取 ──────────────────────────────────────────────────────────────
def fetch_news(topic: str, count: int = 3) -> list[dict]:
    query = quote(topic)
    url = (
        f"https://news.google.com/rss/search"
        f"?q={query}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    )
    feed = feedparser.parse(url)
    candidates = [{"title": e.title, "link": e.link} for e in feed.entries[:10]]

    if not candidates:
        return []

    # AI 重要性排序
    if claude and len(candidates) > count:
        try:
            news_text = "\n".join(f"{i+1}. {n['title']}" for i, n in enumerate(candidates))
            resp = claude.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=32,
                messages=[{
                    "role": "user",
                    "content": (
                        f"以下是「{topic}」的新聞標題，選出最重要的 {count} 則，"
                        f"只回傳編號用逗號分隔，例如：2,5,7\n\n{news_text}"
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

    # 加上摘要與縮網址
    return [
        {
            "title": item["title"],
            "summary": get_summary(item["title"]),
            "link": shorten_url(item["link"]),
        }
        for item in candidates
    ]

# ── 格式化 ────────────────────────────────────────────────────────────────
def format_news(topic: str, items: list[dict]) -> str:
    if not items:
        return f"「{topic}」目前找不到相關新聞"
    parts = [f"📰 {topic}"]
    for i, item in enumerate(items, 1):
        parts.append(f"\n{i}. {item['summary']}\n🔗 {item['link']}")
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
        reply = _handle_command(event.message.text.strip(), event.source.user_id)
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply)],
                )
            )
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

# ── 啟動 ──────────────────────────────────────────────────────────────────
init_db()
register_push_jobs()
scheduler.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)

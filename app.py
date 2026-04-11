# -*- coding: utf-8 -*-
import os
import sqlite3
import anthropic
from urllib.parse import quote

import feedparser
from flask import Flask, request, abort

from linebot.v3.webhook import WebhookParser          # v3 正確路徑
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
USER_ID              = os.getenv("LINE_USER_ID")   # 首次部署可留空
PUSH_SECRET          = os.getenv("PUSH_SECRET", "change-me")  # 保護排程 endpoint
DB_PATH              = os.getenv("DB_PATH", "subscriptions.db")
ANTHROPIC_API_KEY    = os.getenv("ANTHROPIC_API_KEY")

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

parser        = WebhookParser(CHANNEL_SECRET)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)

# ── 預設訂閱主題 ─────────────────────────────────────────────────────────
DEFAULT_TOPICS = ["科技", "AI", "台灣", "國際", "財經"]

# ── SQLite 工具 ───────────────────────────────────────────────────────────
def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS subscriptions (topic TEXT PRIMARY KEY)"
        )
        for topic in DEFAULT_TOPICS:
            conn.execute(
                "INSERT OR IGNORE INTO subscriptions VALUES (?)", (topic,)
            )

def get_subscriptions() -> list[str]:
    with sqlite3.connect(DB_PATH) as conn:
        return [row[0] for row in conn.execute("SELECT topic FROM subscriptions ORDER BY topic")]

def add_subscription(topic: str) -> bool:
    """回傳 True 表示新增成功，False 表示已存在"""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT INTO subscriptions VALUES (?)", (topic,))
        return True
    except sqlite3.IntegrityError:
        return False

def remove_subscription(topic: str) -> bool:
    """回傳 True 表示刪除成功，False 表示不存在"""
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "DELETE FROM subscriptions WHERE topic = ?", (topic,)
        )
        return cur.rowcount > 0

# ── 新聞抓取 ──────────────────────────────────────────────────────────────
def fetch_news(topic: str, count: int = 3) -> list[dict]:
    """抓取新聞：有 Claude API 則用 AI 篩選最重要的，否則直接回傳 RSS 前幾則"""
    query = quote(topic)
    url = (
        f"https://news.google.com/rss/search"
        f"?q={query}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    )
    feed = feedparser.parse(url)
    candidates = [
        {"title": e.title, "link": e.link}
        for e in feed.entries[:10]   # 多抓幾則，給 AI 挑選
    ]

    if not candidates:
        return []

    # 有 Claude API：讓 AI 從候選中選出最重要的 3 則
    if claude and len(candidates) > count:
        try:
            news_text = "\n".join(
                f"{i+1}. {n['title']}" for i, n in enumerate(candidates)
            )
            resp = claude.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{
                    "role": "user",
                    "content": (
                        f"以下是關於「{topic}」的新聞標題列表，"
                        f"請選出最重要、最值得關注的 {count} 則，"
                        f"只回傳編號，用逗號分隔，例如：2,5,7\n\n{news_text}"
                    )
                }]
            )
            picks = [
                int(x.strip()) - 1
                for x in resp.content[0].text.strip().split(",")
                if x.strip().isdigit()
            ]
            return [candidates[i] for i in picks if i < len(candidates)][:count]
        except Exception as e:
            print(f"[WARN] Claude 篩選失敗，改用預設排序：{e}")

    return candidates[:count]

def format_news(topic: str, items: list[dict]) -> str:
    if not items:
        return f"「{topic}」目前找不到相關新聞"
    parts = [f"📰 {topic} 最新 {len(items)} 則新聞"]
    for i, item in enumerate(items, 1):
        parts.append(f"\n{i}. {item['title']}\n🔗 {item['link']}")
    return "\n".join(parts)

# ── Line 推播工具 ─────────────────────────────────────────────────────────
def push_to_user(messages: list[str]) -> None:
    if not USER_ID:
        print("[WARN] LINE_USER_ID 未設定，略過推播")
        return
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        for msg in messages:
            api.push_message(
                PushMessageRequest(
                    to=USER_ID,
                    messages=[TextMessage(text=msg)],
                )
            )

# ── Webhook（處理使用者訊息）────────────────────────────────────────────
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        events = parser.parse(body, signature)
    except InvalidSignatureError:
        abort(400)

    for event in events:
        if not isinstance(event, MessageEvent):
            continue
        if not isinstance(event.message, TextMessageContent):
            continue

        text       = event.message.text.strip()
        reply_tok  = event.reply_token
        sender_id  = event.source.user_id

        reply = _handle_command(text, sender_id)

        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(
                    reply_token=reply_tok,
                    messages=[TextMessage(text=reply)],
                )
            )

    return "OK"

def _handle_command(text: str, sender_id: str) -> str:
    # 首次設定：顯示 User ID
    if not USER_ID:
        return (
            f"你的 User ID 是：\n{sender_id}\n\n"
            "請將此值設定到環境變數 LINE_USER_ID，再重新部署。"
        )

    if text.startswith("訂閱 "):
        topic = text[3:].strip()
        if not topic:
            return "請輸入主題，例如：訂閱 科技"
        added = add_subscription(topic)
        return f"✅ 已訂閱「{topic}」" if added else f"「{topic}」已在訂閱清單中"

    if text.startswith("取消訂閱 "):
        topic = text[5:].strip()
        if not topic:
            return "請輸入主題，例如：取消訂閱 科技"
        removed = remove_subscription(topic)
        return f"✅ 已取消訂閱「{topic}」" if removed else f"「{topic}」不在訂閱清單中"

    if text == "我的訂閱":
        subs = get_subscriptions()
        if subs:
            return "📋 目前訂閱主題：\n" + "\n".join(f"• {t}" for t in subs)
        return "目前沒有訂閱主題\n輸入「訂閱 主題名稱」來新增"

    if text == "說明" or text == "help":
        return (
            "📖 指令說明\n\n"
            "訂閱 <主題>   → 新增訂閱\n"
            "取消訂閱 <主題> → 移除訂閱\n"
            "我的訂閱      → 查看清單\n"
            "<任意文字>    → 即時搜尋該主題新聞"
        )

    # 預設：即時查詢
    items = fetch_news(text)
    return format_news(text, items)

# ── 排程推播 endpoint（由 GitHub Actions 呼叫）──────────────────────────
@app.route("/trigger-push", methods=["POST"])
def trigger_push():
    """GitHub Actions 在 9:00 / 13:00 / 22:00 用 Bearer token 呼叫此 endpoint"""
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {PUSH_SECRET}":
        abort(401)

    subs = get_subscriptions()
    if not subs:
        return "no subscriptions", 200

    messages = []
    for topic in subs:
        items = fetch_news(topic)
        messages.append(format_news(topic, items))

    push_to_user(messages)
    print(f"[INFO] 推播完成，共 {len(messages)} 個主題")
    return "ok", 200

# ── 啟動 ──────────────────────────────────────────────────────────────────
init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)

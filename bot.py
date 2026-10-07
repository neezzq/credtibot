"""Простой бот поддержки для Telegram (без внешних зависимостей).

Пользователь пишет боту -> сообщение приходит админу.
Админ отвечает через "Ответить" (reply) на это сообщение -> ответ уходит пользователю.
"""
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# На многих VPS сломан IPv6: соединение висит до таймаута. Подключаемся только по IPv4.
_getaddrinfo = socket.getaddrinfo


def _ipv4_only(host, port, family=0, *args, **kwargs):
    return _getaddrinfo(host, port, socket.AF_INET, *args, **kwargs)


socket.getaddrinfo = _ipv4_only

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "data.json")


def load_env():
    path = os.path.join(BASE_DIR, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_ID", "").replace(" ", "").split(",") if x}
WELCOME = os.environ.get(
    "WELCOME_TEXT", "Здравствуйте! Напишите ваш вопрос, и мы ответим как можно скорее."
)
CONFIRM = os.environ.get("CONFIRM_TEXT", "Сообщение получено, скоро ответим.")

if not TOKEN:
    sys.exit("Укажите BOT_TOKEN в файле .env (см. .env.example)")

API = f"https://api.telegram.org/bot{TOKEN}/"

# msg_map: id сообщения в чате админа -> id пользователя; banned: список забаненных
data = {"msg_map": {}, "banned": []}
if os.path.exists(DATA_FILE):
    with open(DATA_FILE, encoding="utf-8") as f:
        data.update(json.load(f))


def save():
    # храним только последние 5000 связей
    if len(data["msg_map"]) > 5000:
        for k in list(data["msg_map"])[:-5000]:
            del data["msg_map"][k]
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)


def call(method, **params):
    req = urllib.request.Request(
        API + method,
        data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r).get("result")
    except urllib.error.HTTPError as e:
        print(f"[{method}] ошибка: {e.read().decode()}")
    except Exception as e:
        print(f"[{method}] ошибка: {e}")
    return None


def send(chat_id, text, **kw):
    return call("sendMessage", chat_id=chat_id, text=text, **kw)


def handle_user(msg):
    user = msg["from"]
    uid = user["id"]
    text = msg.get("text", "")

    if text.startswith("/start"):
        if not ADMIN_IDS:
            send(uid, f"Ваш ID: {uid}\nВпишите его в ADMIN_ID в файле .env и перезапустите бота.")
        else:
            send(uid, WELCOME)
        return
    if uid in data["banned"]:
        return

    name = user.get("first_name", "") + (" " + user["last_name"] if user.get("last_name") else "")
    uname = f"@{user['username']}" if user.get("username") else "без username"
    header = f"📩 {name} ({uname})\nID: {uid}"

    for admin in ADMIN_IDS:
        h = send(admin, header)
        c = call("copyMessage", chat_id=admin, from_chat_id=uid, message_id=msg["message_id"])
        for m in (h, c):
            if m:
                data["msg_map"][str(m["message_id"])] = uid
    save()
    send(uid, CONFIRM)


def handle_admin(msg):
    text = msg.get("text", "")
    reply = msg.get("reply_to_message")

    if text.startswith("/start") or text.startswith("/help"):
        send(
            msg["chat"]["id"],
            "Чтобы ответить пользователю — сделайте Reply на его сообщение.\n"
            "/ban — (reply) заблокировать пользователя\n"
            "/unban — (reply) разблокировать\n"
            "Поддерживаются текст, фото, файлы, голосовые и т.д.",
        )
        return

    if not reply:
        send(msg["chat"]["id"], "Сделайте Reply на сообщение пользователя, чтобы ответить.")
        return

    uid = data["msg_map"].get(str(reply["message_id"]))
    if not uid:
        send(msg["chat"]["id"], "Не удалось определить пользователя (сообщение слишком старое?).")
        return

    if text.startswith("/ban"):
        if uid not in data["banned"]:
            data["banned"].append(uid)
            save()
        send(msg["chat"]["id"], f"🚫 Пользователь {uid} заблокирован.")
    elif text.startswith("/unban"):
        if uid in data["banned"]:
            data["banned"].remove(uid)
            save()
        send(msg["chat"]["id"], f"✅ Пользователь {uid} разблокирован.")
    else:
        res = call(
            "copyMessage",
            chat_id=uid,
            from_chat_id=msg["chat"]["id"],
            message_id=msg["message_id"],
        )
        send(msg["chat"]["id"], "✅ Отправлено" if res else "❌ Не удалось отправить (бот заблокирован пользователем?)")


def main():
    me = call("getMe")
    if not me:
        sys.exit("Не удалось подключиться к Telegram. Проверьте BOT_TOKEN.")
    print(f"Бот @{me['username']} запущен. Админы: {ADMIN_IDS or 'не заданы'}")
    offset = 0
    while True:
        updates = call("getUpdates", offset=offset, timeout=30, allowed_updates=["message"])
        if updates is None:
            time.sleep(3)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            msg = u.get("message")
            if not msg or msg["chat"]["type"] != "private":
                continue
            try:
                if msg["from"]["id"] in ADMIN_IDS:
                    handle_admin(msg)
                else:
                    handle_user(msg)
            except Exception as e:
                print("Ошибка обработки:", e)


if __name__ == "__main__":
    main()

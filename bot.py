"""Простой бот поддержки для Telegram (без внешних зависимостей).

Пользователь отправляет обращение командой /hi текст -> заявка приходит админу
с кнопками (профиль, ответить, бан).
Админ отвечает кнопкой "Ответить" или через Reply на заявку -> ответ уходит пользователю.
"""
import html
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
    "WELCOME_TEXT", "Здравствуйте! Чтобы отправить обращение, напишите:\n/hi ваш вопрос"
)
CONFIRM = os.environ.get("CONFIRM_TEXT", "Обращение #{n} получено, скоро ответим.")
HINT = "Чтобы отправить обращение, напишите команду с текстом:\n/hi ваш вопрос"

if not TOKEN:
    sys.exit("Укажите BOT_TOKEN в файле .env (см. .env.example)")

API = f"https://api.telegram.org/bot{TOKEN}/"

# msg_map: id сообщения в чате админа -> id пользователя; banned: список забаненных;
# counter: номер последнего обращения
data = {"msg_map": {}, "banned": [], "counter": 0}
if os.path.exists(DATA_FILE):
    with open(DATA_FILE, encoding="utf-8") as f:
        data.update(json.load(f))

# админ -> пользователь, которому он сейчас пишет ответ (после кнопки "Ответить")
reply_target = {}


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


def full_name(user):
    return (user.get("first_name", "") + " " + user.get("last_name", "")).strip() or "Без имени"


def ticket_keyboard(uid, username):
    rows = []
    if username:
        rows.append([{"text": f"👤 @{username}", "url": f"https://t.me/{username}"}])
    ban = (
        {"text": "✅ Разбанить", "callback_data": f"unban:{uid}"}
        if uid in data["banned"]
        else {"text": "🚫 Забанить", "callback_data": f"ban:{uid}"}
    )
    rows.append([{"text": "✉️ Ответить", "callback_data": f"reply:{uid}"}, ban])
    return {"inline_keyboard": rows}


def parse_command(text):
    """'/hi@bot текст' -> ('/hi', 'текст')"""
    cmd, _, rest = text.partition(" ")
    return cmd.split("@")[0].lower(), rest.strip()


def handle_user(msg):
    user = msg["from"]
    uid = user["id"]
    cmd, arg = parse_command(msg.get("text", ""))

    if cmd == "/start":
        if not ADMIN_IDS:
            send(uid, f"Ваш ID: {uid}\nВпишите его в ADMIN_ID в файле .env и перезапустите бота.")
        else:
            send(uid, WELCOME)
        return
    if uid in data["banned"]:
        return
    if cmd != "/hi":
        send(uid, HINT)
        return
    if not arg:
        send(uid, "Напишите текст обращения после команды, например:\n/hi не приходит код")
        return

    data["counter"] += 1
    n = data["counter"]
    username = user.get("username")
    text = (
        f"📩 <b>Обращение #{n}</b>\n"
        f'👤 <a href="tg://user?id={uid}">{html.escape(full_name(user))}</a>'
        + (f" (@{username})" if username else "")
        + f"\n🆔 <code>{uid}</code>\n\n"
        + html.escape(arg[:3500])
    )
    for admin in ADMIN_IDS:
        m = send(admin, text, parse_mode="HTML", reply_markup=ticket_keyboard(uid, username))
        if m:
            data["msg_map"][str(m["message_id"])] = uid
    save()
    send(uid, CONFIRM.format(n=n))


def handle_callback(cq):
    admin = cq["from"]["id"]
    if admin not in ADMIN_IDS:
        call("answerCallbackQuery", callback_query_id=cq["id"])
        return
    action, _, uid = cq.get("data", "").partition(":")
    uid = int(uid)
    msg = cq.get("message")

    if action == "reply":
        reply_target[admin] = uid
        call("answerCallbackQuery", callback_query_id=cq["id"])
        send(admin, f"✍️ Напишите ответ пользователю {uid} одним сообщением.\n/cancel — отмена")
        return

    if action == "ban" and uid not in data["banned"]:
        data["banned"].append(uid)
    elif action == "unban" and uid in data["banned"]:
        data["banned"].remove(uid)
    save()
    call(
        "answerCallbackQuery",
        callback_query_id=cq["id"],
        text="🚫 Заблокирован" if action == "ban" else "✅ Разблокирован",
    )
    if msg:
        # достаём username из кнопки профиля, чтобы не потерять её при обновлении
        rows = msg.get("reply_markup", {}).get("inline_keyboard", [])
        url = next((b.get("url", "") for r in rows for b in r if "url" in b), "")
        username = url.rsplit("/", 1)[-1] if url else None
        call(
            "editMessageReplyMarkup",
            chat_id=msg["chat"]["id"],
            message_id=msg["message_id"],
            reply_markup=ticket_keyboard(uid, username),
        )


def answer_user(admin, msg, uid):
    res = call("copyMessage", chat_id=uid, from_chat_id=admin, message_id=msg["message_id"])
    send(admin, "✅ Отправлено" if res else "❌ Не удалось отправить (бот заблокирован пользователем?)")


def handle_admin(msg):
    admin = msg["from"]["id"]
    cmd, _ = parse_command(msg.get("text", ""))
    reply = msg.get("reply_to_message")

    if cmd in ("/start", "/help"):
        send(
            admin,
            "Обращения приходят с кнопками:\n"
            "✉️ Ответить — следующее ваше сообщение уйдёт пользователю\n"
            "🚫 Забанить / ✅ Разбанить\n\n"
            "Ещё можно сделать Reply на обращение — ответ уйдёт автору.\n"
            "/cancel — отменить ответ",
        )
        return
    if cmd == "/cancel":
        reply_target.pop(admin, None)
        send(admin, "Отменено.")
        return

    uid = data["msg_map"].get(str(reply["message_id"])) if reply else None
    if uid:
        reply_target.pop(admin, None)
        answer_user(admin, msg, uid)
    elif admin in reply_target:
        answer_user(admin, msg, reply_target.pop(admin))
    else:
        send(admin, "Нажмите «✉️ Ответить» под обращением или сделайте Reply на него.")


def main():
    me = call("getMe")
    if not me:
        sys.exit("Не удалось подключиться к Telegram. Проверьте BOT_TOKEN.")
    print(f"Бот @{me['username']} запущен. Админы: {ADMIN_IDS or 'не заданы'}")
    offset = 0
    while True:
        updates = call(
            "getUpdates", offset=offset, timeout=30, allowed_updates=["message", "callback_query"]
        )
        if updates is None:
            time.sleep(3)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            try:
                if "callback_query" in u:
                    handle_callback(u["callback_query"])
                    continue
                msg = u.get("message")
                if not msg or msg["chat"]["type"] != "private":
                    continue
                if msg["from"]["id"] in ADMIN_IDS:
                    handle_admin(msg)
                else:
                    handle_user(msg)
            except Exception as e:
                print("Ошибка обработки:", e)


if __name__ == "__main__":
    main()

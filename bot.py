"""Простой бот поддержки для Telegram (без внешних зависимостей).

Пользователь отправляет обращение командой /hi текст -> заявка приходит админу
с кнопками (профиль, ответить, бан).
Админ отвечает кнопкой "Ответить" или через Reply на заявку -> ответ уходит пользователю.
/tickets — список всех обращений с листанием.
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
from datetime import datetime, timedelta, timezone

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
# часовой пояс для дат в списке обращений (по умолчанию МСК, UTC+3)
TZ = timezone(timedelta(hours=float(os.environ.get("TZ_OFFSET", "3"))))
PAGE_SIZE = 5

if not TOKEN:
    sys.exit("Укажите BOT_TOKEN в файле .env (см. .env.example)")

API = f"https://api.telegram.org/bot{TOKEN}/"

# msg_map: id сообщения в чате админа -> id пользователя (старые заявки);
# msg_ticket: id сообщения в чате админа -> номер обращения; banned: список забаненных;
# counter: номер последнего обращения; tickets: все обращения
data = {"msg_map": {}, "msg_ticket": {}, "banned": [], "counter": 0, "tickets": []}
if os.path.exists(DATA_FILE):
    with open(DATA_FILE, encoding="utf-8") as f:
        data.update(json.load(f))

# админ -> номер обращения, на которое он сейчас пишет ответ (после кнопки "Ответить")
reply_target = {}


def save():
    # храним только последние 5000 связей
    for key in ("msg_map", "msg_ticket"):
        if len(data[key]) > 5000:
            for k in list(data[key])[:-5000]:
                del data[key][k]
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def call(method, **params):
    params = {k: v for k, v in params.items() if v is not None}
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


def fmt_date(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m.%Y %H:%M")


def who(t):
    name = f'<a href="tg://user?id={t["uid"]}">{html.escape(t["name"])}</a>'
    return name + (f" (@{t['username']})" if t.get("username") else "")


def ticket_text(t):
    status = "✅ отвечено" if t.get("answered") else "🕐 ждёт ответа"
    return (
        f"📩 <b>Обращение #{t['n']}</b> · {status}\n"
        f"📅 {fmt_date(t['date'])}\n"
        f"👤 {who(t)}\n"
        f"🆔 <code>{t['uid']}</code>\n\n"
        + html.escape(t["text"])
    )


def ticket_keyboard(t):
    uid, username = t["uid"], t.get("username")
    rows = []
    if username:
        rows.append([{"text": f"👤 @{username}", "url": f"https://t.me/{username}"}])
    ban = (
        {"text": "✅ Разбанить", "callback_data": f"unban:{uid}"}
        if uid in data["banned"]
        else {"text": "🚫 Забанить", "callback_data": f"ban:{uid}"}
    )
    rows.append([{"text": "✉️ Ответить", "callback_data": f"ans:{t['n']}"}, ban])
    return {"inline_keyboard": rows}


def send_ticket(admin, t):
    m = send(admin, ticket_text(t), parse_mode="HTML", reply_markup=ticket_keyboard(t))
    if m:
        data["msg_ticket"][str(m["message_id"])] = t["n"]
    return m


def find_ticket(n):
    return next((t for t in data["tickets"] if t["n"] == n), None)


def last_ticket(uid):
    """Последнее обращение пользователя — для заявок, отправленных до появления номеров."""
    return next((t for t in reversed(data["tickets"]) if t["uid"] == uid), None)


def tickets_page(page, order):
    """Текст и кнопки страницы списка. order: 'new' — сначала новые, 'old' — сначала старые."""
    tickets = sorted(data["tickets"], key=lambda t: (t["date"], t["n"]), reverse=order == "new")
    total = len(tickets)
    if not total:
        return "📋 Обращений пока нет.", None
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    page = max(0, min(page, pages - 1))
    chunk = tickets[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    waiting = sum(1 for t in tickets if not t.get("answered"))

    lines = [
        f"📋 <b>Обращения</b>: всего {total}, ждут ответа {waiting}\n"
        f"Сортировка: {'сначала новые' if order == 'new' else 'сначала старые'}"
    ]
    for t in chunk:
        preview = t["text"] if len(t["text"]) <= 100 else t["text"][:100] + "…"
        lines.append(
            f"<b>#{t['n']}</b> · {fmt_date(t['date'])} · {'✅' if t.get('answered') else '🕐'}\n"
            f"👤 {who(t)}\n"
            f"{html.escape(preview)}"
        )

    nav = []
    if page > 0:
        nav.append({"text": "◀️", "callback_data": f"page:{page - 1}:{order}"})
    nav.append({"text": f"{page + 1}/{pages}", "callback_data": "noop"})
    if page < pages - 1:
        nav.append({"text": "▶️", "callback_data": f"page:{page + 1}:{order}"})
    other = "old" if order == "new" else "new"
    keyboard = {
        "inline_keyboard": [
            [{"text": f"#{t['n']}", "callback_data": f"open:{t['n']}"} for t in chunk],
            nav,
            [{
                "text": "🔃 Сначала старые" if order == "new" else "🔃 Сначала новые",
                "callback_data": f"page:0:{other}",
            }],
        ]
    }
    return "\n\n".join(lines), keyboard


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
    t = {
        "n": data["counter"],
        "uid": uid,
        "name": full_name(user),
        "username": user.get("username"),
        "text": arg[:3500],
        "date": int(time.time()),
        "answered": False,
    }
    data["tickets"].append(t)
    for admin in ADMIN_IDS:
        send_ticket(admin, t)
    save()
    send(uid, CONFIRM.format(n=t["n"]))


def handle_callback(cq):
    admin = cq["from"]["id"]
    if admin not in ADMIN_IDS:
        call("answerCallbackQuery", callback_query_id=cq["id"])
        return
    action, _, arg = cq.get("data", "").partition(":")
    msg = cq.get("message")

    if action == "noop":
        call("answerCallbackQuery", callback_query_id=cq["id"])
        return

    if action == "page":
        page, _, order = arg.partition(":")
        text, keyboard = tickets_page(int(page), order)
        call("answerCallbackQuery", callback_query_id=cq["id"])
        call(
            "editMessageText",
            chat_id=msg["chat"]["id"],
            message_id=msg["message_id"],
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        return

    if action == "open":
        t = find_ticket(int(arg))
        call("answerCallbackQuery", callback_query_id=cq["id"], text="" if t else "Обращение не найдено")
        if t:
            send_ticket(admin, t)
            save()
        return

    if action in ("ans", "reply"):
        # ans:<номер обращения>; reply:<id пользователя> — кнопки у старых заявок
        t = find_ticket(int(arg)) if action == "ans" else last_ticket(int(arg))
        uid = t["uid"] if t else int(arg)
        reply_target[admin] = (uid, t["n"] if t else None)
        call("answerCallbackQuery", callback_query_id=cq["id"])
        about = f"по обращению #{t['n']}" if t else f"пользователю {uid}"
        send(admin, f"✍️ Напишите ответ {about} одним сообщением.\n/cancel — отмена")
        return

    uid = int(arg)
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
    t = None
    if msg:
        # находим обращение по кнопке "Ответить", чтобы перерисовать клавиатуру заявки
        rows = msg.get("reply_markup", {}).get("inline_keyboard", [])
        n = next((b["callback_data"][4:] for r in rows for b in r
                  if b.get("callback_data", "").startswith("ans:")), None)
        t = find_ticket(int(n)) if n else last_ticket(uid)
    if t:
        call(
            "editMessageReplyMarkup",
            chat_id=msg["chat"]["id"],
            message_id=msg["message_id"],
            reply_markup=ticket_keyboard(t),
        )


def utf16_len(s):
    return len(s.encode("utf-16-le")) // 2


def with_header(header, text, entities):
    """Добавляет жирный заголовок перед текстом, сдвигая форматирование админа."""
    prefix = header + ("\n\n" if text else "")
    shift = utf16_len(prefix)
    ents = [{"type": "bold", "offset": 0, "length": utf16_len(header)}]
    ents += [dict(e, offset=e["offset"] + shift) for e in entities or []]
    return prefix + (text or ""), ents


CAPTION_TYPES = ("photo", "video", "document", "audio", "voice", "animation")


def answer_user(admin, msg, uid, n):
    header = f"Ответ по обращению #{n}" if n else "Ответ поддержки"
    if "text" in msg:
        text, ents = with_header(header, msg["text"], msg.get("entities"))
        res = call("sendMessage", chat_id=uid, text=text, entities=ents)
    elif any(k in msg for k in CAPTION_TYPES):
        caption, ents = with_header(header, msg.get("caption"), msg.get("caption_entities"))
        res = call(
            "copyMessage", chat_id=uid, from_chat_id=admin, message_id=msg["message_id"],
            caption=caption[:1024], caption_entities=ents,
        )
    else:
        # стикеры, кружки и т.п. не поддерживают подпись — заголовок отдельным сообщением
        text, ents = with_header(header, "", None)
        res = call("sendMessage", chat_id=uid, text=text, entities=ents) and call(
            "copyMessage", chat_id=uid, from_chat_id=admin, message_id=msg["message_id"]
        )
    if res:
        t = find_ticket(n) if n else None
        if t:
            t["answered"] = True
            save()
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
            "/tickets — все обращения (по 5 на странице)\n"
            "/cancel — отменить ответ",
        )
        return
    if cmd == "/cancel":
        reply_target.pop(admin, None)
        send(admin, "Отменено.")
        return
    if cmd == "/tickets":
        text, keyboard = tickets_page(0, "new")
        send(admin, text, parse_mode="HTML", reply_markup=keyboard)
        return

    target = None
    if reply:
        key = str(reply["message_id"])
        if key in data["msg_ticket"]:
            t = find_ticket(data["msg_ticket"][key])
            target = (t["uid"], t["n"]) if t else None
        elif key in data["msg_map"]:
            uid = data["msg_map"][key]
            t = last_ticket(uid)
            target = (uid, t["n"] if t else None)
    if target:
        reply_target.pop(admin, None)
        answer_user(admin, msg, *target)
    elif admin in reply_target:
        answer_user(admin, msg, *reply_target.pop(admin))
    else:
        send(admin, "Нажмите «✉️ Ответить» под обращением или сделайте Reply на него.")


def main():
    me = call("getMe")
    if not me:
        sys.exit("Не удалось подключиться к Telegram. Проверьте BOT_TOKEN.")
    print(f"Бот @{me['username']} запущен. Админы: {ADMIN_IDS or 'не заданы'}")
    call(
        "setMyCommands",
        commands=[{"command": "hi", "description": "Отправить обращение: /hi текст"}],
    )
    for admin in ADMIN_IDS:
        call(
            "setMyCommands",
            scope={"type": "chat", "chat_id": admin},
            commands=[
                {"command": "tickets", "description": "Все обращения"},
                {"command": "cancel", "description": "Отменить ответ"},
                {"command": "help", "description": "Помощь"},
            ],
        )
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

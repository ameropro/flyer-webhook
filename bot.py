#!/usr/bin/env python3
import os
import logging
import sqlite3
import asyncio
from datetime import datetime, timedelta
from typing import Optional, List, Tuple

from aiohttp import web

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes, CallbackQueryHandler,
    MessageHandler, filters
)

# ================== CONFIG (через ENV) ==================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "AmeroStars_bot").strip()  # без @
DB_PATH = os.getenv("DB_PATH", "referrals_v2.db").strip()

# Tgrass webhook (Cloudflare Worker -> Render)
TGRASS_SECRET = os.getenv("TGRASS_SECRET", "").strip()

# Награды
DEFAULT_TASK_REWARD = int(os.getenv("DEFAULT_TASK_REWARD", "2"))
DEFAULT_SUB_REWARD = int(os.getenv("DEFAULT_SUB_REWARD", "2"))  # 1 раз в 24 часа

# Рефералка
REF_BONUS_ON_SUB = int(os.getenv("REF_BONUS_ON_SUB", "5"))

# Выводы
MAX_WITHDRAWS_PER_DAY = int(os.getenv("MAX_WITHDRAWS_PER_DAY", "5"))

# Авто-админы (через запятую)
AUTO_ADMINS = []
_auto_admins = os.getenv("AUTO_ADMINS", "").strip()
if _auto_admins:
    for x in _auto_admins.split(","):
        x = x.strip()
        if x.isdigit():
            AUTO_ADMINS.append(int(x))

# Порт для Render
PORT = int(os.getenv("PORT", "10000"))

# ================== LOGGING ==================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("bot")

# ================== DB ==================
def db_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    conn = db_conn()
    cur = conn.cursor()

    # users
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_seen TIMESTAMP,
        referred_by INTEGER,
        balance INTEGER DEFAULT 0
    );
    """)

    # referrals: один реферер на человека
    cur.execute("""
    CREATE TABLE IF NOT EXISTS referrals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        referrer_id INTEGER,
        referred_id INTEGER UNIQUE,
        ts TIMESTAMP
    );
    """)

    # admins
    cur.execute("""
    CREATE TABLE IF NOT EXISTS admins (
        user_id INTEGER PRIMARY KEY
    );
    """)

    # withdraw requests
    cur.execute("""
    CREATE TABLE IF NOT EXISTS withdraw_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount INTEGER,
        kind TEXT,
        status TEXT DEFAULT 'pending',
        admin_id INTEGER,
        ts TIMESTAMP
    );
    """)

    # promocodes
    cur.execute("""
    CREATE TABLE IF NOT EXISTS promocodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE,
        amount INTEGER,
        max_uses INTEGER,
        uses_count INTEGER DEFAULT 0,
        valid_until TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # promocode activations
    cur.execute("""
    CREATE TABLE IF NOT EXISTS promocode_activations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        promocode_id INTEGER,
        activated_at TIMESTAMP,
        UNIQUE(user_id, promocode_id)
    );
    """)

    # === Tgrass tables ===
    # события, чтобы не начислять дважды за одно событие
    cur.execute("""
    CREATE TABLE IF NOT EXISTS tgrass_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT UNIQUE,
        user_id INTEGER,
        event_type TEXT,
        reward INTEGER,
        ts TIMESTAMP
    );
    """)

    # подписка 1 раз в 24 часа
    cur.execute("""
    CREATE TABLE IF NOT EXISTS tgrass_daily_subs (
        user_id INTEGER PRIMARY KEY,
        last_reward_at TIMESTAMP
    );
    """)

    # авто-админы
    for admin_id in AUTO_ADMINS:
        cur.execute("INSERT OR IGNORE INTO admins(user_id) VALUES (?)", (admin_id,))

    conn.commit()
    conn.close()

def add_user(user_id: int, username: str, referred_by: Optional[int]):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id, referred_by FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()

    if row is None:
        cur.execute(
            "INSERT INTO users(user_id, username, first_seen, referred_by, balance) VALUES(?,?,?,?,0)",
            (user_id, username, datetime.utcnow().isoformat(), referred_by, )
        )
        # referral row
        if referred_by and referred_by != user_id:
            try:
                cur.execute(
                    "INSERT OR IGNORE INTO referrals(referrer_id, referred_id, ts) VALUES(?,?,?)",
                    (referred_by, user_id, datetime.utcnow().isoformat())
                )
            except Exception:
                pass
    else:
        # не переписываем referred_by если уже есть
        existing_ref = row[1]
        if existing_ref is None and referred_by and referred_by != user_id:
            cur.execute("UPDATE users SET referred_by=? WHERE user_id=?", (referred_by, user_id))
        cur.execute("UPDATE users SET username=? WHERE user_id=?", (username, user_id))

    conn.commit()
    conn.close()

def get_balance(user_id: int) -> int:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(balance,0) FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return int(row[0]) if row else 0

def change_balance(user_id: int, delta: int):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET balance = COALESCE(balance,0) + ? WHERE user_id=?", (delta, user_id))
    conn.commit()
    conn.close()

def is_admin(user_id: int) -> bool:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,))
    ok = cur.fetchone() is not None
    conn.close()
    return ok

def withdraws_count_last_24h(user_id: int) -> int:
    since = (datetime.utcnow() - timedelta(hours=24)).isoformat()
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM withdraw_requests WHERE user_id=? AND ts>?", (user_id, since))
    c = cur.fetchone()[0]
    conn.close()
    return int(c)

def create_withdraw_request(user_id: int, amount: int, kind: str) -> int:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO withdraw_requests(user_id, amount, kind, status, ts) VALUES(?,?,?,'pending',?)",
        (user_id, amount, kind, datetime.utcnow().isoformat())
    )
    rid = cur.lastrowid
    conn.commit()
    conn.close()
    return int(rid)

def list_pending_withdraws() -> List[Tuple]:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT id,user_id,amount,kind,ts FROM withdraw_requests WHERE status='pending' ORDER BY ts ASC")
    rows = cur.fetchall()
    conn.close()
    return rows

def set_withdraw_status(req_id: int, status: str, admin_id: int):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("UPDATE withdraw_requests SET status=?, admin_id=? WHERE id=?", (status, admin_id, req_id))
    conn.commit()
    conn.close()

# ===== Promocodes =====
def create_promocode(code: str, amount: int, max_uses: int, days_valid: int):
    valid_until = (datetime.utcnow() + timedelta(days=days_valid)).isoformat()
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO promocodes(code, amount, max_uses, valid_until) VALUES(?,?,?,?)",
        (code.upper(), amount, max_uses, valid_until)
    )
    conn.commit()
    conn.close()

def get_promocode(code: str):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT id,amount,max_uses,uses_count,valid_until FROM promocodes WHERE code=?", (code.upper(),))
    row = cur.fetchone()
    conn.close()
    return row

def activate_promocode(user_id: int, promo_id: int) -> Tuple[bool, str]:
    conn = db_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT 1 FROM promocode_activations WHERE user_id=? AND promocode_id=?", (user_id, promo_id))
        if cur.fetchone():
            return False, "Ты уже активировал этот промокод."

        cur.execute("SELECT amount,max_uses,uses_count,valid_until FROM promocodes WHERE id=?", (promo_id,))
        row = cur.fetchone()
        if not row:
            return False, "Промокод не найден."

        amount, max_uses, uses_count, valid_until = row
        if int(uses_count) >= int(max_uses):
            return False, "Лимит активаций промокода исчерпан."
        if datetime.fromisoformat(str(valid_until)) < datetime.utcnow():
            return False, "Срок действия промокода истёк."

        cur.execute("UPDATE promocodes SET uses_count = uses_count + 1 WHERE id=?", (promo_id,))
        cur.execute(
            "INSERT INTO promocode_activations(user_id,promocode_id,activated_at) VALUES(?,?,?)",
            (user_id, promo_id, datetime.utcnow().isoformat())
        )
        cur.execute("UPDATE users SET balance = COALESCE(balance,0) + ? WHERE user_id=?", (int(amount), user_id))

        conn.commit()
        return True, f"✅ Промокод активирован! +{amount}⭐"
    except Exception as e:
        conn.rollback()
        logger.exception(e)
        return False, "Ошибка при активации промокода."
    finally:
        conn.close()

# ===== Tgrass logic =====
def tgrass_seen_event(event_id: str) -> bool:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM tgrass_events WHERE event_id=?", (event_id,))
    ok = cur.fetchone() is not None
    conn.close()
    return ok

def tgrass_save_event(event_id: str, user_id: int, event_type: str, reward: int):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO tgrass_events(event_id,user_id,event_type,reward,ts) VALUES(?,?,?,?,?)",
        (event_id, user_id, event_type, reward, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()

def can_daily_sub_reward(user_id: int) -> bool:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT last_reward_at FROM tgrass_daily_subs WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if not row or not row[0]:
        return True
    last = datetime.fromisoformat(str(row[0]))
    return (datetime.utcnow() - last) >= timedelta(hours=24)

def mark_daily_sub_reward(user_id: int):
    now = datetime.utcnow().isoformat()
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tgrass_daily_subs(user_id,last_reward_at) VALUES(?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET last_reward_at=excluded.last_reward_at",
        (user_id, now)
    )
    conn.commit()
    conn.close()

def get_referred_by(user_id: int) -> Optional[int]:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT referred_by FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return row[0]

# ================== BOT UI ==================
def main_menu_kb(is_admin_user: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("⭐ Баланс", callback_data="balance")],
        [InlineKeyboardButton("💰 Заработать (Tgrass)", callback_data="earn")],
        [InlineKeyboardButton("💸 Вывод", callback_data="withdraw")],
        [InlineKeyboardButton("🎁 Промокод", callback_data="promo")],
        [InlineKeyboardButton("👥 Реф-ссылка", callback_data="ref")],
    ]
    if is_admin_user:
        rows.append([InlineKeyboardButton("⚙️ Админ", callback_data="admin")])
    return InlineKeyboardMarkup(rows)

def admin_menu_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📨 Pending выводы", callback_data="admin_withdraws")],
        [InlineKeyboardButton("🎁 Промокоды", callback_data="admin_promos")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back")],
    ]
    return InlineKeyboardMarkup(rows)

# ================== HANDLERS ==================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    referred_by = None

    if args and args[0].startswith("r") and args[0][1:].isdigit():
        referred_by = int(args[0][1:])

    add_user(user.id, user.username or user.full_name or f"user_{user.id}", referred_by)

    text = (
        "🏠 Меню\n\n"
        "• Задания и подписки приходят через Tgrass\n"
        "• Начисление идёт автоматически по webhook\n\n"
        "Выбирай кнопку ниже:"
    )
    await update.message.reply_text(text, reply_markup=main_menu_kb(is_admin(user.id)))

async def cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data

    if data == "back":
        await q.edit_message_text("🏠 Меню", reply_markup=main_menu_kb(is_admin(uid)))
        return

    if data == "balance":
        bal = get_balance(uid)
        await q.edit_message_text(f"⭐ Твой баланс: <b>{bal}</b>", parse_mode="HTML",
                                  reply_markup=main_menu_kb(is_admin(uid)))
        return

    if data == "earn":
        # Тут ты ставишь ссылку на Tgrass (их offerwall / smartlink / etc.)
        tgrass_url = os.getenv("TGRASS_EARN_URL", "").strip()
        if not tgrass_url:
            await q.edit_message_text(
                "⚠️ Админ не настроил ссылку Tgrass.\n"
                "Надо на Render добавить переменную окружения TGRASS_EARN_URL.",
                reply_markup=main_menu_kb(is_admin(uid))
            )
            return

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 Открыть задания", url=tgrass_url)],
            [InlineKeyboardButton("🔙 Назад", callback_data="back")]
        ])
        await q.edit_message_text("💰 Заработок через Tgrass:", reply_markup=kb)
        return

    if data == "withdraw":
        await q.edit_message_text(
            "💸 Вывод\n\n"
            "Напиши сообщение в формате:\n"
            "<pre>вывод 100 карта</pre>\n\n"
            "Где:\n"
            "• 100 — сумма ⭐\n"
            "• карта/премиум/другое — тип вывода",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back")]])
        )
        context.user_data["await_withdraw"] = True
        return

    if data == "promo":
        await q.edit_message_text(
            "🎁 Введи промокод сообщением (просто текстом).\n\nПример: <pre>NEWYEAR</pre>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back")]])
        )
        context.user_data["await_promo"] = True
        return

    if data == "ref":
        link = f"https://t.me/{BOT_USERNAME}?start=r{uid}"
        await q.edit_message_text(
            f"👥 Твоя реф-ссылка:\n{link}",
            reply_markup=main_menu_kb(is_admin(uid))
        )
        return

    if data == "admin":
        if not is_admin(uid):
            await q.edit_message_text("❌ Нет доступа.", reply_markup=main_menu_kb(False))
            return
        await q.edit_message_text("⚙️ Админ меню:", reply_markup=admin_menu_kb())
        return

    if data == "admin_withdraws":
        if not is_admin(uid):
            return
        rows = list_pending_withdraws()
        if not rows:
            await q.edit_message_text("📨 Pending выводов нет.", reply_markup=admin_menu_kb())
            return
        text = "📨 <b>Pending выводы</b>\n\n"
        for rid, user_id, amount, kind, ts in rows[:20]:
            text += f"#{rid} | {user_id} | {amount}⭐ | {kind} | {ts}\n"
        text += "\nОдобрить: /approve ID\nОтклонить: /reject ID"
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=admin_menu_kb())
        return

    if data == "admin_promos":
        if not is_admin(uid):
            return
        await q.edit_message_text(
            "🎁 Промокоды\n\n"
            "Создать: /createpromo CODE AMOUNT MAXUSES DAYS\n"
            "Пример: /createpromo NEWYEAR 10 100 30\n",
            reply_markup=admin_menu_kb()
        )
        return

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.message.text or "").strip()

    # вывод
    if context.user_data.get("await_withdraw"):
        context.user_data["await_withdraw"] = False

        if not text.lower().startswith("вывод "):
            await update.message.reply_text("❌ Формат неверный. Пример: вывод 100 карта")
            return

        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            await update.message.reply_text("❌ Формат: вывод <сумма> <тип>")
            return

        try:
            amount = int(parts[1])
        except ValueError:
            await update.message.reply_text("❌ Сумма должна быть числом.")
            return

        kind = parts[2].strip()
        bal = get_balance(uid)
        if amount <= 0:
            await update.message.reply_text("❌ Сумма должна быть > 0.")
            return
        if amount > bal:
            await update.message.reply_text("❌ Недостаточно звёзд.")
            return
        if withdraws_count_last_24h(uid) >= MAX_WITHDRAWS_PER_DAY:
            await update.message.reply_text("❌ Лимит заявок на вывод за 24 часа исчерпан.")
            return

        # списываем сразу
        change_balance(uid, -amount)
        rid = create_withdraw_request(uid, amount, kind)
        await update.message.reply_text(f"✅ Заявка создана: #{rid}\nОжидай обработки админом.")
        return

    # промокод
    if context.user_data.get("await_promo"):
        context.user_data["await_promo"] = False
        code = text.strip().upper()
        promo = get_promocode(code)
        if not promo:
            await update.message.reply_text("❌ Промокод не найден.")
            return
        promo_id = int(promo[0])
        ok, msg = activate_promocode(uid, promo_id)
        await update.message.reply_text(msg)
        return

    await update.message.reply_text("🏠 Открой меню: /start")

# ================== ADMIN COMMANDS ==================
async def createpromo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("❌ Только админ.")
        return
    if len(context.args) != 4:
        await update.message.reply_text("Формат: /createpromo CODE AMOUNT MAXUSES DAYS")
        return
    code = context.args[0].upper()
    amount = int(context.args[1])
    max_uses = int(context.args[2])
    days = int(context.args[3])
    try:
        create_promocode(code, amount, max_uses, days)
        await update.message.reply_text(f"✅ Промокод создан: {code} (+{amount}⭐)")
    except Exception as e:
        logger.exception(e)
        await update.message.reply_text("❌ Ошибка создания промокода (возможно, уже существует).")

async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("❌ Только админ.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Формат: /approve ID")
        return
    rid = int(context.args[0])
    set_withdraw_status(rid, "approved", uid)
    await update.message.reply_text(f"✅ Одобрено #{rid}")

async def reject_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("❌ Только админ.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Формат: /reject ID")
        return
    rid = int(context.args[0])
    # возврат денег пользователю
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id, amount, status FROM withdraw_requests WHERE id=?", (rid,))
    row = cur.fetchone()
    conn.close()
    if not row:
        await update.message.reply_text("❌ Заявка не найдена.")
        return
    user_id, amount, status = row
    if status != "pending":
        await update.message.reply_text("❌ Уже обработано.")
        return

    change_balance(int(user_id), int(amount))
    set_withdraw_status(rid, "rejected", uid)
    await update.message.reply_text(f"❌ Отклонено #{rid} (сумма возвращена)")

# ================== TGRASS WEBHOOK SERVER ==================
# Ожидаем JSON от твоего Worker (он просто пересылает тело):
# {
#   "event_id": "unique_string",
#   "user_id": 123456789,
#   "type": "task" | "subscription",
#   "reward": 2
# }
async def tgrass_webhook(request: web.Request):
    secret = request.headers.get("X-TGRASS-SECRET", "")
    if not TGRASS_SECRET or secret != TGRASS_SECRET:
        return web.Response(status=403, text="forbidden")

    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="bad json")

    event_id = str(data.get("event_id", "")).strip()
    user_id = data.get("user_id")
    event_type = str(data.get("type", "")).strip().lower()
    reward = data.get("reward", None)

    if not event_id or not isinstance(user_id, int) or event_type not in ("task", "subscription"):
        return web.Response(status=400, text="bad payload")

    if reward is None:
        reward = DEFAULT_TASK_REWARD if event_type == "task" else DEFAULT_SUB_REWARD
    reward = int(reward)

    if tgrass_seen_event(event_id):
        return web.Response(text="duplicate")

    # гарантируем, что юзер есть в users
    add_user(user_id, f"user_{user_id}", None)

    # === начисления ===
    if event_type == "task":
        change_balance(user_id, reward)
        tgrass_save_event(event_id, user_id, event_type, reward)
        return web.Response(text="ok task")

    # subscription: 1 раз в 24 часа
    if not can_daily_sub_reward(user_id):
        tgrass_save_event(event_id, user_id, event_type, 0)
        return web.Response(text="cooldown")

    change_balance(user_id, reward)
    mark_daily_sub_reward(user_id)
    tgrass_save_event(event_id, user_id, event_type, reward)

    # бонус рефереру (если есть)
    ref = get_referred_by(user_id)
    if ref and isinstance(ref, int) and ref != user_id and REF_BONUS_ON_SUB > 0:
        change_balance(ref, REF_BONUS_ON_SUB)

    return web.Response(text="ok sub")

async def start_web_server():
    app = web.Application()
    app.router.add_post("/tgrass", tgrass_webhook)     # сюда будет стучать Cloudflare Worker
    app.router.add_get("/", lambda r: web.Response(text="OK"))  # healthcheck

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Webhook server started on 0.0.0.0:{PORT}")

# ================== MAIN ==================
async def run_bot():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty (set it in Render Environment).")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    # admin commands
    app.add_handler(CommandHandler("createpromo", createpromo_cmd))
    app.add_handler(CommandHandler("approve", approve_cmd))
    app.add_handler(CommandHandler("reject", reject_cmd))

    # запуск в “ручном” режиме, чтобы вместе с aiohttp
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    logger.info("Telegram polling started.")

    # веб-сервер
    await start_web_server()

    # вечный idle
    await asyncio.Event().wait()

def main():
    asyncio.run(run_bot())

if __name__ == "__main__":
    main()

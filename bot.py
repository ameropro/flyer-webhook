import json
import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import aiohttp
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from database import (
    add_sponsor,
    add_user_if_not_exists,
    create_assignment,
    create_promocode,
    create_task,
    delete_subscription_watch,
    get_assignment,
    get_assignment_by_task_user,
    get_stats,
    get_subscription_watch,
    get_task,
    get_user,
    increment_balance,
    init_db,
    list_sponsors,
    list_subscription_watches,
    list_tasks,
    redeem_promocode,
    remove_sponsor,
    schedule_subscription_watch,
    update_assignment_status,
    update_level,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN", "")
CRYPTOPAY_TOKEN = os.getenv("CRYPTOPAY_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x}

COIN_TO_USD = 0.00005  # 1000 монет = 0.05$
MIN_REWARD = {"subscribe": 1000, "view": 300, "reaction": 500}

MAIN_MENU = [
    [KeyboardButton("👤 Профиль"), KeyboardButton("📝 Задания")],
    [KeyboardButton("💰 Пополнить баланс"), KeyboardButton("💸 Создать задание")],
    [KeyboardButton("🎯 Промокод"), KeyboardButton("📊 Админ панель")],
]


@dataclass
class PendingProof:
    assignment_id: int
    type: str


async def ensure_sponsors(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    sponsors = list_sponsors()
    if not sponsors:
        return True

    missing = []
    for sponsor in sponsors:
        try:
            member = await context.bot.get_chat_member(chat_id=sponsor["chat_id"], user_id=user_id)
            if member.status in ("left", "kicked"):
                missing.append(sponsor)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Sponsor check failed: %s", exc)
            missing.append(sponsor)

    if missing:
        buttons = []
        for s in missing:
            chat_id = str(s["chat_id"])
            url = chat_id if chat_id.startswith("http") else f"https://t.me/{chat_id.lstrip('@').lstrip('-')}"
            buttons.append([InlineKeyboardButton(s["title"] or chat_id, url=url)])
        buttons.append([InlineKeyboardButton("✅ Проверить подписку", callback_data="check_sponsors")])
        await update.effective_message.reply_text(
            "👋 Привет! Подпишись на спонсоров, чтобы продолжить.", reply_markup=InlineKeyboardMarkup(buttons)
        )
        return False
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    referrer = None
    if context.args:
        ref_arg = context.args[0]
        if ref_arg.startswith("ref_") and ref_arg[4:].isdigit():
            referrer = int(ref_arg[4:])
    add_user_if_not_exists(user.id, referrer)

    sponsors_ok = await ensure_sponsors(update, context)
    if not sponsors_ok:
        return

    await send_main_menu(update)


async def send_main_menu(update: Update) -> None:
    await update.effective_message.reply_text(
        "Выберите действие:", reply_markup=ReplyKeyboardMarkup(MAIN_MENU, resize_keyboard=True)
    )


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_row = get_user(user_id)
    if not user_row:
        add_user_if_not_exists(user_id)
        user_row = get_user(user_id)
    level = user_row["level"]
    balance = user_row["balance"]
    await update.effective_message.reply_html(
        f"👤 <b>Профиль</b>\n"
        f"ID: <code>{user_id}</code>\n"
        f"Уровень: <b>{level}</b>\n"
        f"Баланс: <b>{balance} монет</b>\n"
        "Реферальная ссылка: "
        f"<code>https://t.me/{context.bot.username}?start=ref_{user_id}</code>\n"
        "За каждое выполненное задание по вашей ссылке вы получаете 15% награды.",
    )


def _task_label(row) -> str:
    emoji = {"subscribe": "👥", "view": "👁", "reaction": "❤️"}.get(row["type"], "📝")
    return f"{emoji} {row['title']} • {row['reward']} монет"


async def list_tasks_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tasks = list_tasks()
    if not tasks:
        await update.effective_message.reply_text("Пока нет активных заданий.")
        return
    buttons = [[InlineKeyboardButton(_task_label(t), callback_data=f"task_{t['id']}")] for t in tasks]
    await update.effective_message.reply_text("Выберите задание:", reply_markup=InlineKeyboardMarkup(buttons))


async def task_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    task_id = int(query.data.split("_")[1])
    task = get_task(task_id)
    if not task:
        await query.edit_message_text("Задание не найдено.")
        return
    payload = json.loads(task["payload"])
    text = (
        f"<b>{task['title']}</b>\n{task['description'] or ''}\n\n"
        f"Тип: {task['type']}\nНаграда: {task['reward']} монет"
    )
    buttons = [[InlineKeyboardButton("✅ Выполнить", callback_data=f"take_{task_id}")]]
    if task["type"] == "subscribe" and payload.get("chat_username"):
        buttons.append(
            [InlineKeyboardButton("Перейти", url=f"https://t.me/{payload['chat_username'].lstrip('@')}")]
        )
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML)


async def take_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    task_id = int(query.data.split("_")[1])
    task = get_task(task_id)
    if not task:
        await query.edit_message_text("Задание не найдено.")
        return
    payload = json.loads(task["payload"])
    user_id = query.from_user.id

    existing = get_assignment_by_task_user(task_id, user_id)
    if existing and existing["status"] not in ("rejected", "needs_work"):
        await query.edit_message_text("Вы уже взяли это задание.")
        return
    assignment_id = create_assignment(task_id, user_id)

    if task["type"] == "subscribe":
        await query.edit_message_text(
            f"Подпишись на канал/группу: {payload.get('chat_username')}\n"
            "После подписки нажми «Проверить».",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Перейти",
                            url=f"https://t.me/{payload.get('chat_username', '').lstrip('@')}",
                        )
                    ],
                    [InlineKeyboardButton("✅ Проверить", callback_data=f"verify_sub_{assignment_id}")],
                ]
            ),
        )
    elif task["type"] == "view":
        await query.edit_message_text(
            f"Открой пост: {payload.get('link')}\nНажми «Готово» после просмотра.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("✅ Готово", callback_data=f"complete_view_{assignment_id}")]]
            ),
        )
    else:
        await query.edit_message_text(
            f"Поставь реакцию {payload.get('reaction')} на сообщение: {payload.get('link')}\n"
            "Отправь скриншот ниже, чтобы получить награду.",
        )
        context.user_data["pending_proof"] = PendingProof(assignment_id=assignment_id, type="reaction")


async def verify_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    assignment_id = int(query.data.split("_")[2])
    assignment = get_assignment(assignment_id)
    if not assignment:
        await query.edit_message_text("Задание не найдено.")
        return
    task = get_task(assignment["task_id"])
    payload = json.loads(task["payload"])
    chat_username = payload.get("chat_username")
    try:
        member = await context.bot.get_chat_member(chat_id=chat_username, user_id=query.from_user.id)
        if member.status in ("left", "kicked"):
            raise ValueError("not_subscribed")
    except Exception:
        await query.edit_message_text("❌ Не вижу подписку. Убедись, что вступил.")
        return

    await reward_user(query.from_user.id, task["reward"], assignment_id, context)
    due_at = int(time.time() + 7 * 24 * 3600)
    watch_id = schedule_subscription_watch(
        user_id=query.from_user.id,
        chat_id=str(chat_username),
        reward=task["reward"],
        task_id=task["id"],
        due_at=due_at,
        stage="follow",
    )
    context.job_queue.run_once(check_subscription_job, when=timedelta(seconds=due_at - int(time.time())), data=watch_id)
    update_assignment_status(assignment_id, "approved", proof="subscription_ok")
    await query.edit_message_text(
        "✅ Подписка подтверждена! Награда зачислена. Не отписывайся 7 дней, иначе монеты спишутся."
    )


async def complete_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    assignment_id = int(query.data.split("_")[2])
    assignment = get_assignment(assignment_id)
    if not assignment:
        await query.edit_message_text("Задание не найдено.")
        return
    task = get_task(assignment["task_id"])
    await reward_user(query.from_user.id, task["reward"], assignment_id, context)
    update_assignment_status(assignment_id, "approved", proof="viewed")
    await query.edit_message_text("✅ Награда зачислена!")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    proof: Optional[PendingProof] = context.user_data.get("pending_proof")
    if not proof:
        return
    file_id = update.message.photo[-1].file_id
    assignment = get_assignment(proof.assignment_id)
    if not assignment:
        await update.message.reply_text("Задание не найдено.")
        return
    task = get_task(assignment["task_id"])
    update_assignment_status(proof.assignment_id, "submitted", proof=file_id)
    context.user_data.pop("pending_proof", None)
    await update.message.reply_text("📸 Скриншот отправлен на проверку рекламодателю.")
    if task["created_by"]:
        buttons = [
            [
                InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_{proof.assignment_id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{proof.assignment_id}"),
            ],
            [
                InlineKeyboardButton(
                    "✏️ На доработку", callback_data=f"rework_{proof.assignment_id}"
                ),
            ],
        ]
        try:
            await context.bot.send_photo(
                chat_id=task["created_by"],
                photo=file_id,
                caption=f"Проверка реакции для задания #{task['id']} от пользователя {assignment['user_id']}",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Cannot notify creator: %s", exc)


async def approve_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    assignment_id = int(query.data.split("_")[1])
    assignment = get_assignment(assignment_id)
    if not assignment:
        await query.edit_message_caption("Задание не найдено.")
        return
    task = get_task(assignment["task_id"])
    await reward_user(assignment["user_id"], task["reward"], assignment_id, context)
    update_assignment_status(assignment_id, "approved")
    await query.edit_message_caption("✅ Одобрено. Награда отправлена исполнителю.")


async def reject_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    assignment_id = int(query.data.split("_")[1])
    update_assignment_status(assignment_id, "rejected", comment="Отклонено")
    await query.edit_message_caption("❌ Отклонено.")
    assignment = get_assignment(assignment_id)
    if assignment:
        await context.bot.send_message(chat_id=assignment["user_id"], text="❌ Ваш скрин отклонён.")


async def rework_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    assignment_id = int(query.data.split("_")[1])
    update_assignment_status(assignment_id, "needs_work", comment="Нужно исправить")
    await query.edit_message_caption("🔄 Отправлено на доработку.")
    assignment = get_assignment(assignment_id)
    if assignment:
        await context.bot.send_message(
            chat_id=assignment["user_id"], text="🔄 Требуется доработка. Отправьте новый скриншот."
        )


async def reward_user(user_id: int, reward: int, assignment_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    increment_balance(user_id, reward)
    user_row = get_user(user_id)
    if user_row and user_row["referrer_id"]:
        ref_bonus = math.floor(reward * 0.15)
        increment_balance(user_row["referrer_id"], ref_bonus)
        try:
            await context.bot.send_message(
                chat_id=user_row["referrer_id"],
                text=f"💎 Ваш реферал выполнил задание. +{ref_bonus} монет.",
            )
        except Exception:  # noqa: BLE001
            pass
    if user_row and user_row["balance"] >= 5000:
        update_level(user_id, max(user_row["level"], 2))


async def create_task_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    buttons = [
        [
            InlineKeyboardButton("👥 Подписка", callback_data="newtask_subscribe"),
            InlineKeyboardButton("👁 Просмотр", callback_data="newtask_view"),
        ],
        [InlineKeyboardButton("❤️ Реакция + скрин", callback_data="newtask_reaction")],
    ]
    await update.effective_message.reply_text(
        "Выберите тип задания:", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def newtask_choose(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, task_type = query.data.split("_")
    context.user_data["newtask"] = {"type": task_type}
    await query.edit_message_text(
        "Отправьте заголовок и описание через перенос строки.\n\nПример:\n"
        "Подпишись на канал\nКороткое описание задания.",
    )


async def newtask_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if "newtask" not in context.user_data:
        return
    text = update.effective_message.text or ""
    parts = text.split("\n", 1)
    title = parts[0][:80]
    description = parts[1] if len(parts) > 1 else ""
    context.user_data["newtask"]["title"] = title
    context.user_data["newtask"]["description"] = description
    task_type = context.user_data["newtask"]["type"]
    if task_type == "subscribe":
        await update.effective_message.reply_text(
            "Отправьте username канала/группы (например, @mychannel), куда нужно подписаться."
        )
    else:
        await update.effective_message.reply_text(
            "Отправьте ссылку на сообщение или chat_id:message_id, на котором нужно оставить реакцию/просмотр."
        )


async def newtask_payload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if "newtask" not in context.user_data or "title" not in context.user_data["newtask"]:
        return
    payload_text = update.effective_message.text.strip()
    context.user_data["newtask"]["payload_text"] = payload_text
    task_type = context.user_data["newtask"]["type"]
    if task_type == "reaction":
        await update.effective_message.reply_text("Укажите какую реакцию нужно поставить (например ❤️).")
    else:
        await update.effective_message.reply_text(
            f"Укажите награду (минимум {MIN_REWARD.get(task_type, 0)} монет)."
        )


async def newtask_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if "newtask" not in context.user_data or context.user_data["newtask"]["type"] != "reaction":
        return
    reaction = (update.effective_message.text or "").strip() or "❤️"
    context.user_data["newtask"]["reaction"] = reaction
    await update.effective_message.reply_text(f"Укажите награду (минимум {MIN_REWARD['reaction']} монет).")


async def newtask_reward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if "newtask" not in context.user_data:
        return
    reward_text = update.effective_message.text.strip()
    if not reward_text.isdigit():
        await update.effective_message.reply_text("Награда должна быть числом.")
        return
    reward = int(reward_text)
    task_type = context.user_data["newtask"]["type"]
    if reward < MIN_REWARD.get(task_type, 0):
        await update.effective_message.reply_text(
            f"Минимальная награда для этого типа: {MIN_REWARD[task_type]}"
        )
        return
    data = context.user_data["newtask"]
    payload: Dict[str, str] = {}
    if task_type == "subscribe":
        payload["chat_username"] = data["payload_text"].lstrip("@")
    else:
        payload["link"] = data["payload_text"]
    if task_type == "reaction":
        payload["reaction"] = data.get("reaction", "❤️")
    task_id = create_task(
        task_type=task_type,
        title=data["title"],
        description=data.get("description", ""),
        reward=reward,
        payload=payload,
        created_by=update.effective_user.id,
    )
    context.user_data.pop("newtask", None)
    await update.effective_message.reply_text(f"✅ Задание #{task_id} создано.")


async def promo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("Введите промокод:")
    context.user_data["awaiting_promo"] = True


async def promo_apply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data.get("awaiting_promo"):
        return
    code = update.effective_message.text.strip()
    ok, message, reward = redeem_promocode(update.effective_user.id, code)
    context.user_data.pop("awaiting_promo", None)
    text = f"{message}"
    if ok:
        text += f" +{reward} монет."
    await update.effective_message.reply_text(text)


async def topup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Введите сумму в монетах, которую хотите пополнить.\n"
        "1000 монет = $0.05. Оплата через CryptoPay. Также можно включить Stars в @BotFather."
    )
    context.user_data["awaiting_topup"] = True


async def topup_create(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data.get("awaiting_topup"):
        return
    context.user_data.pop("awaiting_topup", None)
    amount_str = update.effective_message.text.strip()
    if not amount_str.isdigit():
        await update.effective_message.reply_text("Введите число монет.")
        return
    coins = int(amount_str)
    usd_amount = round(coins * COIN_TO_USD, 2)
    if not CRYPTOPAY_TOKEN:
        await update.effective_message.reply_text(
            f"USD сумма: {usd_amount}. Настройте CRYPTOPAY_TOKEN, чтобы выдавать платёжные ссылки."
        )
        return
    url = await create_cryptopay_invoice(usd_amount, coins)
    await update.effective_message.reply_text(
        f"Оплатите по ссылке {url}\nПосле оплаты нажмите /start, чтобы баланс обновился.",
        disable_web_page_preview=True,
    )


async def create_cryptopay_invoice(usd_amount: float, coins: int) -> str:
    payload = {"asset": "USDT", "amount": usd_amount, "currency_type": "fiat", "description": f"{coins} coins"}
    headers = {"Crypto-Pay-API-Token": CRYPTOPAY_TOKEN}
    async with aiohttp.ClientSession() as session:
        async with session.post("https://pay.crypt.bot/api/createInvoice", json=payload, headers=headers) as resp:
            data = await resp.json()
            if not data.get("ok"):
                raise RuntimeError(f"CryptoPay error: {data}")
            return data["result"]["pay_url"]


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return
    stats = get_stats()
    buttons = [
        [InlineKeyboardButton("👥 Спонсоры", callback_data="admin_sponsors")],
        [InlineKeyboardButton("🎟 Промокод", callback_data="admin_promo")],
        [InlineKeyboardButton("📨 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton("🔍 Баланс по ID", callback_data="admin_balance")],
    ]
    await update.effective_message.reply_text(
        f"📊 Статистика\nПользователей: {stats['users']}\nЗаданий: {stats['tasks']}\n"
        f"Суммарный баланс: {stats['balance_sum']} монет",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    action = query.data.split("_", 1)[1]
    if action == "sponsors":
        sponsors = list_sponsors()
        text = "Текущие спонсоры:\n" + "\n".join(
            [f"{s['title']} ({s['chat_id']})" for s in sponsors]
        ) if sponsors else "Нет спонсоров."
        await query.edit_message_text(
            text + "\n\nОтправьте chat_id и название через точку с запятой для добавления.\n"
            "Пример: -100123456;Мой канал\nЧтобы удалить, отправьте: delete;-100123456",
        )
        context.user_data["awaiting_sponsor"] = True
    elif action == "promo":
        await query.edit_message_text(
            "Отправьте код;награда;YYYY-MM-DD;лимит_использований (лимит и дата можно пустыми)"
        )
        context.user_data["awaiting_admin_promo"] = True
    elif action == "broadcast":
        await query.edit_message_text("Отправьте текст рассылки.")
        context.user_data["awaiting_broadcast"] = True
    elif action == "balance":
        await query.edit_message_text("Введите ID пользователя.")
        context.user_data["awaiting_balance_id"] = True


async def admin_messages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.effective_message.text.strip()
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        return
    if context.user_data.pop("awaiting_sponsor", False):
        if text.startswith("delete;"):
            chat_id = int(text.split(";", 1)[1])
            remove_sponsor(chat_id)
            await update.effective_message.reply_text("Удалено.")
        else:
            chat_id_str, title = text.split(";", 1)
            add_sponsor(int(chat_id_str), title)
            await update.effective_message.reply_text("Добавлено.")
    elif context.user_data.pop("awaiting_admin_promo", False):
        code, reward_str, *rest = text.split(";")
        expires = rest[0] if rest and rest[0] else None
        uses_left = int(rest[1]) if len(rest) > 1 and rest[1] else None
        create_promocode(code, int(reward_str), expires, uses_left)
        await update.effective_message.reply_text("Промокод создан.")
    elif context.user_data.pop("awaiting_broadcast", False):
        await broadcast(text, context)
        await update.effective_message.reply_text("Рассылка отправлена.")
    elif context.user_data.pop("awaiting_balance_id", False):
        if text.isdigit():
            user_row = get_user(int(text))
            await update.effective_message.reply_text(
                f"Баланс: {user_row['balance'] if user_row else 'нет данных'}"
            )


async def broadcast(text: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Читаем всех пользователей напрямую из БД
    import sqlite3

    conn = sqlite3.connect("data.db")
    cur = conn.cursor()
    cur.execute("SELECT id FROM users")
    ids = [row[0] for row in cur.fetchall()]
    conn.close()
    for uid in ids:
        try:
            await context.bot.send_message(chat_id=uid, text=text)
        except Exception:  # noqa: BLE001
            continue


async def check_subscription_job(context: CallbackContext) -> None:
    watch_id = context.job.data
    watch = get_subscription_watch(watch_id)
    if not watch:
        return
    user_id = watch["user_id"]
    chat_id = watch["chat_id"]
    task_id = watch["task_id"]
    try:
        member = await context.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        if member.status in ("left", "kicked"):
            raise ValueError("left")
    except Exception:
        increment_balance(user_id, -watch["reward"])
        await context.bot.send_message(
            chat_id=user_id,
            text="❌ Вы отписались раньше 7 дней. Награда списана. "
            "У вас есть 1 час, чтобы снова подписаться, иначе баланс уйдёт в минус.",
        )
    delete_subscription_watch(watch_id)


def reschedule_watches(application: Application) -> None:
    now = int(time.time())
    for watch in list_subscription_watches():
        delay = max(0, watch["due_at"] - now)
        application.job_queue.run_once(check_subscription_job, when=timedelta(seconds=delay), data=watch["id"])


async def check_sponsors_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if await ensure_sponsors(update, context):
        await query.edit_message_text("Спасибо! Доступ открыт.")
        await send_main_menu(update)


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.effective_message.text
    if text == "👤 Профиль":
        await profile(update, context)
    elif text == "📝 Задания":
        await list_tasks_handler(update, context)
    elif text == "💸 Создать задание":
        await create_task_start(update, context)
    elif text == "🎯 Промокод":
        await promo(update, context)
    elif text == "💰 Пополнить баланс":
        await topup(update, context)
    elif text == "📊 Админ панель":
        await admin_panel(update, context)
    else:
        # fallback for conversations
        await newtask_title(update, context)
        await newtask_payload(update, context)
        await newtask_reaction(update, context)
        await newtask_reward(update, context)
        await promo_apply(update, context)
        await topup_create(update, context)
        await admin_messages(update, context)


def build_application() -> Application:
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN не задан.")
    application = ApplicationBuilder().token(TOKEN).rate_limiter(AIORateLimiter()).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("profile", profile))
    application.add_handler(CommandHandler("tasks", list_tasks_handler))
    application.add_handler(CommandHandler("create_task", create_task_start))
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(CallbackQueryHandler(task_detail, pattern=r"^task_"))
    application.add_handler(CallbackQueryHandler(take_task, pattern=r"^take_"))
    application.add_handler(CallbackQueryHandler(verify_subscription, pattern=r"^verify_sub_"))
    application.add_handler(CallbackQueryHandler(complete_view, pattern=r"^complete_view_"))
    application.add_handler(CallbackQueryHandler(approve_reaction, pattern=r"^approve_"))
    application.add_handler(CallbackQueryHandler(reject_reaction, pattern=r"^reject_"))
    application.add_handler(CallbackQueryHandler(rework_reaction, pattern=r"^rework_"))
    application.add_handler(CallbackQueryHandler(newtask_choose, pattern=r"^newtask_"))
    application.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^admin_"))
    application.add_handler(CallbackQueryHandler(check_sponsors_button, pattern=r"^check_sponsors$"))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    return application


def main() -> None:
    init_db()
    application = build_application()
    reschedule_watches(application)
    logger.info("Bot started")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

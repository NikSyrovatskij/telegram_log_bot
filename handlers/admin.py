import os
import csv
import html
import logging
import asyncio
import uuid
import time
import zipfile
from datetime import timedelta, datetime
from aiogram import Router, F, types, Bot
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    FSInputFile, ReplyKeyboardMarkup, KeyboardButton, 
    InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery,
    CopyTextButton
)

from sqlalchemy import select, func, desc, and_, or_
from database.engine import Session
from database.models import MsgLog, Conn, Settings, UserAccount, PaymentRecord
from yookassa import Configuration, Payment

router = Router()
logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_ID"))
PAGE_SIZE = 10
START_PHOTO_PATH = "start_photo.jpg"
START_ATTEMPTS = int(os.getenv("START_ATTEMPTS", 10))
MEDIA_DIR = "media"
ARCHIVE_HISTORY_FILE = os.path.join(MEDIA_DIR, ".archived.txt")

Configuration.account_id = os.getenv("YOOKASSA_SHOP_ID")
Configuration.secret_key = os.getenv("YOOKASSA_SECRET_KEY")

# Цены в копейках из .env
PRICE_30_KOPECKS = int(os.getenv("PRICE_30_DAYS", 100))
PRICE_60_KOPECKS = int(os.getenv("PRICE_60_DAYS", 170))

class AdminStates(StatesGroup):
    waiting_for_attempts = State()
    waiting_for_broadcast_all = State()
    waiting_for_broadcast_one = State()
    waiting_for_search = State()
    waiting_for_msg_search = State()
    waiting_for_chat_search = State()

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def get_price_rub(kopecks):
    return kopecks // 100

def fmt_user_info(name, username, user_id=None, is_paid=False):
    mark = "⭐" if is_paid else "👤"
    safe_name = html.escape(name or "???")
    un = f"@{html.escape(username)} " if username else ""
    return f"{mark} {un}({safe_name})" + (f" [ID:{user_id}]" if user_id else "")

async def safe_edit_or_answer(message: Message, text: str, reply_markup=None, parse_mode=None):
    try:
        return await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception as e:
        err_str = str(e).lower()
        if "message is not modified" in err_str:
            return message
        try:
            await message.delete()
        except Exception:
            pass
        return await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)

async def get_interlocutor_info(session, owner_id, chat_id):
    res = await session.execute(
        select(MsgLog.from_name, MsgLog.from_username)
        .where(MsgLog.owner_id == owner_id, MsgLog.chat_id == chat_id, MsgLog.from_id == chat_id)
        .order_by(desc(MsgLog.created_at)).limit(1)
    )
    data = res.first()
    return fmt_user_info(data[0], data[1], chat_id) if data else f"ID: {chat_id}"

# --- КЛАВИАТУРЫ ---

def get_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📈 Статистика"), KeyboardButton(text="🛠 Настройки бота")],
        [KeyboardButton(text="👥 Пользователи"), KeyboardButton(text="🔍 Поиск юзера")],
        [KeyboardButton(text="💬 Поиск в переписке"), KeyboardButton(text="🔍 Список логов")],
        [KeyboardButton(text="📥 Экспорт всей базы (CSV)"), KeyboardButton(text="📦 Архив медиа")]
    ], resize_keyboard=True)

def get_export_period_kb(action_prefix: str, back_cb: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⏱ За 1 час", callback_data=f"{action_prefix}:1"),
            InlineKeyboardButton(text="⏱ За 3 часа", callback_data=f"{action_prefix}:3"),
        ],
        [
            InlineKeyboardButton(text="⏱ За 6 часов", callback_data=f"{action_prefix}:6"),
            InlineKeyboardButton(text="⏱ За 24 часа", callback_data=f"{action_prefix}:24"),
        ],
        [
            InlineKeyboardButton(text="⏱ За 3 суток", callback_data=f"{action_prefix}:72"),
            InlineKeyboardButton(text="♾ За всё время", callback_data=f"{action_prefix}:all"),
        ],
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb)
        ]
    ])

def get_admin_settings_kb(global_notify):
    status = "✅ ВКЛ" if global_notify else "❌ ВЫКЛ"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Уведомления логов: {status}", callback_data="toggle_global_notify")],
        [InlineKeyboardButton(text="📢 Рассылка сообщений", callback_data="admin_broadcast_menu")],
        [InlineKeyboardButton(text="🤝 Рефералы", callback_data="admin_refs")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")]
    ])

def get_user_manage_kb(user_id, daily_status, attempts, is_paid):
    att_text = "Бесконечно ⭐" if is_paid else f"{attempts} шт."
    daily_text = "✅ Авто-экспорт: ВКЛ" if daily_status else "❌ Авто-экспорт: ВЫКЛ"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Список чатов", callback_data=f"owner:{user_id}:0")],
        [InlineKeyboardButton(text="✉️ Написать сообщение", callback_data=f"send_to:{user_id}")],
        [InlineKeyboardButton(text=f"💎 Попытки: {att_text}", callback_data=f"edit_att:{user_id}")],
        [InlineKeyboardButton(text=daily_text, callback_data=f"u_toggle_daily:{user_id}")],
        [InlineKeyboardButton(text="📥 Экспорт истории (CSV)", callback_data=f"u_export:{user_id}")],
        [InlineKeyboardButton(text="⬅️ Назад к списку", callback_data="own_pg:0")]
    ])

def get_client_settings_kb(acc: UserAccount):
    edit_status = "✅ ВКЛ" if acc.notify_edits else "❌ ВЫКЛ"
    del_status = "✅ ВКЛ" if acc.notify_deletes else "❌ ВЫКЛ"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Уведомление о правке: {edit_status}", callback_data="toggle_u:edits")],
        [InlineKeyboardButton(text=f"Уведомление об удалении: {del_status}", callback_data="toggle_u:deletes")],
        [InlineKeyboardButton(text=f"⭐ Подписка 30 дней ({get_price_rub(PRICE_30_KOPECKS)}₽)", callback_data="buy_premium:30")],
        [InlineKeyboardButton(text=f"⭐ Подписка 60 дней ({get_price_rub(PRICE_60_KOPECKS)}₽)", callback_data="buy_premium:60")]
    ])

# --- ОПЛАТА ЮKASSA ---

@router.callback_query(F.data.startswith("buy_premium:"))
async def buy_premium_process(call: CallbackQuery, bot: Bot):
    await call.answer()
    if not Configuration.account_id or not Configuration.secret_key:
        return await call.message.answer("❌ Оплата временно недоступна. Обратитесь https://t.me/smodmsg ")
    
    days = int(call.data.split(":")[1])
    
    # ИСПРАВЛЕНО: Правильная проверка дней и расчет цены
    price_kopecks = PRICE_30_KOPECKS if days == 30 else PRICE_60_KOPECKS
    price_rub = price_kopecks / 100
    
    idempotence_key = str(uuid.uuid4())
    try:
        payment = await asyncio.to_thread(Payment.create, {
            "amount": {"value": f"{price_rub:.2f}", "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": f"https://t.me/{(await bot.get_me()).username}"},
            "capture": True, "description": f"Подписка Premium на {days} дней"
        }, idempotence_key)
    except Exception as e:
        logger.error(f"YooKassa Create Error: {e}")
        return await call.message.answer("❌ Ошибка при создании платежа. Обратитесь в поддержку https://t.me/smodmsg.")

    async with Session() as session:
        session.add(PaymentRecord(user_id=call.from_user.id, payment_id=payment.id, days=days))
        await session.commit()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить (СБП / Карта)", url=payment.confirmation.confirmation_url)],
        [InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"check_pay:{payment.id}")]
    ])
    await call.message.answer(f"🧾 <b>Счет на оплату</b>\n\nТариф: <b>Premium на {days} дней</b>\nСумма: <b>{int(price_rub)} руб.</b>\n\nНажмите кнопку ниже для перехода к оплате ЮKassa. После оплаты нажмите «Проверить оплату».", reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data.startswith("check_pay:"))
async def check_payment_status(call: CallbackQuery):
    payment_id = call.data.split(":")[1]
    try: payment = await asyncio.to_thread(Payment.find_one, payment_id)
    except Exception as e: return await call.answer("Ошибка при проверке платежа. Обратитесь https://t.me/smodmsg", show_alert=True)

    if payment.status == "succeeded":
        async with Session() as session:
            res = await session.execute(select(PaymentRecord).where(PaymentRecord.payment_id == payment_id, PaymentRecord.status == "pending"))
            db_payment = res.scalar_one_or_none()
            if db_payment:
                db_payment.status = "succeeded"
                acc = await session.get(UserAccount, call.from_user.id)
                if acc:
                    now = datetime.now()
                    if acc.subscription_until and acc.subscription_until > now: acc.subscription_until += timedelta(days=db_payment.days)
                    else: acc.subscription_until = now + timedelta(days=db_payment.days)
                await session.commit()
                await call.message.edit_text(f"🎉 <b>Оплата прошла успешно!</b>\nВам начислен статус <b>Premium ⭐</b> на {db_payment.days} дней.", parse_mode="HTML")
            else: await call.answer("Оплата уже была зачислена ранее.", show_alert=True)
    elif payment.status == "canceled": await call.message.edit_text("❌ Платеж был отменен или время ожидания истекло.")
    else: await call.answer("⏳ Платеж еще не подтвержден. Если вы уже оплатили, подождите минуту и нажмите снова или обратитесь https://t.me/smodmsg", show_alert=True)

# --- ОБРАБОТКА /START ---

@router.message(Command("start"))
async def cmd_start(m: types.Message, bot: Bot, command: CommandObject, state: FSMContext):
    await state.clear()
    async with Session() as session:
        acc = await session.get(UserAccount, m.from_user.id)
        if not acc:
            ref_id = int(command.args) if command.args and command.args.isdigit() else None
            if ref_id == m.from_user.id: ref_id = None
            session.add(UserAccount(user_id=m.from_user.id, attempts=START_ATTEMPTS, referrer_id=ref_id))
            await session.commit()

    if m.from_user.id == ADMIN_ID:
        return await m.answer("🕵️‍♂️ Кабинет админа активен!", reply_markup=get_kb())
    
    bot_info = await bot.get_me()
    username = f"@{bot_info.username}"
    
    welcome_text = (
        "<b>Подключите бота к аккаунту, чтобы он мог помочь вам в переписке в нужный момент.</b>\n\n"
        f"Для подключения используйте:\n<code>{username}</code>"
    )
    
    username_to_copy = f"@{bot_info.username}"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Скопировать @username", copy_text=CopyTextButton(text=username_to_copy))],
            [InlineKeyboardButton(text="Подробная инструкция", url=f"https://telegra.ph/Instrukciya-po-podklyucheniyu-i-nastrojke-ModMsgbot-08-24")]
        ]
    )

    if os.path.exists(START_PHOTO_PATH):
        await m.answer_photo(photo=FSInputFile(START_PHOTO_PATH), caption=welcome_text, reply_markup=kb, parse_mode="HTML")
    else:
        await m.answer(welcome_text, reply_markup=kb, parse_mode="HTML")

# --- ЛИЧНЫЕ НАСТРОЙКИ КЛИЕНТА ---

@router.message(Command("settings", "setting"))
async def cmd_settings(m: types.Message, state: FSMContext):
    await state.clear()
    if m.from_user.id == ADMIN_ID:
        return await admin_settings_main(m)
        
    async with Session() as session:
        acc = await session.get(UserAccount, m.from_user.id)
        if not acc: return await m.answer("Нажмите /start для регистрации.")
        
        is_paid = acc.subscription_until and acc.subscription_until > datetime.now()
        status_text = f"Premium ⭐ (до {acc.subscription_until.strftime('%d.%m.%Y')})" if is_paid else "Базовый 👤"
        att_text = "Бесконечно ⭐" if is_paid else f"{acc.attempts} шт."
        
        text = f"⚙️ <b>Ваши настройки</b>\n\nСтатус: <b>{status_text}</b>\nОсталось попыток: <b>{att_text}</b>"
        await m.answer(text, reply_markup=get_client_settings_kb(acc), parse_mode="HTML")

@router.callback_query(F.data.startswith("toggle_u:"))
async def toggle_user_notif(call: CallbackQuery):
    action = call.data.split(":")[1]
    async with Session() as session:
        acc = await session.get(UserAccount, call.from_user.id)
        if action == "edits": acc.notify_edits = not acc.notify_edits
        else: acc.notify_deletes = not acc.notify_deletes
        await session.commit()
        await call.message.edit_reply_markup(reply_markup=get_client_settings_kb(acc))
        await call.answer("Настройки обновлены")

@router.message(Command("ref", "referral"))
async def cmd_ref(m: types.Message, bot: Bot, state: FSMContext):
    await state.clear()
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={m.from_user.id}"
    await m.answer(f"🎁 <b>Ваша реферальная ссылка:</b>\n<code>{ref_link}</code>\n\nЗа каждого друга вы получите <b>+1</b> попыток!", parse_mode="HTML")

# --- АДМИНКА: ПОИСК ПОЛЬЗОВАТЕЛЯ ---

@router.message(F.text.in_(["🔍 Поиск", "🔍 Поиск юзера"]), F.from_user.id == ADMIN_ID)
async def search_user_start(m: types.Message, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_search)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_main")]])
    await m.answer("🔎 Введите ID или @username пользователя:", reply_markup=kb)

@router.message(AdminStates.waiting_for_search, F.from_user.id == ADMIN_ID)
async def search_user_exec(m: types.Message, state: FSMContext):
    query = m.text.replace("@", "").strip()
    async with Session() as session:
        if query.isdigit():
            res = await session.execute(select(Conn).where(Conn.user_id == int(query)))
        else:
            res = await session.execute(select(Conn).where(Conn.username == query))
        
        c = res.scalars().first()
        if c:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="👤 Управление", callback_data=f"u_menu:{c.user_id}")]])
            await m.answer(f"✅ <b>Найден:</b> {fmt_user_info(c.full_name, c.username, c.user_id)}", reply_markup=kb, parse_mode="HTML")
        else:
            await m.answer("❌ Пользователь не найден в базе.")
    await state.clear()

# --- АДМИНКА: ПОИСК ПО СООБЩЕНИЯМ ---

@router.message(F.text.contains("Поиск в переписке"), F.from_user.id == ADMIN_ID)
async def start_msg_search(m: types.Message, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_msg_search)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_main")]])
    await m.answer(
        "🔎 <b>Поиск по тексту сообщений</b>\n\n"
        "Введите слово или фразу для поиска (минимум 3 символа):\n"
        "<i>Поиск ведётся по всей базе сохранённых сообщений.</i>",
        reply_markup=kb,
        parse_mode="HTML"
    )

@router.callback_query(F.data == "search_msgs_again", F.from_user.id == ADMIN_ID)
async def search_msgs_again_cb(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await start_msg_search(call.message, state)

@router.message(AdminStates.waiting_for_msg_search, F.from_user.id == ADMIN_ID)
async def search_msg_exec(m: types.Message, state: FSMContext):
    query = (m.text or "").strip()
    if len(query) < 3:
        return await m.answer("⚠️ Введите минимум 3 символа для поиска:")
    
    await state.clear()
    async with Session() as session:
        stmt = (
            select(MsgLog)
            .where(MsgLog.text.ilike(f"%{query}%"))
            .order_by(desc(MsgLog.created_at))
            .limit(10)
        )
        res = await session.execute(stmt)
        results = res.scalars().all()
        
        if not results:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔎 Искать снова", callback_data="search_msgs_again")],
                [InlineKeyboardButton(text="⬅️ В меню", callback_data="back_to_main")]
            ])
            return await m.answer(f"❌ По запросу «<b>{html.escape(query)}</b>» ничего не найдено.", reply_markup=kb, parse_mode="HTML")
        
        text = f"🔎 <b>Найдено {len(results)} сообщений</b> (по запросу «<i>{html.escape(query)}</i>»):\n\n"
        kb = InlineKeyboardMarkup(inline_keyboard=[])
        
        for idx, log in enumerate(results, 1):
            time_str = (log.created_at + timedelta(hours=3)).strftime("%d.%m %H:%M")
            sender = f"@{log.from_username}" if log.from_username else (log.from_name or f"ID:{log.from_id}")
            snippet = (log.text[:80] + "...") if len(log.text) > 80 else log.text
            text += (
                f"<b>{idx}.</b> 🕒 <code>{time_str}</code> | От: <b>{html.escape(sender)}</b>\n"
                f"   Чат ID: <code>{log.chat_id}</code> | Аккаунт: <code>{log.owner_id}</code>\n"
                f"   └ <i>«{html.escape(snippet)}»</i>\n\n"
            )
            kb.inline_keyboard.append([
                InlineKeyboardButton(
                    text=f"💬 Открыть чат #{idx} ({sender[:15]})",
                    callback_data=f"chat:{log.owner_id}:{log.chat_id}"
                )
            ])
            
        kb.inline_keyboard.append([
            InlineKeyboardButton(text="🔎 Искать ещё", callback_data="search_msgs_again"),
            InlineKeyboardButton(text="⬅️ В главное меню", callback_data="back_to_main")
        ])
        await m.answer(text, reply_markup=kb, parse_mode="HTML")

# --- АДМИНКА: НАСТРОЙКИ И РАССЫЛКА ---

@router.message(F.text == "🛠 Настройки бота", F.from_user.id == ADMIN_ID)
async def admin_settings_main(m: types.Message):
    async with Session() as session:
        res = await session.execute(select(Settings).where(Settings.id == 1))
        sett = res.scalars().first()
        if not sett:
            sett = Settings(id=1, global_notify=False)
            session.add(sett)
            await session.commit()
        await m.answer("⚙️ <b>Настройки управления ботом:</b>", reply_markup=get_admin_settings_kb(sett.global_notify), parse_mode="HTML")

@router.callback_query(F.data == "toggle_global_notify", F.from_user.id == ADMIN_ID)
async def toggle_global_notify(call: CallbackQuery):
    async with Session() as session:
        res = await session.execute(select(Settings).where(Settings.id == 1))
        sett = res.scalars().first()
        sett.global_notify = not sett.global_notify
        await session.commit()
        await call.message.edit_reply_markup(reply_markup=get_admin_settings_kb(sett.global_notify))

@router.callback_query(F.data == "admin_broadcast_menu", F.from_user.id == ADMIN_ID)
async def broadcast_menu(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    info_id = data.get('broadcast_preview_info_id')
    if info_id:
        try:
            await call.bot.delete_message(chat_id=call.message.chat.id, message_id=info_id)
        except Exception:
            pass
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌍 Всем пользователям", callback_data="broadcast:all")],
        [InlineKeyboardButton(text="👤 Одному пользователю", callback_data="broadcast:one:0")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")]
    ])
    await safe_edit_or_answer(call.message, "Выберите тип рассылки:", reply_markup=kb)

@router.callback_query(F.data == "broadcast:all", F.from_user.id == ADMIN_ID)
async def broadcast_all_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_broadcast_all)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="admin_broadcast_menu")]])
    await safe_edit_or_answer(call.message, "📝 Введите сообщение для рассылки ВСЕМ (можно с фото/видео):", reply_markup=kb)
    await call.answer()

@router.message(AdminStates.waiting_for_broadcast_all, F.from_user.id == ADMIN_ID)
async def broadcast_all_preview(m: types.Message, state: FSMContext):
    await state.update_data(broadcast_msg_id=m.message_id, broadcast_from_chat=m.chat.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить и отправить ВСЕМ", callback_data="broadcast_confirm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_broadcast_menu")]
    ])
    async with Session() as session:
        count = (await session.execute(select(func.count(UserAccount.user_id)))).scalar() or 0
        
    info_msg = await m.answer(
        f"⚠️ <b>Подтверждение массовой рассылки</b>\n\n"
        f"🌍 Получатели: <b>ВСЕ пользователи ({count} чел.)</b>\n\n"
        f"<i>Предпросмотр сообщения ниже:</i>",
        parse_mode="HTML"
    )
    copy_msg = await m.send_copy(chat_id=m.chat.id, reply_markup=kb)
    await state.update_data(broadcast_preview_info_id=info_msg.message_id)

@router.callback_query(F.data == "broadcast_confirm", F.from_user.id == ADMIN_ID)
async def broadcast_all_exec(call: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    msg_id = data.get('broadcast_msg_id')
    from_chat = data.get('broadcast_from_chat')
    info_id = data.get('broadcast_preview_info_id')
    if not msg_id: return await call.answer("Ошибка: сообщение не найдено.", show_alert=True)
        
    try: await call.message.delete()
    except: pass
    if info_id:
        try: await bot.delete_message(chat_id=call.message.chat.id, message_id=info_id)
        except: pass
    
    status_msg = await call.message.answer("⏳ Начинаю рассылку...")
    async with Session() as session:
        res = await session.execute(select(UserAccount.user_id))
        users = res.scalars().all()
        count = 0
        for uid in users:
            try:
                await bot.copy_message(chat_id=uid, from_chat_id=from_chat, message_id=msg_id)
                count += 1
                await asyncio.sleep(0.05)
            except: pass
    await status_msg.edit_text(f"✅ Рассылка завершена. Успешно доставлено: {count} чел.")
    await state.clear()

@router.callback_query(F.data.startswith("broadcast:one:"), F.from_user.id == ADMIN_ID)
async def broadcast_one_list(call: CallbackQuery):
    page = int(call.data.split(":")[2])
    async with Session() as session:
        res = await session.execute(
            select(Conn.user_id).distinct().limit(PAGE_SIZE).offset(page * PAGE_SIZE)
        )
        user_ids = res.scalars().all()
        
        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for uid in user_ids:
            c_res = await session.execute(select(Conn).where(Conn.user_id == uid).order_by(desc(Conn.id)).limit(1))
            c = c_res.scalars().first()
            un_text = f"@{c.username}" if (c and c.username) else (c.full_name if c else "Пользователь")
            kb.inline_keyboard.append([InlineKeyboardButton(text=f"👤 {un_text} [{uid}]", callback_data=f"send_to:{uid}")])
            
        nav = []
        if page > 0: nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"broadcast:one:{page-1}"))
        if len(user_ids) == PAGE_SIZE: nav.append(InlineKeyboardButton(text="➡️", callback_data=f"broadcast:one:{page+1}"))
        if nav: kb.inline_keyboard.append(nav)
        
        kb.inline_keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_broadcast_menu")])
        await safe_edit_or_answer(call.message, "Выберите пользователя для личного сообщения:", reply_markup=kb)

@router.callback_query(F.data.startswith("send_to:"), F.from_user.id == ADMIN_ID)
async def broadcast_one_start(call: CallbackQuery, state: FSMContext):
    uid = int(call.data.split(":")[1])
    async with Session() as session:
        c_res = await session.execute(select(Conn).where(Conn.user_id == uid).order_by(desc(Conn.id)).limit(1))
        c = c_res.scalars().first()
        target_un = f"@{c.username}" if (c and c.username) else ""
        target_name = c.full_name if (c and c.full_name) else "Пользователь"
        target_label = f"{target_un} ({target_name}) [ID:{uid}]" if target_un else f"{target_name} [ID:{uid}]"
        
    await state.update_data(target_id=uid, target_label=target_label)
    await state.set_state(AdminStates.waiting_for_broadcast_one)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="admin_broadcast_menu")]])
    await safe_edit_or_answer(
        call.message,
        f"📝 Введите сообщение для <b>{target_label}</b>:\n\n<i>Можно отправить текст, фото или видео.</i>",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await call.answer()

@router.message(AdminStates.waiting_for_broadcast_one, F.from_user.id == ADMIN_ID)
async def broadcast_one_preview(m: types.Message, state: FSMContext):
    data = await state.get_data()
    uid = data.get('target_id')
    target_label = data.get('target_label', f"ID:{uid}")
    
    await state.update_data(broadcast_msg_id=m.message_id, broadcast_from_chat=m.chat.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить и отправить", callback_data="broadcast_one_confirm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_broadcast_menu")]
    ])
    
    info_msg = await m.answer(
        f"⚠️ <b>Подтверждение отправки</b>\n\n"
        f"👤 Получатель: <b>{target_label}</b>\n\n"
        f"<i>Предпросмотр отправляемого сообщения:</i>",
        parse_mode="HTML"
    )
    copy_msg = await m.send_copy(chat_id=m.chat.id, reply_markup=kb)
    await state.update_data(broadcast_preview_info_id=info_msg.message_id)

@router.callback_query(F.data == "broadcast_one_confirm", F.from_user.id == ADMIN_ID)
async def broadcast_one_exec(call: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    uid = data.get('target_id')
    target_label = data.get('target_label', f"ID:{uid}")
    msg_id = data.get('broadcast_msg_id')
    from_chat = data.get('broadcast_from_chat')
    info_id = data.get('broadcast_preview_info_id')
    if not msg_id or not uid: return await call.answer("Ошибка данных.", show_alert=True)
    
    try: await call.message.delete()
    except: pass
    if info_id:
        try: await bot.delete_message(chat_id=call.message.chat.id, message_id=info_id)
        except: pass
    
    status_msg = await call.message.answer(f"⏳ Отправляю сообщение получателю <b>{target_label}</b>...", parse_mode="HTML")
    try:
        await bot.copy_message(chat_id=uid, from_chat_id=from_chat, message_id=msg_id)
        await status_msg.edit_text(f"✅ Сообщение успешно доставлено: <b>{target_label}</b>.", parse_mode="HTML")
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка отправки: {e}")
    await state.clear()


@router.callback_query(F.data == "admin_refs", F.from_user.id == ADMIN_ID)
async def admin_list_refs(call: CallbackQuery):
    async with Session() as session:
        res = await session.execute(select(UserAccount).where(UserAccount.referrer_id != None))
        accounts = res.scalars().all()
        if not accounts: return await call.answer("Рефералов пока нет.", show_alert=True)
        
        text = "🤝 <b>Список приглашений:</b>\n\n"
        for acc in accounts:
            u_res = await session.execute(select(Conn).where(Conn.user_id == acc.user_id))
            u = u_res.scalars().first()
            invited = fmt_user_info(u.full_name, u.username, acc.user_id) if u else f"ID:{acc.user_id}"
            
            r_res = await session.execute(select(Conn).where(Conn.user_id == acc.referrer_id))
            r = r_res.scalars().first()
            inviter = fmt_user_info(r.full_name, r.username, acc.referrer_id) if r else f"ID:{acc.referrer_id}"
            text += f"👤 {inviter}\n ➡️ {invited}\n\n"
        
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_settings")]])
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")

# --- УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ ---

@router.message(F.text.contains("Пользователи"), F.from_user.id == ADMIN_ID)
async def list_owners_cmd(m: types.Message):
    await list_owners(m, 0)

async def list_owners(m, page: int):
    async with Session() as session:
        # Берем только уникальные user_id тех, кто реально подключил бизнес-бота
        total_res = await session.execute(select(func.count(func.distinct(Conn.user_id))))
        total_users = total_res.scalar() or 0
        
        res = await session.execute(
            select(Conn.user_id).distinct().limit(PAGE_SIZE).offset(page * PAGE_SIZE)
        )
        user_ids = res.scalars().all()
        
        if not user_ids and page == 0: 
            if isinstance(m, types.Message): return await m.answer("Подключённых пользователей нет.")
            else: return await m.edit_text("Подключённых пользователей нет.")
        
        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for uid in user_ids:
            c_res = await session.execute(select(Conn).where(Conn.user_id == uid).order_by(desc(Conn.id)).limit(1))
            c = c_res.scalars().first()
            
            res_acc = await session.execute(select(UserAccount).where(UserAccount.user_id == uid))
            acc = res_acc.scalars().first()
            
            is_paid = acc.subscription_until and acc.subscription_until > datetime.now() if acc else False
            name = c.full_name if c else "Пользователь"
            username = c.username if c else None
            
            btn_text = fmt_user_info(name, username, uid, is_paid=is_paid)
            kb.inline_keyboard.append([InlineKeyboardButton(text=btn_text, callback_data=f"u_menu:{uid}")])
        
        nav_btns = []
        if page > 0: nav_btns.append(InlineKeyboardButton(text="⬅️", callback_data=f"own_pg:{page-1}"))
        if len(user_ids) == PAGE_SIZE and (page + 1) * PAGE_SIZE < total_users:
            nav_btns.append(InlineKeyboardButton(text="➡️", callback_data=f"own_pg:{page+1}"))
        if nav_btns: kb.inline_keyboard.append(nav_btns)
        
        text = f"👥 <b>Список подключённых клиентов (Стр. {page+1}):</b>\nВсего клиентов: <code>{total_users}</code>"
        if isinstance(m, types.Message): await m.answer(text, reply_markup=kb, parse_mode="HTML")
        else: await m.edit_text(text, reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data.startswith("own_pg:"), F.from_user.id == ADMIN_ID)
async def owner_pagination(call: CallbackQuery):
    await call.answer(); await list_owners(call.message, int(call.data.split(":")[1]))

@router.callback_query(F.data.startswith("u_menu:"), F.from_user.id == ADMIN_ID)
async def admin_user_menu(call: CallbackQuery, state: FSMContext):
    if state: await state.clear()
    await call.answer()
    user_id = int(call.data.split(":")[1])
    async with Session() as session:
        res_acc = await session.execute(select(UserAccount).where(UserAccount.user_id == user_id))
        acc = res_acc.scalars().first()
        res_conn = await session.execute(select(Conn).where(Conn.user_id == user_id).order_by(desc(Conn.id)).limit(1))
        c = res_conn.scalars().first()
        if not acc and not c: return await call.answer("Данные не найдены")
        
        is_paid = acc.subscription_until and acc.subscription_until > datetime.now() if acc else False
        name = c.full_name if c else "Пользователь"
        username = c.username if c else None
        
        status_label = "Подключён (активен)" if (acc and acc.is_active) else "Отключён"
        attempts_count = acc.attempts if acc else 0
        daily_exp = acc.daily_export if acc else False
        
        text = f"👤 <b>Управление клиентом:</b>\n{fmt_user_info(name, username, user_id, is_paid)}\n\nСтатус бота: <b>{status_label}</b>"
        if is_paid and acc and acc.subscription_until:
            text += f"\n📅 Подписка до: {acc.subscription_until.strftime('%d.%m.%Y')}"
        
        await call.message.edit_text(text, reply_markup=get_user_manage_kb(user_id, daily_exp, attempts_count, is_paid), parse_mode="HTML")

@router.callback_query(F.data.startswith("u_toggle_daily:"), F.from_user.id == ADMIN_ID)
async def toggle_daily_export(call: CallbackQuery, state: FSMContext):
    user_id = int(call.data.split(":")[1])
    async with Session() as session:
        res = await session.execute(select(UserAccount).where(UserAccount.user_id == user_id))
        acc = res.scalars().first()
        if acc:
            acc.daily_export = not acc.daily_export
            await session.commit()
            await call.answer("Статус изменен")
            await admin_user_menu(call, state)

@router.callback_query(F.data.startswith("edit_att:"), F.from_user.id == ADMIN_ID)
async def admin_edit_att_start(call: CallbackQuery, state: FSMContext):
    user_id = int(call.data.split(":")[1])
    await state.update_data(target_user_id=user_id)
    await state.set_state(AdminStates.waiting_for_attempts)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=f"u_menu:{user_id}")]])
    await call.message.edit_text(f"🔢 Введите число попыток для <code>{user_id}</code>:", reply_markup=kb, parse_mode="HTML")
    await call.answer()

@router.message(AdminStates.waiting_for_attempts, F.from_user.id == ADMIN_ID)
async def admin_set_att_handler(m: types.Message, state: FSMContext):
    if not m.text or not m.text.isdigit(): return await m.answer("⚠️ Введите число.")
    new_count = int(m.text)
    data = await state.get_data()
    u_id = data.get("target_user_id")
    async with Session() as session:
        res = await session.execute(select(UserAccount).where(UserAccount.user_id == u_id))
        acc = res.scalars().first()
        if acc: 
            acc.attempts = new_count
            await session.commit()
            await m.answer(f"✅ Установлено попыток: {new_count} шт.")
    await state.clear()

# --- АДМИНКА: ЧАТЫ ---

@router.callback_query(F.data.startswith("owner:"), F.from_user.id == ADMIN_ID)
async def list_owner_chats(call: CallbackQuery):
    await call.answer()
    data = call.data.split(":")
    owner_id = int(data[1])
    page = int(data[2]) if len(data) > 2 else 0
    
    async with Session() as session:
        owner_res = await session.execute(select(Conn).where(Conn.user_id == owner_id))
        owner = owner_res.scalars().first()
        owner_info = fmt_user_info(owner.full_name, owner.username, owner_id) if owner else f"ID: {owner_id}"

        chat_ids_res = await session.execute(
            select(MsgLog.chat_id).where(MsgLog.owner_id == owner_id).distinct().limit(PAGE_SIZE).offset(page * PAGE_SIZE)
        )
        chat_ids = chat_ids_res.scalars().all()
        
        kb = InlineKeyboardMarkup(inline_keyboard=[])
        
        if not chat_ids:
            text = f"📂 <b>Чаты пользователя:</b>\n{owner_info}\n\n<i>Чатов пока нет.</i>"
        else:
            text = f"📂 <b>Чаты пользователя:</b>\n{owner_info}"
            for cid in chat_ids:
                if cid == owner_id:
                    c_btn_text = "💬 Избранное (Saved Messages)"
                else:
                    name_res = await session.execute(
                        select(MsgLog.from_name, MsgLog.from_username)
                        .where(MsgLog.owner_id == owner_id, MsgLog.chat_id == cid, MsgLog.from_id != owner_id)
                        .order_by(desc(MsgLog.created_at)).limit(1)
                    )
                    inter = name_res.first()
                    if inter and (inter[0] or inter[1]):
                        c_btn_text = f"💬 {fmt_user_info(inter[0], inter[1], cid)}"
                    else:
                        c_btn_text = f"💬 Чат ID: {cid}"
                    
                kb.inline_keyboard.append([InlineKeyboardButton(text=c_btn_text, callback_data=f"chat:{owner_id}:{cid}")])
        
        nav_btns = [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"u_menu:{owner_id}")]
        if page > 0: nav_btns.insert(0, InlineKeyboardButton(text="⬅️", callback_data=f"owner:{owner_id}:{page-1}"))
        if len(chat_ids) == PAGE_SIZE: nav_btns.append(InlineKeyboardButton(text="➡️", callback_data=f"owner:{owner_id}:{page+1}"))
        kb.inline_keyboard.append(nav_btns)
        
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data.startswith("chat:"), F.from_user.id == ADMIN_ID)
async def chat_menu(call: CallbackQuery):
    await call.answer()
    _, owner_id, chat_id = call.data.split(":")
    owner_id, chat_id = int(owner_id), int(chat_id)
    async with Session() as session: inter_info = await get_interlocutor_info(session, owner_id, chat_id)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📜 Сообщения", callback_data=f"msgs:{owner_id}:{chat_id}:0:all")],
        [InlineKeyboardButton(text="🖼 Медиа", callback_data=f"media:{owner_id}:{chat_id}:0:all")],
        [InlineKeyboardButton(text="🔎 Поиск по этому чату", callback_data=f"c_search:{owner_id}:{chat_id}")],
        [InlineKeyboardButton(text="📥 Экспорт чата (CSV)", callback_data=f"c_export:{owner_id}:{chat_id}")],
        [InlineKeyboardButton(text="⬅️ Назад к чатам", callback_data=f"owner:{owner_id}:0")]
    ])
    await call.message.edit_text(f"⚙️ <b>Управление чатом:</b>\n{inter_info}", reply_markup=kb, parse_mode="HTML")

# --- АДМИНКА: ПОИСК ВНУТРИ ЧАТА ---

@router.callback_query(F.data.startswith("c_search:"), F.from_user.id == ADMIN_ID)
async def chat_search_start(call: CallbackQuery, state: FSMContext):
    await call.answer()
    _, owner_id, chat_id = call.data.split(":")
    await state.update_data(c_search_owner=int(owner_id), c_search_chat=int(chat_id))
    await state.set_state(AdminStates.waiting_for_chat_search)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=f"chat:{owner_id}:{chat_id}")]])
    await call.message.edit_text(
        f"🔎 <b>Поиск по чату ID:{chat_id}</b>\n\n"
        "Введите слово или фразу (минимум 2 символа) для поиска в этом диалоге:",
        reply_markup=kb,
        parse_mode="HTML"
    )

@router.message(AdminStates.waiting_for_chat_search, F.from_user.id == ADMIN_ID)
async def chat_search_exec(m: types.Message, state: FSMContext):
    query = (m.text or "").strip()
    if len(query) < 2:
        return await m.answer("⚠️ Введите минимум 2 символа:")
        
    data = await state.get_data()
    owner_id = data.get("c_search_owner")
    chat_id = data.get("c_search_chat")
    await state.clear()
    
    async with Session() as session:
        stmt = (
            select(MsgLog)
            .where(MsgLog.owner_id == owner_id, MsgLog.chat_id == chat_id, MsgLog.text.ilike(f"%{query}%"))
            .order_by(desc(MsgLog.created_at))
            .limit(10)
        )
        res = await session.execute(stmt)
        results = res.scalars().all()
        
        if not results:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔎 Искать снова", callback_data=f"c_search:{owner_id}:{chat_id}")],
                [InlineKeyboardButton(text="⬅️ Назад к чату", callback_data=f"chat:{owner_id}:{chat_id}")]
            ])
            return await m.answer(f"❌ В этом чате ничего не найдено по запросу «<b>{html.escape(query)}</b>».", reply_markup=kb, parse_mode="HTML")
            
        text = f"🔎 <b>Найдено {len(results)} сообщений</b> в этом чате (по «<i>{html.escape(query)}</i>»):\n\n"
        for idx, log in enumerate(results, 1):
            time_str = (log.created_at + timedelta(hours=3)).strftime("%d.%m %H:%M")
            who = "🟢 Клиент" if log.from_id == owner_id else "⚪️ Собеседник"
            snippet = (log.text[:70] + "...") if len(log.text) > 70 else log.text
            text += f"<b>{idx}.</b> {who} <code>[{time_str}]</code> (ID:<code>#{log.message_id}</code>):\n└ <i>«{html.escape(snippet)}»</i>\n\n"
            
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📜 Открыть диалог", callback_data=f"msgs:{owner_id}:{chat_id}:0")],
            [InlineKeyboardButton(text="🔎 Искать снова", callback_data=f"c_search:{owner_id}:{chat_id}")],
            [InlineKeyboardButton(text="⬅️ Назад к чату", callback_data=f"chat:{owner_id}:{chat_id}")]
        ])
        await m.answer(text, reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data.startswith("msgs:"), F.from_user.id == ADMIN_ID)
async def view_chat_msgs(call: CallbackQuery):
    await call.answer()
    parts = call.data.split(":")
    owner_id = int(parts[1])
    chat_id = int(parts[2])
    page = int(parts[3]) if len(parts) > 3 else 0
    m_filter = parts[4] if len(parts) > 4 else "all"
    
    async with Session() as session:
        inter_info = await get_interlocutor_info(session, owner_id, chat_id)
        owner_res = await session.execute(select(Conn).where(Conn.user_id == owner_id).order_by(desc(Conn.id)).limit(1))
        owner = owner_res.scalars().first()
        owner_label = f"@{owner.username}" if (owner and owner.username) else (owner.full_name if owner else f"ID:{owner_id}")

        stmt = select(MsgLog).where(MsgLog.owner_id == owner_id, MsgLog.chat_id == chat_id)
        if m_filter == "sd":
            stmt = stmt.where(MsgLog.is_self_destruct == True)
        elif m_filter == "deleted":
            stmt = stmt.where(MsgLog.is_deleted == True)
        elif m_filter == "edited":
            stmt = stmt.where(MsgLog.is_edited == True)
        elif m_filter == "media":
            stmt = stmt.where(or_(MsgLog.file_path != None, MsgLog.telegram_file_id != None))

        total_res = await session.execute(select(func.count()).select_from(stmt.subquery()))
        total_msgs = total_res.scalar() or 0
        max_pages = max(1, (total_msgs + PAGE_SIZE - 1) // PAGE_SIZE)
        if page >= max_pages: page = max_pages - 1

        res = await session.execute(
            stmt.order_by(desc(MsgLog.created_at)).limit(PAGE_SIZE).offset(page * PAGE_SIZE)
        )
        logs = res.scalars().all()
        
        filter_labels = {
            "all": "Все", "sd": "🔥 Исчезающие", "deleted": "🗑 Удалённые", "edited": "✏️ Правки", "media": "📷 Медиа"
        }
        
        text = (
            f"💬 <b>Диалог:</b> <b>{owner_label}</b> ⇄ <b>{inter_info}</b> (Стр. {page+1}/{max_pages})\n"
            f"<i>Клиент ID:{owner_id} | Чат ID:{chat_id}</i> | Фильтр: <b>{filter_labels.get(m_filter, m_filter)}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
        )
        
        media_in_page = []
        if not logs:
            text += "<i>В этой категории сообщений пока нет.</i>\n\n"
        else:
            for l in reversed(logs):
                time_str = (l.created_at + timedelta(hours=3)).strftime("%H:%M")
                is_client = (l.from_id == owner_id)
                sender_badge = "🟢 <b>Клиент</b>" if is_client else "⚪️ <b>Собеседник</b>"
                reply_tag = f" <i>(в ответ на #{l.reply_to_id})</i>" if l.reply_to_id else ""
                
                sd_icon = "🔥 " if l.is_self_destruct else ""
                del_icon = "🗑 [УДАЛЕНО] " if getattr(l, 'is_deleted', False) else ""
                edit_icon = "✏️ [ИЗМЕНЕНО] " if getattr(l, 'is_edited', False) else ""
                
                content = ""
                if l.text:
                    content = l.text
                elif l.media_type:
                    m_name = {"photo": "Фото", "video": "Видео", "video_note": "Кружочек", "voice": "Голосовое", "document": "Документ"}.get(l.media_type, "Медиа")
                    content = f"[{m_name}]"
                else:
                    content = "[Сообщение]"
                    
                msg_body = f"{del_icon}{edit_icon}{sd_icon}{content}"
                text += f"{sender_badge} <code>[{time_str}]</code>{reply_tag}:\n<blockquote>{html.escape(msg_body)}</blockquote>\n\n"
                
                if (l.file_path or l.telegram_file_id) and len(media_in_page) < 4:
                    media_in_page.append(l)
        
        # Кнопки фильтров в самом диалоге
        filters_row = [
            InlineKeyboardButton(text=f"{'• ' if m_filter == 'all' else ''}Все", callback_data=f"msgs:{owner_id}:{chat_id}:0:all"),
            InlineKeyboardButton(text=f"{'• ' if m_filter == 'sd' else ''}🔥 SD", callback_data=f"msgs:{owner_id}:{chat_id}:0:sd"),
            InlineKeyboardButton(text=f"{'• ' if m_filter == 'deleted' else ''}🗑 Удал.", callback_data=f"msgs:{owner_id}:{chat_id}:0:deleted"),
            InlineKeyboardButton(text=f"{'• ' if m_filter == 'edited' else ''}✏️ Правки", callback_data=f"msgs:{owner_id}:{chat_id}:0:edited"),
            InlineKeyboardButton(text=f"{'• ' if m_filter == 'media' else ''}📷 Медиа", callback_data=f"msgs:{owner_id}:{chat_id}:0:media"),
        ]
        kb = InlineKeyboardMarkup(inline_keyboard=[filters_row])
        
        # Интерактивные кнопки скачивания медиа прямо из этого сообщения
        if media_in_page:
            m_row = []
            for m in media_in_page:
                type_icon = {"photo": "📷", "video": "🎥", "video_note": "⭕", "voice": "🎙"}.get(m.media_type, "📎")
                sd_mark = "🔥 " if m.is_self_destruct else ""
                btn_txt = f"📥 {sd_mark}{type_icon} #{m.message_id}"
                m_row.append(InlineKeyboardButton(text=btn_txt, callback_data=f"get_f:{m.id}"))
                if len(m_row) == 2:
                    kb.inline_keyboard.append(m_row)
                    m_row = []
            if m_row:
                kb.inline_keyboard.append(m_row)
        
        # Пагинация
        nav_btns = []
        if page > 0:
            nav_btns.append(InlineKeyboardButton(text="⬅️", callback_data=f"msgs:{owner_id}:{chat_id}:{page-1}:{m_filter}"))
        nav_btns.append(InlineKeyboardButton(text=f"{page+1}/{max_pages}", callback_data="noop"))
        if (page + 1) < max_pages:
            nav_btns.append(InlineKeyboardButton(text="➡️", callback_data=f"msgs:{owner_id}:{chat_id}:{page+1}:{m_filter}"))
        if nav_btns:
            kb.inline_keyboard.append(nav_btns)
            
        # Быстрые действия
        kb.inline_keyboard.append([
            InlineKeyboardButton(text="🔎 Поиск", callback_data=f"c_search:{owner_id}:{chat_id}"),
            InlineKeyboardButton(text="🖼 Все медиа", callback_data=f"media:{owner_id}:{chat_id}:0:all")
        ])
        kb.inline_keyboard.append([InlineKeyboardButton(text="⬅️ Меню чата", callback_data=f"chat:{owner_id}:{chat_id}")])
        
        await call.message.edit_text(text[:4000], reply_markup=kb, parse_mode="HTML")

# --- ЭКСПОРТЫ И CSV С ВЫБОРОМ ПЕРИОДА ---

@router.message(F.text.contains("Экспорт всей базы"), F.from_user.id == ADMIN_ID)
async def export_all_csv_start(m: types.Message):
    kb = get_export_period_kb(action_prefix="do_full_exp", back_cb="back_to_main")
    await m.answer("📅 <b>Выберите период для экспорта всей базы сообщений:</b>", reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data.startswith("u_export:"), F.from_user.id == ADMIN_ID)
async def export_user_csv_start(call: CallbackQuery):
    await call.answer()
    user_id = int(call.data.split(":")[1])
    kb = get_export_period_kb(action_prefix=f"do_u_exp:{user_id}", back_cb=f"u_menu:{user_id}")
    await call.message.edit_text(f"📅 <b>Выберите период экспорта истории пользователя ID:{user_id}:</b>", reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data.startswith("c_export:"), F.from_user.id == ADMIN_ID)
async def export_chat_csv_start(call: CallbackQuery):
    await call.answer()
    _, owner_id, chat_id = call.data.split(":")
    kb = get_export_period_kb(action_prefix=f"do_c_exp:{owner_id}:{chat_id}", back_cb=f"chat:{owner_id}:{chat_id}")
    await call.message.edit_text(f"📅 <b>Выберите период экспорта чата ID:{chat_id}:</b>", reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data.startswith("do_full_exp:"), F.from_user.id == ADMIN_ID)
async def do_full_export_csv(call: CallbackQuery):
    await call.answer("Формирую выгрузку...")
    period = call.data.split(":")[1]
    path = f"export_full_{period}.csv"
    
    async with Session() as session:
        stmt = select(MsgLog).order_by(MsgLog.created_at)
        if period != "all":
            cutoff = datetime.now() - timedelta(hours=int(period))
            stmt = stmt.where(MsgLog.created_at >= cutoff)
            
        res = await session.execute(stmt)
        rows = res.scalars().all()
        if not rows:
            return await call.message.answer("❌ За выбранный период сообщений не найдено.")
            
        seen = set(); unique_rows = []
        for r in rows:
            key = (r.message_id, r.text)
            if key not in seen: seen.add(key); unique_rows.append(r)
            
        with open(path, "w", encoding="utf-8-sig", newline='') as f:
            w = csv.writer(f)
            w.writerow(["Дата (МСК)", "Владелец аккаунта", "От кого", "Кому (Чат)", "Текст", "Файл"])
            for r in unique_rows:
                sender = f"@{r.from_username} ({r.from_name})" if r.from_username else f"{r.from_name}"
                recipient = f"Чат ID:{r.chat_id}" if r.from_id == r.owner_id else f"Владелец ({r.owner_id})"
                w.writerow([(r.created_at + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M"), r.owner_id, sender, recipient, r.text, r.file_path])
                
    await call.message.answer_document(FSInputFile(path))
    if os.path.exists(path): os.remove(path)

@router.callback_query(F.data.startswith("do_u_exp:"), F.from_user.id == ADMIN_ID)
async def do_user_export_csv(call: CallbackQuery):
    await call.answer("Формирую выгрузку...")
    _, user_id, period = call.data.split(":")
    user_id = int(user_id)
    path = f"export_{user_id}_{period}.csv"
    
    async with Session() as session:
        stmt = select(MsgLog).where(MsgLog.owner_id == user_id).order_by(MsgLog.created_at)
        if period != "all":
            cutoff = datetime.now() - timedelta(hours=int(period))
            stmt = stmt.where(MsgLog.created_at >= cutoff)
            
        res = await session.execute(stmt)
        rows = res.scalars().all()
        if not rows:
            return await call.message.answer(f"❌ За выбранный период сообщений пользователя {user_id} не найдено.")
            
        seen = set(); unique_rows = []
        for r in rows:
            key = (r.message_id, r.text)
            if key not in seen: seen.add(key); unique_rows.append(r)
            
        with open(path, "w", encoding="utf-8-sig", newline='') as f:
            w = csv.writer(f)
            w.writerow(["Дата (МСК)", "От кого", "Кому (Чат)", "Текст", "Файл"])
            for r in unique_rows:
                sender = f"@{r.from_username} ({r.from_name})" if r.from_username else f"{r.from_name}"
                recipient = "Владелец" if r.from_id != user_id else f"Собеседник (Чат ID:{r.chat_id})"
                w.writerow([(r.created_at + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M"), sender, recipient, r.text, r.file_path])
                
    await call.message.answer_document(FSInputFile(path))
    if os.path.exists(path): os.remove(path)

@router.callback_query(F.data.startswith("do_c_exp:"), F.from_user.id == ADMIN_ID)
async def do_chat_export_csv(call: CallbackQuery):
    await call.answer("Формирую выгрузку...")
    _, owner_id, chat_id, period = call.data.split(":")
    owner_id, chat_id = int(owner_id), int(chat_id)
    path = f"chat_{chat_id}_{period}.csv"
    
    async with Session() as session:
        stmt = select(MsgLog).where(MsgLog.owner_id == owner_id, MsgLog.chat_id == chat_id).order_by(MsgLog.created_at)
        if period != "all":
            cutoff = datetime.now() - timedelta(hours=int(period))
            stmt = stmt.where(MsgLog.created_at >= cutoff)
            
        res = await session.execute(stmt)
        rows = res.scalars().all()
        if not rows:
            return await call.message.answer(f"❌ За выбранный период сообщений в чате {chat_id} не найдено.")
            
        seen = set(); unique_rows = []
        for r in rows:
            key = (r.message_id, r.text)
            if key not in seen: seen.add(key); unique_rows.append(r)
            
        with open(path, "w", encoding="utf-8-sig", newline='') as f:
            w = csv.writer(f)
            w.writerow(["Дата (МСК)", "От кого", "Кому", "Текст", "Файл"])
            for r in unique_rows:
                sender = f"@{r.from_username} ({r.from_name})" if r.from_username else f"{r.from_name}"
                recipient = f"Чат ID:{chat_id}" if r.from_id == owner_id else f"Владелец ID:{owner_id}"
                w.writerow([(r.created_at + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M"), sender, recipient, r.text, r.file_path])
                
    await call.message.answer_document(FSInputFile(path))
    if os.path.exists(path): os.remove(path)

# --- УМНЫЙ АРХИВ МЕДИА (С ЗАЩИТОЙ ОТ ПОВТОРОВ) ---

def get_archived_files():
    if not os.path.exists(ARCHIVE_HISTORY_FILE): return set()
    with open(ARCHIVE_HISTORY_FILE, "r") as f:
        return set(f.read().splitlines())

def mark_as_archived(filepaths):
    with open(ARCHIVE_HISTORY_FILE, "a") as f:
        for p in filepaths:
            f.write(f"{os.path.basename(p)}\n")

@router.message(F.text.contains("Архив медиа"), F.from_user.id == ADMIN_ID)
async def archive_menu(m: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Архив всего", callback_data="archive:all")],
        [InlineKeyboardButton(text="⏳ За последние 1 час", callback_data="archive:1")],
        [InlineKeyboardButton(text="⏳ За последние 6 часов", callback_data="archive:6")],
        [InlineKeyboardButton(text="⏳ За последние 24 часа", callback_data="archive:24")],
        [InlineKeyboardButton(text="⏳ За последние 3 дня", callback_data="archive:72")],
    ])
    await m.answer("Выберите период для архивации:", reply_markup=kb, parse_mode="HTML")

# НОВЫЙ ШАГ: Спрашиваем про пропуск файлов
@router.callback_query(F.data.startswith("archive:"), F.from_user.id == ADMIN_ID)
async def ask_skip_archived(call: CallbackQuery):
    period = call.data.split(":")[1]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, пропустить", callback_data=f"do_archive:{period}:skip")],
        [InlineKeyboardButton(text="❌ Нет, скачать всё", callback_data=f"do_archive:{period}:all")],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_to_main")]
    ])
    await call.message.edit_text(
        "Пропустить файлы, которые уже были заархивированы ранее?",
        reply_markup=kb
    )

# ИСПОЛНЕНИЕ АРХИВАЦИИ
@router.callback_query(F.data.startswith("do_archive:"), F.from_user.id == ADMIN_ID)
async def execute_archive(call: CallbackQuery, state: FSMContext):
    _, period, skip_mode = call.data.split(":")
    await call.message.edit_text("⏳ Сканирую файлы... (Смотрите логи на сервере)")
    
    if not os.path.exists(MEDIA_DIR):
        return await call.message.edit_text("Папка с медиа пуста.")

    full_archived_set = get_archived_files()
    files_to_archive = []
    new_to_archive_txt = [] # Сюда запишем только те, которых еще не было в истории
    current_time = time.time()

    logger.info(f"--- СТАРТ АРХИВАЦИИ: {period} | Режим пропуска: {skip_mode} ---")

    for root, dirs, files in os.walk(MEDIA_DIR):
        for f in files:
            if f == ".archived.txt": continue
            
            # Если выбрали "Да, пропустить" и файл уже есть в истории - пропускаем
            if skip_mode == "skip" and f in full_archived_set:
                logger.info(f"[ПРОПУСК] {f} (Уже был в архиве)")
                continue

            path = os.path.join(root, f)
            file_age_hours = (current_time - os.path.getmtime(path)) / 3600

            add_file = False
            if period == "all":
                add_file = True
                logger.info(f"[ДОБАВЛЕН] {f} (Архив всего)")
            else:
                hours = int(period)
                if file_age_hours <= hours:
                    add_file = True
                    logger.info(f"[ДОБАВЛЕН] {f} (Возраст: {file_age_hours:.1f}ч <= {hours}ч)")
                else:
                    logger.info(f"[ПРОПУСК] {f} (Слишком старый: {file_age_hours:.1f}ч > {hours}ч)")

            if add_file:
                files_to_archive.append(path)
                # Запоминаем файл для истории, только если его там еще нет (чтобы не дублировать записи)
                if f not in full_archived_set:
                    new_to_archive_txt.append(path)

    if not files_to_archive:
        return await call.message.edit_text("✅ Нет подходящих файлов для архивации за этот период.")

    await call.message.edit_text(f"📦 Найдено {len(files_to_archive)} файлов. Начинаю упаковку...")
    
    MAX_ZIP_SIZE = 45 * 1024 * 1024 
    zip_paths = []
    current_zip_idx = 1
    current_zip_path = f"archive_part{current_zip_idx}.zip"
    current_zip = zipfile.ZipFile(current_zip_path, 'w', zipfile.ZIP_DEFLATED)
    zip_paths.append(current_zip_path)
    current_size = 0

    for path in files_to_archive:
        file_size = os.path.getsize(path)
        if current_size + file_size > MAX_ZIP_SIZE and current_size > 0:
            current_zip.close()
            current_zip_idx += 1
            current_zip_path = f"archive_part{current_zip_idx}.zip"
            current_zip = zipfile.ZipFile(current_zip_path, 'w', zipfile.ZIP_DEFLATED)
            zip_paths.append(current_zip_path)
            current_size = 0

        arcname = os.path.relpath(path, MEDIA_DIR)
        current_zip.write(path, arcname)
        current_size += file_size

    current_zip.close()

    for zp in zip_paths:
        try:
            await call.message.answer_document(FSInputFile(zp))
        except Exception as e:
            logger.error(f"Ошибка отправки архива {zp}: {e}")
        finally:
            if os.path.exists(zp): os.remove(zp)

    # Записываем в историю только новые файлы
    if new_to_archive_txt:
        mark_as_archived(new_to_archive_txt)
        
    await state.update_data(files_to_delete=files_to_archive)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚨 ДА, удалить с сервера", callback_data="confirm_media_delete")],
        [InlineKeyboardButton(text="❌ НЕТ, оставить файлы", callback_data="cancel_media_delete")]
    ])
    await call.message.answer(f"✅ Все архивы отправлены.\n\nУдалить эти {len(files_to_archive)} файлов с сервера для освобождения места?", reply_markup=kb)

@router.callback_query(F.data == "confirm_media_delete", F.from_user.id == ADMIN_ID)
async def confirm_media_delete(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    files = data.get("files_to_delete", [])
    count = 0
    for f in files:
        if os.path.exists(f):
            os.remove(f)
            count += 1
    await call.message.edit_text(f"✅ Успешно удалено {count} медиафайлов. Место освобождено.")
    await state.clear()

@router.callback_query(F.data == "cancel_media_delete", F.from_user.id == ADMIN_ID)
async def cancel_media_delete(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("✅ Файлы оставлены на сервере.")
    await state.clear()

# --- СТАТИСТИКА, ЛОГИ И ВОЗВРАТ ---

@router.message(F.text.contains("Статистика"), F.from_user.id == ADMIN_ID)
async def stats(m: types.Message):
    async with Session() as session:
        total_msgs = (await session.execute(select(func.count(MsgLog.id)))).scalar() or 0
        total_users = (await session.execute(select(func.count(UserAccount.user_id)))).scalar() or 0
        active_users = (await session.execute(select(func.count(UserAccount.user_id)).where(UserAccount.is_active == True))).scalar() or 0
        inactive_users = total_users - active_users
        paid_users = (await session.execute(select(func.count(UserAccount.user_id)).where(UserAccount.subscription_until > datetime.now()))).scalar() or 0
        
        stat_text = (
            "📊 <b>Статистика бота:</b>\n\n"
            f"💬 Всего сообщений: <code>{total_msgs}</code>\n"
            f"👥 Пользователей: <code>{total_users}</code>\n"
            f"  ├ 🟢 Активны (подключено): <code>{active_users}</code>\n"
            f"  ├ 🔴 Отключили бота: <code>{inactive_users}</code>\n"
            f"  └ ⭐ С активной подпиской: <code>{paid_users}</code>"
        )
        await m.answer(stat_text, parse_mode="HTML")

async def render_global_logs(target, filter_type="all", edit: bool = False):
    async with Session() as session:
        stmt = (
            select(MsgLog, Conn.username.label('owner_user'), Conn.full_name.label('owner_name'))
            .join(Conn, MsgLog.owner_id == Conn.user_id)
        )
        
        if filter_type == "sd":
            stmt = stmt.where(MsgLog.is_self_destruct == True)
        elif filter_type == "deleted":
            stmt = stmt.where(MsgLog.is_deleted == True)
        elif filter_type == "edited":
            stmt = stmt.where(MsgLog.is_edited == True)
        elif filter_type == "media":
            stmt = stmt.where(or_(MsgLog.file_path != None, MsgLog.telegram_file_id != None))
            
        stmt = stmt.order_by(desc(MsgLog.id)).limit(40)
        res = await session.execute(stmt)
        rows = res.all()
        
        filter_names = {
            "all": "Все события",
            "sd": "🔥 Исчезающие",
            "deleted": "🗑 Удалённые",
            "edited": "✏️ Отредактированные",
            "media": "📷 Медиафайлы"
        }
        
        seen_events = set()
        unique_events = []
        
        for msg, owner_un, owner_nm in rows:
            content_key = (msg.text or "").strip()
            if not content_key:
                content_key = f"media_{msg.media_type}_{msg.telegram_file_id or msg.file_path or ''}"
            time_bucket = int(msg.created_at.timestamp() // 10)
            dedup_key = (msg.from_id, content_key, time_bucket)
            
            if dedup_key in seen_events:
                continue
            seen_events.add(dedup_key)
            unique_events.append((msg, owner_un, owner_nm))
            if len(unique_events) >= 10:
                break
                
        filters_row = [
            InlineKeyboardButton(text=f"{'• ' if filter_type == 'all' else ''}Все", callback_data="logs_f:all"),
            InlineKeyboardButton(text=f"{'• ' if filter_type == 'sd' else ''}🔥 SD", callback_data="logs_f:sd"),
            InlineKeyboardButton(text=f"{'• ' if filter_type == 'deleted' else ''}🗑 Удал.", callback_data="logs_f:deleted"),
            InlineKeyboardButton(text=f"{'• ' if filter_type == 'edited' else ''}✏️ Правки", callback_data="logs_f:edited"),
            InlineKeyboardButton(text=f"{'• ' if filter_type == 'media' else ''}📷 Медиа", callback_data="logs_f:media"),
        ]
        
        kb = InlineKeyboardMarkup(inline_keyboard=[filters_row])
        
        if not unique_events:
            text = f"🔍 <b>События ({filter_names.get(filter_type, filter_type)}):</b>\n\n<i>Записей не найдено.</i>"
            kb.inline_keyboard.append([
                InlineKeyboardButton(text="🔄 Обновить", callback_data=f"logs_f:{filter_type}"),
                InlineKeyboardButton(text="⬅️ В меню", callback_data="back_to_main")
            ])
            if edit:
                try:
                    return await target.edit_text(text, reply_markup=kb, parse_mode="HTML")
                except Exception:
                    return
            else:
                return await target.answer(text, reply_markup=kb, parse_mode="HTML")
                
        text = f"🔍 <b>События ({filter_names.get(filter_type, filter_type)}):</b>\n\n"
        
        seen_chats = {}
        for idx, (msg, owner_un, owner_nm) in enumerate(unique_events, 1):
            time_str = (msg.created_at + timedelta(hours=3)).strftime("%H:%M")
            owner_info = fmt_user_info(owner_nm, owner_un, msg.owner_id)
            sender_info = fmt_user_info(msg.from_name, msg.from_username, msg.from_id)
            is_out = (msg.from_id == msg.owner_id)
            arrow = "📤 Исходящее" if is_out else "📥 Входящее"
            
            sd_tag = "🔥 [SD] " if msg.is_self_destruct else ""
            del_tag = "🗑 [УДАЛЕНО] " if getattr(msg, 'is_deleted', False) else ""
            edit_tag = "✏️ [ИЗМЕНЕНО] " if getattr(msg, 'is_edited', False) else ""
            
            raw_text = msg.text or f"[{msg.media_type or 'Медиа'}]"
            snippet = html.escape((raw_text[:50] + "...") if len(raw_text) > 50 else raw_text)
            
            text += (
                f"<b>{idx}.</b> 🕒 <code>{time_str}</code> | {owner_info}\n"
                f"   {arrow} от <b>{sender_info}</b> (ID:<code>#{msg.message_id}</code>):\n"
                f"   └ {del_tag}{edit_tag}{sd_tag}<i>{snippet}</i>\n\n"
            )
            
            # Сохраняем уникальный диалог и имя собеседника
            chat_key = (msg.owner_id, msg.chat_id)
            if chat_key not in seen_chats or seen_chats[chat_key].startswith("ID:"):
                if msg.from_id != msg.owner_id:
                    inter_label = f"@{msg.from_username}" if msg.from_username else (msg.from_name or f"ID:{msg.chat_id}")
                    seen_chats[chat_key] = inter_label
                elif chat_key not in seen_chats:
                    seen_chats[chat_key] = f"ID:{msg.chat_id}"

        # Формируем кнопки перехода к диалогам без дубликатов
        if len(seen_chats) == 1:
            (owner_id, chat_id), inter_label = list(seen_chats.items())[0]
            kb.inline_keyboard.append([
                InlineKeyboardButton(text=f"💬 Открыть этот диалог ({inter_label})", callback_data=f"msgs:{owner_id}:{chat_id}:0")
            ])
        elif len(seen_chats) > 1:
            chat_btns = []
            for (owner_id, chat_id), inter_label in seen_chats.items():
                btn_title = f"💬 Диалог с {inter_label[:15]}"
                chat_btns.append(InlineKeyboardButton(text=btn_title, callback_data=f"msgs:{owner_id}:{chat_id}:0"))
            
            for i in range(0, len(chat_btns), 2):
                kb.inline_keyboard.append(chat_btns[i:i+2])
            
        kb.inline_keyboard.append([
            InlineKeyboardButton(text="🔄 Обновить", callback_data=f"logs_f:{filter_type}"),
            InlineKeyboardButton(text="⬅️ В главное меню", callback_data="back_to_main")
        ])
        
        if edit:
            try:
                await target.edit_text(text[:4000], reply_markup=kb, parse_mode="HTML")
            except Exception:
                pass
        else:
            await target.answer(text[:4000], reply_markup=kb, parse_mode="HTML")

@router.message(F.text.contains("логов"), F.from_user.id == ADMIN_ID)
async def list_global_logs(m: types.Message):
    await render_global_logs(m, "all", edit=False)

@router.callback_query(F.data.startswith("logs_f:"), F.from_user.id == ADMIN_ID)
async def filter_global_logs_cb(call: CallbackQuery):
    await call.answer()
    filter_type = call.data.split(":")[1]
    await render_global_logs(call.message, filter_type, edit=True)

MEDIA_PAGE_SIZE = 8

@router.callback_query(F.data.startswith("media:"), F.from_user.id == ADMIN_ID)
async def view_chat_media(call: CallbackQuery):
    await call.answer()
    parts = call.data.split(":")
    owner_id = int(parts[1])
    chat_id = int(parts[2])
    page = int(parts[3]) if len(parts) > 3 else 0
    m_filter = parts[4] if len(parts) > 4 else "all"

    async with Session() as session:
        inter_info = await get_interlocutor_info(session, owner_id, chat_id)
        
        stmt = select(MsgLog).where(
            MsgLog.owner_id == owner_id,
            MsgLog.chat_id == chat_id,
            or_(MsgLog.file_path != None, MsgLog.telegram_file_id != None)
        )
        if m_filter == "sd":
            stmt = stmt.where(MsgLog.is_self_destruct == True)
        elif m_filter == "deleted":
            stmt = stmt.where(MsgLog.is_deleted == True)
        elif m_filter == "photo":
            stmt = stmt.where(MsgLog.media_type == "photo")
        elif m_filter == "video":
            stmt = stmt.where(or_(MsgLog.media_type == "video", MsgLog.media_type == "video_note"))
        elif m_filter == "voice":
            stmt = stmt.where(or_(MsgLog.media_type == "voice", MsgLog.media_type == "audio"))
        elif m_filter == "doc":
            stmt = stmt.where(MsgLog.media_type == "document")
            
        total_res = await session.execute(select(func.count()).select_from(stmt.subquery()))
        total_count = total_res.scalar() or 0
        
        max_pages = max(1, (total_count + MEDIA_PAGE_SIZE - 1) // MEDIA_PAGE_SIZE)
        if page >= max_pages:
            page = max(0, max_pages - 1)

        res = await session.execute(stmt.order_by(desc(MsgLog.id)).limit(MEDIA_PAGE_SIZE).offset(page * MEDIA_PAGE_SIZE))
        media_items = res.scalars().all()

        filter_names = {
            "all": "Все файлы",
            "sd": "🔥 Исчезающие",
            "deleted": "🗑 Удалённые",
            "photo": "📷 Фото",
            "video": "🎥 Видео",
            "voice": "🎙 Голос",
            "doc": "📄 Документы"
        }
        
        filter_row1 = [
            InlineKeyboardButton(text=f"{'• ' if m_filter == 'all' else ''}Все", callback_data=f"media:{owner_id}:{chat_id}:0:all"),
            InlineKeyboardButton(text=f"{'• ' if m_filter == 'sd' else ''}🔥 SD", callback_data=f"media:{owner_id}:{chat_id}:0:sd"),
            InlineKeyboardButton(text=f"{'• ' if m_filter == 'deleted' else ''}🗑 Удал.", callback_data=f"media:{owner_id}:{chat_id}:0:deleted"),
        ]
        filter_row2 = [
            InlineKeyboardButton(text=f"{'• ' if m_filter == 'photo' else ''}📷 Фото", callback_data=f"media:{owner_id}:{chat_id}:0:photo"),
            InlineKeyboardButton(text=f"{'• ' if m_filter == 'video' else ''}🎥 Видео", callback_data=f"media:{owner_id}:{chat_id}:0:video"),
            InlineKeyboardButton(text=f"{'• ' if m_filter == 'voice' else ''}🎙 Голос", callback_data=f"media:{owner_id}:{chat_id}:0:voice"),
        ]
        
        kb = InlineKeyboardMarkup(inline_keyboard=[filter_row1, filter_row2])
        
        for m in media_items:
            time_str = (m.created_at + timedelta(hours=3)).strftime("%d.%m %H:%M")
            sd_mark = "🔥 " if m.is_self_destruct else ""
            del_mark = "🗑 " if getattr(m, 'is_deleted', False) else ""
            type_icon = {"photo": "📷", "video": "🎥", "video_note": "⭕", "voice": "🎙", "audio": "🎵", "document": "📄"}.get(m.media_type, "📎")
            btn_title = f"{del_mark}{sd_mark}{type_icon} #{m.message_id} ({time_str})"
            kb.inline_keyboard.append([InlineKeyboardButton(text=btn_title, callback_data=f"get_f:{m.id}")])

        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"media:{owner_id}:{chat_id}:{page-1}:{m_filter}"))
        nav_row.append(InlineKeyboardButton(text=f"{page+1}/{max_pages}", callback_data="noop"))
        if page + 1 < max_pages:
            nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"media:{owner_id}:{chat_id}:{page+1}:{m_filter}"))
        
        kb.inline_keyboard.append(nav_row)
        kb.inline_keyboard.append([InlineKeyboardButton(text="⬅️ Назад к чату", callback_data=f"chat:{owner_id}:{chat_id}")])

        text = (
            f"🖼 <b>Медиа чата:</b> {inter_info}\n"
            f"Всего файлов: <code>{total_count}</code> | Фильтр: <b>{filter_names.get(m_filter, m_filter)}</b>\n\n"
            f"<i>Нажмите на файл в списке ниже, чтобы бот прислал его:</i>"
        )
        if not media_items:
            text += "\n\n<i>Медиафайлов не найдено.</i>"

        try:
            await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            await call.message.answer(text, reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data == "noop")
async def noop_cb(call: CallbackQuery):
    await call.answer()

@router.callback_query(F.data.startswith("get_f:"), F.from_user.id == ADMIN_ID)
async def send_file(call: CallbackQuery, bot: Bot):
    await call.answer("Отправка файла...")
    log_id = int(call.data.split(":")[1])
    async with Session() as session:
        res = await session.execute(select(MsgLog).where(MsgLog.id == log_id))
        log = res.scalars().first()
        if not log:
            return await call.message.answer("❌ Запись не найдена в базе.")
        
        target = None
        if log.file_path and os.path.exists(log.file_path):
            target = FSInputFile(log.file_path)
        elif log.telegram_file_id:
            target = log.telegram_file_id
        
        if not target:
            return await call.message.answer("❌ Файл не найден на сервере и нет file_id.")
            
        try:
            time_str = (log.created_at + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M")
            sd_mark = "🔥 [ИСЧЕЗАЮЩЕЕ МЕДИА] " if log.is_self_destruct else ""
            caption = f"{sd_mark}🕒 {time_str} | ID: <code>#{log.message_id}</code>"
            if log.media_type == "photo": await bot.send_photo(ADMIN_ID, target, caption=caption, parse_mode="HTML")
            elif log.media_type == "voice": await bot.send_voice(ADMIN_ID, target, caption=caption, parse_mode="HTML")
            elif log.media_type == "video": await bot.send_video(ADMIN_ID, target, caption=caption, parse_mode="HTML")
            elif log.media_type == "video_note": await bot.send_video_note(ADMIN_ID, target)
            else: await bot.send_document(ADMIN_ID, target, caption=caption, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Error sending file {log_id}: {e}")
            await call.message.answer(f"❌ Ошибка отправки: {e}")

@router.callback_query(F.data == "back_to_settings", F.from_user.id == ADMIN_ID)
async def back_to_settings(call: CallbackQuery):
    try: await call.message.delete()
    except: pass
    await admin_settings_main(call.message)

@router.callback_query(F.data == "back_to_main", F.from_user.id == ADMIN_ID)
async def back_to_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    try: await call.message.delete()
    except: pass
    await call.message.answer("🕵️‍♂️ Кабинет админа активен!", reply_markup=get_kb())
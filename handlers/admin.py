import os
import csv
import html
import logging
import asyncio
import time
import zipfile
from datetime import timedelta, datetime
from aiogram import Router, F, types, Bot
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    FSInputFile, ReplyKeyboardMarkup, KeyboardButton, 
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)

try:
    from aiogram.types import CopyTextButton
except ImportError:
    CopyTextButton = None

from sqlalchemy import select, func, desc, and_, or_
from database.engine import Session
from database.models import MsgLog, Conn, Settings, UserAccount

router = Router()
logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_ID"))
PAGE_SIZE = 10
START_PHOTO_PATH = "start_photo.jpg"
START_ATTEMPTS = int(os.getenv("START_ATTEMPTS", 10))
MEDIA_DIR = "media"

# Настройки ЮKassa
YOOKASSA_TOKEN = os.getenv("YOOKASSA_TOKEN", "")
PRICE_30_DAYS = int(os.getenv("PRICE_30_DAYS", 100))
PRICE_60_DAYS = int(os.getenv("PRICE_60_DAYS", 170))

class AdminStates(StatesGroup):
    waiting_for_attempts = State()
    waiting_for_broadcast_all = State()
    waiting_for_broadcast_one = State()
    waiting_for_search = State()

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def fmt_user_info(name, username, user_id=None, is_paid=False):
    mark = "⭐" if is_paid else "👤"
    safe_name = html.escape(name or "???")
    un = f"@{html.escape(username)} " if username else ""
    return f"{mark} {un}({safe_name})" + (f" [ID:{user_id}]" if user_id else "")

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
        [KeyboardButton(text="👥 Пользователи"), KeyboardButton(text="🔍 Поиск")],
        [KeyboardButton(text="🔍 Список логов"), KeyboardButton(text="📥 Экспорт всей базы (CSV)")],
        [KeyboardButton(text="📦 Архив медиа (>7 дней)")] # НОВАЯ КНОПКА
    ], resize_keyboard=True)

def get_admin_settings_kb(global_notify):
    status = "✅ ВКЛ" if global_notify else "❌ ВЫКЛ"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Уведомления логов: {status}", callback_data="toggle_global_notify")],
        [InlineKeyboardButton(text="📢 Рассылка сообщений", callback_data="admin_broadcast_menu")],
        [InlineKeyboardButton(text="🤝 Рефералы", callback_data="admin_refs")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")]
    ])

def get_user_manage_kb(user_id, daily_status, attempts, is_paid):
    att_text = "Бесконечно" if attempts == 0 or is_paid else f"{attempts} шт."
    daily_text = "✅ Авто-экспорт: ВКЛ" if daily_status else "❌ Авто-экспорт: ВЫКЛ"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Список чатов", callback_data=f"owner:{user_id}:0")],
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
        [InlineKeyboardButton(text=f"💎 Подписка 30 дней ({PRICE_30_DAYS}₽)", callback_data="buy_premium:30")],
        [InlineKeyboardButton(text=f"💎 Подписка 60 дней ({PRICE_60_DAYS}₽)", callback_data="buy_premium:60")]
    ])

# --- АРХИВАЦИЯ И ОЧИСТКА МЕДИА (НОВЫЙ БЛОК) ---

@router.message(F.text.contains("Архив медиа"), F.from_user.id == ADMIN_ID)
async def create_media_archive(m: types.Message, state: FSMContext):
    await m.answer("⏳ Сканирую файлы старше 7 дней...")
    
    if not os.path.exists(MEDIA_DIR):
        return await m.answer("Папка с медиа пуста.")

    # Ищем файлы старше 7 дней
    cutoff_time = time.time() - (7 * 24 * 3600)
    files_to_archive = []
    
    for f in os.listdir(MEDIA_DIR):
        path = os.path.join(MEDIA_DIR, f)
        if os.path.isfile(path) and os.path.getmtime(path) < cutoff_time:
            files_to_archive.append(path)

    if not files_to_archive:
        return await m.answer("✅ Нет медиафайлов старше 7 дней. Сервер чист.")

    await m.answer(f"📦 Найдено {len(files_to_archive)} старых файлов. Начинаю упаковку (архивы по ~45 МБ)...")
    
    MAX_ZIP_SIZE = 45 * 1024 * 1024 # 45 МБ лимит Telegram
    zip_paths = []
    current_zip_idx = 1
    current_zip_path = f"archive_part{current_zip_idx}.zip"
    current_zip = zipfile.ZipFile(current_zip_path, 'w', zipfile.ZIP_DEFLATED)
    zip_paths.append(current_zip_path)
    current_size = 0

    # Пакуем файлы с нарезкой
    for path in files_to_archive:
        file_size = os.path.getsize(path)
        if current_size + file_size > MAX_ZIP_SIZE and current_size > 0:
            current_zip.close()
            current_zip_idx += 1
            current_zip_path = f"archive_part{current_zip_idx}.zip"
            current_zip = zipfile.ZipFile(current_zip_path, 'w', zipfile.ZIP_DEFLATED)
            zip_paths.append(current_zip_path)
            current_size = 0

        current_zip.write(path, os.path.basename(path))
        current_size += file_size

    current_zip.close()

    # Отправляем архивы
    for zp in zip_paths:
        try:
            await m.answer_document(FSInputFile(zp))
        except Exception as e:
            logger.error(f"Ошибка отправки архива {zp}: {e}")
        finally:
            if os.path.exists(zp): os.remove(zp) # Удаляем сам ZIP после отправки

    # Сохраняем список файлов в память для удаления
    await state.update_data(files_to_delete=files_to_archive)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚨 ДА, удалить с сервера", callback_data="confirm_media_delete")],
        [InlineKeyboardButton(text="❌ НЕТ, оставить файлы", callback_data="cancel_media_delete")]
    ])
    await m.answer(f"✅ Все архивы отправлены.\n\nУдалить эти {len(files_to_archive)} файлов с сервера для освобождения места?", reply_markup=kb)

@router.callback_query(F.data == "confirm_media_delete", F.from_user.id == ADMIN_ID)
async def confirm_media_delete(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    files = data.get("files_to_delete", [])
    count = 0
    for f in files:
        if os.path.exists(f):
            os.remove(f)
            count += 1
    await call.message.edit_text(f"✅ Успешно удалено {count} старых медиафайлов. Место освобождено.")
    await state.clear()

@router.callback_query(F.data == "cancel_media_delete", F.from_user.id == ADMIN_ID)
async def cancel_media_delete(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("✅ Файлы оставлены на сервере.")
    await state.clear()

# --- ОСТАЛЬНЫЕ ФУНКЦИИ (БЕЗ ИЗМЕНЕНИЙ, ПРОСТО СКОПИРОВАНЫ ДЛЯ ПОЛНОТЫ) ---
# (Здесь идут все функции из прошлой версии: /start, /settings, /ref, поиск, рассылка, экспорт, статистика)

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

    if m.from_user.id == ADMIN_ID: return await m.answer("🕵️‍♂️ Кабинет админа активен!", reply_markup=get_kb())
    
    bot_info = await bot.get_me()
    username = f"@{bot_info.username}"
    welcome_text = f"<b>Подключите бота к аккаунту, чтобы он мог помочь вам в переписке в нужный момент.</b>\n\nДля подключения используйте:\n<code>{username}</code>"
    
    kb_list = []
    if CopyTextButton: kb_list.append([InlineKeyboardButton(text="Скопировать @username", copy_text=CopyTextButton(text=username))])
    else: kb_list.append([InlineKeyboardButton(text="Скопировать @username", callback_data="copy_fallback")])
    kb_list.append([InlineKeyboardButton(text="Подробная инструкция", url=f"https://t.me/{bot_info.username}")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_list)
    
    if os.path.exists(START_PHOTO_PATH): await m.answer_photo(photo=FSInputFile(START_PHOTO_PATH), caption=welcome_text, reply_markup=kb, parse_mode="HTML")
    else: await m.answer(welcome_text, reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data == "copy_fallback")
async def copy_fallback(call: CallbackQuery, bot: Bot):
    bot_info = await bot.get_me(); await call.answer(f"@{bot_info.username}", show_alert=True)

@router.message(Command("settings", "setting"))
async def cmd_settings(m: types.Message, state: FSMContext):
    await state.clear()
    if m.from_user.id == ADMIN_ID: return await admin_settings_main(m)
    async with Session() as session:
        acc = await session.get(UserAccount, m.from_user.id)
        if not acc: return await m.answer("Нажмите /start для регистрации.")
        is_paid = acc.subscription_until and acc.subscription_until > datetime.now()
        status_text = f"Premium ⭐ (до {acc.subscription_until.strftime('%d.%m.%Y')})" if is_paid else "Базовый 👤"
        att_text = "Бесконечно" if is_paid or acc.attempts == 0 else f"{acc.attempts} шт."
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

@router.message(Command("ref", "referral"))
async def cmd_ref(m: types.Message, bot: Bot, state: FSMContext):
    await state.clear()
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={m.from_user.id}"
    await m.answer(f"🎁 <b>Ваша реферальная ссылка:</b>\n<code>{ref_link}</code>\n\nЗа каждого друга вы получите <b>+5</b> попыток!", parse_mode="HTML")

@router.message(F.text == "🔍 Поиск", F.from_user.id == ADMIN_ID)
async def search_user_start(m: types.Message, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_search)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_main")]])
    await m.answer("🔎 Введите ID или @username пользователя:", reply_markup=kb)

@router.message(AdminStates.waiting_for_search, F.from_user.id == ADMIN_ID)
async def search_user_exec(m: types.Message, state: FSMContext):
    query = m.text.replace("@", "").strip()
    async with Session() as session:
        if query.isdigit(): res = await session.execute(select(Conn).where(Conn.user_id == int(query)))
        else: res = await session.execute(select(Conn).where(Conn.username == query))
        c = res.scalars().first()
        if c:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="👤 Управление", callback_data=f"u_menu:{c.user_id}")]])
            await m.answer(f"✅ <b>Найден:</b> {fmt_user_info(c.full_name, c.username, c.user_id)}", reply_markup=kb, parse_mode="HTML")
        else: await m.answer("❌ Пользователь не найден в базе.")
    await state.clear()

@router.message(F.text == "🛠 Настройки бота", F.from_user.id == ADMIN_ID)
async def admin_settings_main(m: types.Message):
    async with Session() as session:
        res = await session.execute(select(Settings).where(Settings.id == 1))
        sett = res.scalars().first()
        if not sett:
            sett = Settings(id=1, global_notify=False)
            session.add(sett); await session.commit()
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
async def broadcast_menu(call: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🌍 Всем", callback_data="broadcast:all")],[InlineKeyboardButton(text="👤 Одному", callback_data="broadcast:one:0")],[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_settings")]])
    await call.message.edit_text("Выберите тип рассылки:", reply_markup=kb)

@router.callback_query(F.data == "broadcast:all", F.from_user.id == ADMIN_ID)
async def broadcast_all_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_broadcast_all)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="admin_broadcast_menu")]])
    await call.message.edit_text("📝 Введите сообщение для рассылки ВСЕМ (можно с фото/видео):", reply_markup=kb); await call.answer()

@router.message(AdminStates.waiting_for_broadcast_all, F.from_user.id == ADMIN_ID)
async def broadcast_all_preview(m: types.Message, state: FSMContext):
    await state.update_data(broadcast_msg_id=m.message_id, broadcast_from_chat=m.chat.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Подтвердить и отправить", callback_data="broadcast_confirm")],[InlineKeyboardButton(text="❌ Отмена", callback_data="admin_broadcast_menu")]])
    await m.send_copy(chat_id=m.chat.id, reply_markup=kb)

@router.callback_query(F.data == "broadcast_confirm", F.from_user.id == ADMIN_ID)
async def broadcast_all_exec(call: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data(); msg_id = data.get('broadcast_msg_id'); from_chat = data.get('broadcast_from_chat')
    if not msg_id: return await call.answer("Ошибка: сообщение не найдено.", show_alert=True)
    await call.message.edit_text("⏳ Начинаю рассылку...")
    async with Session() as session:
        res = await session.execute(select(UserAccount.user_id)); users = res.scalars().all()
        count = 0
        for uid in users:
            try: await bot.copy_message(chat_id=uid, from_chat_id=from_chat, message_id=msg_id); count += 1; await asyncio.sleep(0.05)
            except: pass
    await call.message.edit_text(f"✅ Рассылка завершена. Успешно доставлено: {count} чел."); await state.clear()

@router.callback_query(F.data.startswith("broadcast:one:"), F.from_user.id == ADMIN_ID)
async def broadcast_one_list(call: CallbackQuery):
    page = int(call.data.split(":")[2])
    async with Session() as session:
        res = await session.execute(select(Conn).limit(PAGE_SIZE).offset(page * PAGE_SIZE)); conns = res.scalars().all()
        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for c in conns: kb.inline_keyboard.append([InlineKeyboardButton(text=f"👤 {c.username or c.full_name}", callback_data=f"send_to:{c.user_id}")])
        nav = []
        if page > 0: nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"broadcast:one:{page-1}"))
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"broadcast:one:{page+1}"))
        kb.inline_keyboard.append(nav); kb.inline_keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_broadcast_menu")])
        await call.message.edit_text("Выберите пользователя для личного сообщения:", reply_markup=kb)

@router.callback_query(F.data.startswith("send_to:"), F.from_user.id == ADMIN_ID)
async def broadcast_one_start(call: CallbackQuery, state: FSMContext):
    uid = int(call.data.split(":")[1]); await state.update_data(target_id=uid); await state.set_state(AdminStates.waiting_for_broadcast_one)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="admin_broadcast_menu")]])
    await call.message.edit_text(f"📝 Введите сообщение для пользователя <code>{uid}</code>:", reply_markup=kb, parse_mode="HTML"); await call.answer()

@router.message(AdminStates.waiting_for_broadcast_one, F.from_user.id == ADMIN_ID)
async def broadcast_one_preview(m: types.Message, state: FSMContext):
    await state.update_data(broadcast_msg_id=m.message_id, broadcast_from_chat=m.chat.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Подтвердить и отправить", callback_data="broadcast_one_confirm")],[InlineKeyboardButton(text="❌ Отмена", callback_data="admin_broadcast_menu")]])
    await m.send_copy(chat_id=m.chat.id, reply_markup=kb)

@router.callback_query(F.data == "broadcast_one_confirm", F.from_user.id == ADMIN_ID)
async def broadcast_one_exec(call: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data(); uid = data.get('target_id'); msg_id = data.get('broadcast_msg_id'); from_chat = data.get('broadcast_from_chat')
    if not msg_id or not uid: return await call.answer("Ошибка данных.", show_alert=True)
    await call.message.edit_text("⏳ Отправляю...")
    try: await bot.copy_message(chat_id=uid, from_chat_id=from_chat, message_id=msg_id); await call.message.edit_text(f"✅ Сообщение успешно отправлено пользователю <code>{uid}</code>.", parse_mode="HTML")
    except Exception as e: await call.message.edit_text(f"❌ Ошибка отправки: {e}")
    await state.clear()

@router.callback_query(F.data == "admin_refs", F.from_user.id == ADMIN_ID)
async def admin_list_refs(call: CallbackQuery):
    async with Session() as session:
        res = await session.execute(select(UserAccount).where(UserAccount.referrer_id != None)); accounts = res.scalars().all()
        if not accounts: return await call.answer("Рефералов пока нет.", show_alert=True)
        text = "🤝 <b>Список приглашений:</b>\n\n"
        for acc in accounts:
            u_res = await session.execute(select(Conn).where(Conn.user_id == acc.user_id)); u = u_res.scalars().first()
            r_res = await session.execute(select(Conn).where(Conn.user_id == acc.referrer_id)); r = r_res.scalars().first()
            inviter = fmt_user_info(r.full_name, r.username, acc.referrer_id) if r else f"ID:{acc.referrer_id}"
            invited = fmt_user_info(u.full_name, u.username, acc.user_id) if u else f"ID:{acc.user_id}"
            text += f"👤 {inviter}\n ➡️ {invited}\n\n"
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_settings")]])
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")

@router.message(F.text.contains("Пользователи"), F.from_user.id == ADMIN_ID)
async def list_owners_cmd(m: types.Message): await list_owners(m, 0)

async def list_owners(m, page: int):
    async with Session() as session:
        res = await session.execute(select(Conn).limit(PAGE_SIZE).offset(page * PAGE_SIZE)); conns = res.scalars().all()
        if not conns and page == 0: return await m.answer("Пользователей нет.")
        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for c in conns:
            res_acc = await session.execute(select(UserAccount).where(UserAccount.user_id == c.user_id)); acc = res_acc.scalars().first()
            is_paid = acc.subscription_until and acc.subscription_until > datetime.now() if acc else False
            btn_text = f"👤 {fmt_user_info(c.full_name, c.username, c.user_id, is_paid)}"
            kb.inline_keyboard.append([InlineKeyboardButton(text=btn_text, callback_data=f"u_menu:{c.user_id}")])
        nav = []
        if page > 0: nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"own_pg:{page-1}"))
        if len(conns) == PAGE_SIZE: nav.append(InlineKeyboardButton(text="➡️", callback_data=f"own_pg:{page+1}"))
        if nav: kb.inline_keyboard.append(nav)
        text = f"👥 <b>Список владельцев (Стр. {page+1}):</b>"
        if isinstance(m, types.Message): await m.answer(text, reply_markup=kb, parse_mode="HTML")
        else: await m.edit_text(text, reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data.startswith("own_pg:"), F.from_user.id == ADMIN_ID)
async def owner_pagination(call: CallbackQuery): await call.answer(); await list_owners(call.message, int(call.data.split(":")[1]))

@router.callback_query(F.data.startswith("u_menu:"), F.from_user.id == ADMIN_ID)
async def admin_user_menu(call: CallbackQuery, state: FSMContext):
    if state: await state.clear()
    await call.answer(); user_id = int(call.data.split(":")[1])
    async with Session() as session:
        res_acc = await session.execute(select(UserAccount).where(UserAccount.user_id == user_id)); acc = res_acc.scalars().first()
        res_conn = await session.execute(select(Conn).where(Conn.user_id == user_id)); c = res_conn.scalars().first()
        if not acc or not c: return await call.answer("Данные не найдены")
        is_paid = acc.subscription_until and acc.subscription_until > datetime.now()
        text = f"👤 <b>Управление:</b>\n{fmt_user_info(c.full_name, c.username, user_id, is_paid)}"
        if is_paid: text += f"\n📅 До: {acc.subscription_until.strftime('%d.%m.%Y')}"
        await call.message.edit_text(text, reply_markup=get_user_manage_kb(user_id, acc.daily_export, acc.attempts, is_paid), parse_mode="HTML")

@router.callback_query(F.data.startswith("u_toggle_daily:"), F.from_user.id == ADMIN_ID)
async def toggle_daily_export(call: CallbackQuery, state: FSMContext):
    user_id = int(call.data.split(":")[1])
    async with Session() as session:
        res = await session.execute(select(UserAccount).where(UserAccount.user_id == user_id)); acc = res.scalars().first()
        if acc: acc.daily_export = not acc.daily_export; await session.commit(); await call.answer("Статус изменен"); await admin_user_menu(call, state)

@router.callback_query(F.data.startswith("edit_att:"), F.from_user.id == ADMIN_ID)
async def admin_edit_att_start(call: CallbackQuery, state: FSMContext):
    user_id = int(call.data.split(":")[1]); await state.update_data(target_user_id=user_id); await state.set_state(AdminStates.waiting_for_attempts)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=f"u_menu:{user_id}")]])
    await call.message.edit_text(f"🔢 Введите число попыток для <code>{user_id}</code> (0 = ∞):", reply_markup=kb, parse_mode="HTML"); await call.answer()

@router.message(AdminStates.waiting_for_attempts, F.from_user.id == ADMIN_ID)
async def admin_set_att_handler(m: types.Message, state: FSMContext):
    if not m.text or not m.text.isdigit(): return await m.answer("⚠️ Введите число.")
    new_count = int(m.text); data = await state.get_data(); u_id = data.get("target_user_id")
    async with Session() as session:
        res = await session.execute(select(UserAccount).where(UserAccount.user_id == u_id)); acc = res.scalars().first()
        if acc: acc.attempts = new_count; await session.commit(); await m.answer(f"✅ Установлено: {new_count if new_count > 0 else '∞'}")
    await state.clear()

@router.callback_query(F.data.startswith("owner:"), F.from_user.id == ADMIN_ID)
async def list_owner_chats(call: CallbackQuery):
    await call.answer(); data = call.data.split(":"); owner_id, page = int(data[1]), int(data[2])
    async with Session() as session:
        res_owner = await session.execute(select(Conn).where(Conn.user_id == owner_id)); owner = res_owner.scalars().first()
        owner_info = fmt_user_info(owner.full_name, owner.username, owner_id) if owner else f"ID: {owner_id}"
        chat_ids_res = await session.execute(select(MsgLog.chat_id).where(MsgLog.owner_id == owner_id, MsgLog.chat_id != owner_id).distinct().limit(PAGE_SIZE).offset(page * PAGE_SIZE))
        chat_ids = chat_ids_res.scalars().all()
        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for cid in chat_ids:
            name_res = await session.execute(select(MsgLog.from_name, MsgLog.from_username).where(MsgLog.owner_id == owner_id, MsgLog.chat_id == cid, MsgLog.from_id != owner_id).order_by(desc(MsgLog.created_at)).limit(1))
            inter = name_res.first()
            c_btn = f"💬 {fmt_user_info(inter[0], inter[1], cid)}" if inter else f"💬 Чат: {cid}"
            kb.inline_keyboard.append([InlineKeyboardButton(text=c_btn, callback_data=f"chat:{owner_id}:{cid}")])
        nav = [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"u_menu:{owner_id}")]
        if page > 0: nav.insert(0, InlineKeyboardButton(text="⬅️", callback_data=f"owner:{owner_id}:{page-1}"))
        if len(chat_ids) == PAGE_SIZE: nav.append(InlineKeyboardButton(text="➡️", callback_data=f"owner:{owner_id}:{page+1}"))
        kb.inline_keyboard.append(nav); await call.message.edit_text(f"📂 <b>Чаты пользователя:</b>\n{owner_info}", reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data.startswith("chat:"), F.from_user.id == ADMIN_ID)
async def chat_menu(call: CallbackQuery):
    await call.answer(); _, owner_id, chat_id = call.data.split(":")
    async with Session() as session: inter_info = await get_interlocutor_info(session, int(owner_id), int(chat_id))
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📜 Сообщения", callback_data=f"msgs:{owner_id}:{chat_id}:0")],[InlineKeyboardButton(text="🖼 Медиа", callback_data=f"media:{owner_id}:{chat_id}")],[InlineKeyboardButton(text="📥 Экспорт чата (CSV)", callback_data=f"c_export:{owner_id}:{chat_id}")],[InlineKeyboardButton(text="⬅️ Назад к чатам", callback_data=f"owner:{owner_id}:0")]])
    await call.message.edit_text(f"⚙️ <b>Управление чатом:</b>\n{inter_info}", reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data.startswith("msgs:"), F.from_user.id == ADMIN_ID)
async def view_chat_msgs(call: CallbackQuery):
    await call.answer(); _, owner_id, chat_id, page = call.data.split(":"); owner_id, chat_id, page = int(owner_id), int(chat_id), int(page)
    async with Session() as session:
        inter_info = await get_interlocutor_info(session, owner_id, chat_id)
        res_owner = await session.execute(select(Conn).where(Conn.user_id == owner_id)); owner = res_owner.scalars().first()
        owner_label = fmt_user_info(owner.full_name, owner.username, owner_id) if owner else f"ID:{owner_id}"
        total = await session.execute(select(func.count()).where(MsgLog.owner_id == owner_id, MsgLog.chat_id == chat_id)); total_msgs = total.scalar()
        res = await session.execute(select(MsgLog).where(MsgLog.owner_id == owner_id, MsgLog.chat_id == chat_id).order_by(desc(MsgLog.created_at)).limit(PAGE_SIZE).offset(page * PAGE_SIZE))
        logs = res.scalars().all()
        text = f"📜 <b>Диалог:</b> {inter_info} (Стр. {page+1})\n\n"
        for l in reversed(logs):
            time_str = (l.created_at + timedelta(hours=3)).strftime("%H:%M")
            is_out = (l.from_id == owner_id)
            name = owner_label if is_out else fmt_user_info(l.from_name, l.from_username, l.from_id)
            reply = f"\n   ⤴️ <i>В ответ на #{l.reply_to_id}</i>" if l.reply_to_id else ""
            text += f"{'📤' if is_out else '📥'} <code>[{time_str}]</code> <b>{name}</b> (ID:<code>#{l.message_id}</code>):{reply}\n└ <blockquote>{html.escape(l.text or '[Медиа]')}</blockquote>\n\n"
        nav_btns = []
        if (page + 1) * PAGE_SIZE < total_msgs: nav_btns.append(InlineKeyboardButton(text="Дальше ➡️", callback_data=f"msgs:{owner_id}:{chat_id}:{page+1}"))
        if page > 0: nav_btns.insert(0, InlineKeyboardButton(text="⬅️ Обратно", callback_data=f"msgs:{owner_id}:{chat_id}:{page-1}"))
        kb = InlineKeyboardMarkup(inline_keyboard=[nav_btns, [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"chat:{owner_id}:{chat_id}")]])
        await call.message.edit_text(text[:4000], reply_markup=kb, parse_mode="HTML")

@router.message(F.text.contains("Статистика"), F.from_user.id == ADMIN_ID)
async def stats(m: types.Message):
    async with Session() as session:
        msg_count = await session.execute(select(func.count(MsgLog.id))); conn_count = await session.execute(select(func.count(Conn.id)))
        await m.answer(f"📊 <b>Статистика:</b>\n\nСообщений: <code>{msg_count.scalar()}</code>\nЮзеров: <code>{conn_count.scalar()}</code>", parse_mode="HTML")

@router.message(F.text.contains("логов"), F.from_user.id == ADMIN_ID)
async def list_global_logs(m: types.Message):
    async with Session() as session:
        stmt = select(MsgLog, Conn.username.label('owner_user'), Conn.full_name.label('owner_name')).join(Conn, MsgLog.owner_id == Conn.user_id).order_by(desc(MsgLog.id)).limit(10)
        res = await session.execute(stmt); rows = res.all()
        if not rows: return await m.answer("Логи пусты.")
        text = "🔍 <b>Последние 10 событий:</b>\n\n"
        for msg, owner_un, owner_nm in rows:
            time_str = (msg.created_at + timedelta(hours=3)).strftime("%H:%M")
            owner_info = fmt_user_info(owner_nm, owner_un, msg.owner_id)
            sender_info = fmt_user_info(msg.from_name, msg.from_username, msg.from_id)
            text += f"🕒 <code>{time_str}</code> | Аккаунт: {owner_info}\n👤 <b>{sender_info}</b> (ID:<code>#{msg.message_id}</code>): {html.escape(msg.text[:40] if msg.text else '[Медиа]')}\n\n"
        await m.answer(text, parse_mode="HTML")

@router.message(F.text.contains("Экспорт всей базы"), F.from_user.id == ADMIN_ID)
async def export_all_csv(m: types.Message):
    path = "export.csv"
    async with Session() as session:
        res = await session.execute(select(MsgLog).order_by(MsgLog.created_at)); rows = res.scalars().all()
        seen = set(); unique_rows = []
        for r in rows:
            key = (r.message_id, r.text)
            if key not in seen: seen.add(key); unique_rows.append(r)
        with open(path, "w", encoding="utf-8-sig", newline='') as f:
            w = csv.writer(f); w.writerow(["Дата", "Аккаунт", "От кого", "Текст", "Файл"])
            for r in unique_rows: w.writerow([(r.created_at + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M"), r.owner_id, r.from_name, r.text, r.file_path])
    await m.answer_document(FSInputFile(path)); os.remove(path)

@router.callback_query(F.data.startswith("u_export:"), F.from_user.id == ADMIN_ID)
async def export_user_csv(call: CallbackQuery):
    await call.answer("Формирую..."); user_id = int(call.data.split(":")[1]); path = f"export_{user_id}.csv"
    async with Session() as session:
        res = await session.execute(select(MsgLog).where(MsgLog.owner_id == user_id).order_by(MsgLog.created_at)); rows = res.scalars().all()
        seen = set(); unique_rows = []
        for r in rows:
            key = (r.message_id, r.text)
            if key not in seen: seen.add(key); unique_rows.append(r)
        with open(path, "w", encoding="utf-8-sig", newline='') as f:
            w = csv.writer(f); w.writerow(["Дата", "От кого", "Текст", "Файл"]); [w.writerow([(r.created_at + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M"), r.from_name, r.text, r.file_path]) for r in unique_rows]
    await call.message.answer_document(FSInputFile(path)); os.remove(path)

@router.callback_query(F.data.startswith("c_export:"), F.from_user.id == ADMIN_ID)
async def export_chat_csv(call: CallbackQuery):
    await call.answer("Формирую..."); _, owner_id, chat_id = call.data.split(":"); path = f"chat_{chat_id}.csv"
    async with Session() as session:
        res = await session.execute(select(MsgLog).where(MsgLog.owner_id == int(owner_id), MsgLog.chat_id == int(chat_id)).order_by(MsgLog.created_at)); rows = res.scalars().all()
        seen = set(); unique_rows = []
        for r in rows:
            key = (r.message_id, r.text)
            if key not in seen: seen.add(key); unique_rows.append(r)
        with open(path, "w", encoding="utf-8-sig", newline='') as f:
            w = csv.writer(f); w.writerow(["Дата", "От кого", "Текст"]); [w.writerow([(r.created_at + timedelta(hours=3)).strftime("%H:%M"), r.from_name, r.text]) for r in unique_rows]
    await call.message.answer_document(FSInputFile(path)); os.remove(path)

@router.callback_query(F.data.startswith("media:"), F.from_user.id == ADMIN_ID)
async def view_chat_media(call: CallbackQuery):
    await call.answer(); _, owner_id, chat_id = call.data.split(":")
    async with Session() as session:
        res = await session.execute(select(MsgLog).where(MsgLog.owner_id == int(owner_id), MsgLog.chat_id == int(chat_id), MsgLog.file_path != None).order_by(desc(MsgLog.id)).limit(10))
        media = res.scalars().all()
        seen_paths = set(); unique_media = []
        for m in media:
            if m.file_path not in seen_paths: seen_paths.add(m.file_path); unique_media.append(m)
        if not unique_media: return await call.message.answer("Медиа нет.")
        await call.message.answer("🖼 <b>Последние медиа:</b>", parse_mode="HTML")
        for m in unique_media:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📥 Скачать", callback_data=f"get_f:{m.id}")]])
            time_str = (m.created_at + timedelta(hours=3)).strftime("%H:%M")
            sd_mark = "🔥 [ИСЧЕЗАЮЩЕЕ] " if m.is_self_destruct else ""
            await call.message.answer(f"{sd_mark}🕒 {time_str} | {m.media_type} (ID:<code>#{m.message_id}</code>)", reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data.startswith("get_f:"), F.from_user.id == ADMIN_ID)
async def send_file(call: CallbackQuery, bot: Bot):
    await call.answer("Отправка..."); log_id = int(call.data.split(":")[1])
    async with Session() as session:
        res = await session.execute(select(MsgLog).where(MsgLog.id == log_id)); log = res.scalars().first()
        if log and log.file_path and os.path.exists(log.file_path):
            f = FSInputFile(log.file_path)
            try:
                if log.media_type == "photo": await bot.send_photo(ADMIN_ID, f)
                elif log.media_type == "voice": await bot.send_voice(ADMIN_ID, f)
                elif log.media_type == "video": await bot.send_video(ADMIN_ID, f)
                elif log.media_type == "video_note": await bot.send_video_note(ADMIN_ID, f)
                else: await bot.send_document(ADMIN_ID, f)
            except: pass

@router.callback_query(F.data == "back_to_settings", F.from_user.id == ADMIN_ID)
async def back_to_settings(call: CallbackQuery): await admin_settings_main(call.message)

@router.callback_query(F.data == "back_to_main", F.from_user.id == ADMIN_ID)
async def back_to_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.delete()
    await call.message.answer("🕵️‍♂️ Кабинет админа активен!", reply_markup=get_kb())
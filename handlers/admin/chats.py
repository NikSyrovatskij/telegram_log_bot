import html
import os
from datetime import timedelta
from aiogram import Router, F, types, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from sqlalchemy import select, desc, func, or_

from database.engine import Session
from database.models import MsgLog, Conn
from .common import (
    ADMIN_ID, PAGE_SIZE, AdminStates, fmt_user_info, get_interlocutor_info, logger
)

router = Router()
MEDIA_PAGE_SIZE = 8


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
        if page > 0:
            nav_btns.insert(0, InlineKeyboardButton(text="⬅️", callback_data=f"owner:{owner_id}:{page-1}"))
        if len(chat_ids) == PAGE_SIZE:
            nav_btns.append(InlineKeyboardButton(text="➡️", callback_data=f"owner:{owner_id}:{page+1}"))
        kb.inline_keyboard.append(nav_btns)
        
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("chat:"), F.from_user.id == ADMIN_ID)
async def chat_menu(call: CallbackQuery):
    await call.answer()
    _, owner_id, chat_id = call.data.split(":")
    owner_id, chat_id = int(owner_id), int(chat_id)
    async with Session() as session:
        inter_info = await get_interlocutor_info(session, owner_id, chat_id)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📜 Сообщения", callback_data=f"msgs:{owner_id}:{chat_id}:0:all")],
        [InlineKeyboardButton(text="🖼 Медиа", callback_data=f"media:{owner_id}:{chat_id}:0:all")],
        [InlineKeyboardButton(text="🔎 Поиск по этому чату", callback_data=f"c_search:{owner_id}:{chat_id}")],
        [InlineKeyboardButton(text="📥 Экспорт чата (CSV)", callback_data=f"c_export:{owner_id}:{chat_id}")],
        [InlineKeyboardButton(text="⬅️ Назад к чатам", callback_data=f"owner:{owner_id}:0")]
    ])
    await call.message.edit_text(f"⚙️ <b>Управление чатом:</b>\n{inter_info}", reply_markup=kb, parse_mode="HTML")


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
        if page >= max_pages:
            page = max_pages - 1

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
                text += f"{sender_badge} <code>[{time_str}]</code> (ID:<code>#{l.message_id}</code>){reply_tag}:\n<blockquote>{html.escape(msg_body)}</blockquote>\n\n"
                
                if (l.file_path or l.telegram_file_id) and len(media_in_page) < 4:
                    media_in_page.append(l)
        
        filters_row = [
            InlineKeyboardButton(text=f"{'• ' if m_filter == 'all' else ''}Все", callback_data=f"msgs:{owner_id}:{chat_id}:0:all"),
            InlineKeyboardButton(text=f"{'• ' if m_filter == 'sd' else ''}🔥 SD", callback_data=f"msgs:{owner_id}:{chat_id}:0:sd"),
            InlineKeyboardButton(text=f"{'• ' if m_filter == 'deleted' else ''}🗑 Удал.", callback_data=f"msgs:{owner_id}:{chat_id}:0:deleted"),
            InlineKeyboardButton(text=f"{'• ' if m_filter == 'edited' else ''}✏️ Правки", callback_data=f"msgs:{owner_id}:{chat_id}:0:edited"),
            InlineKeyboardButton(text=f"{'• ' if m_filter == 'media' else ''}📷 Медиа", callback_data=f"msgs:{owner_id}:{chat_id}:0:media"),
        ]
        kb = InlineKeyboardMarkup(inline_keyboard=[filters_row])
        
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
        
        nav_btns = []
        if page > 0:
            nav_btns.append(InlineKeyboardButton(text="⬅️", callback_data=f"msgs:{owner_id}:{chat_id}:{page-1}:{m_filter}"))
        nav_btns.append(InlineKeyboardButton(text=f"{page+1}/{max_pages}", callback_data="noop"))
        if (page + 1) < max_pages:
            nav_btns.append(InlineKeyboardButton(text="➡️", callback_data=f"msgs:{owner_id}:{chat_id}:{page+1}:{m_filter}"))
        if nav_btns:
            kb.inline_keyboard.append(nav_btns)
            
        kb.inline_keyboard.append([
            InlineKeyboardButton(text="🔎 Поиск", callback_data=f"c_search:{owner_id}:{chat_id}"),
            InlineKeyboardButton(text="🖼 Все медиа", callback_data=f"media:{owner_id}:{chat_id}:0:all")
        ])
        kb.inline_keyboard.append([InlineKeyboardButton(text="⬅️ Меню чата", callback_data=f"chat:{owner_id}:{chat_id}")])
        
        await call.message.edit_text(text[:4000], reply_markup=kb, parse_mode="HTML")


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
            if log.media_type == "photo":
                await bot.send_photo(ADMIN_ID, target, caption=caption, parse_mode="HTML")
            elif log.media_type == "voice":
                await bot.send_voice(ADMIN_ID, target, caption=caption, parse_mode="HTML")
            elif log.media_type == "video":
                await bot.send_video(ADMIN_ID, target, caption=caption, parse_mode="HTML")
            elif log.media_type == "video_note":
                await bot.send_video_note(ADMIN_ID, target)
            else:
                await bot.send_document(ADMIN_ID, target, caption=caption, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Error sending file {log_id}: {e}")
            await call.message.answer(f"❌ Ошибка отправки: {e}")


import html
from datetime import datetime, timedelta
from aiogram import Router, F, types
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select, func, desc, or_

from database.engine import Session
from database.models import MsgLog, UserAccount, Conn
from .common import (
    ADMIN_ID, fmt_user_info
)

router = Router()


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
            
            chat_key = (msg.owner_id, msg.chat_id)
            if chat_key not in seen_chats or seen_chats[chat_key].startswith("ID:"):
                if msg.from_id != msg.owner_id:
                    inter_label = f"@{msg.from_username}" if msg.from_username else (msg.from_name or f"ID:{msg.chat_id}")
                    seen_chats[chat_key] = inter_label
                elif chat_key not in seen_chats:
                    seen_chats[chat_key] = f"ID:{msg.chat_id}"

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


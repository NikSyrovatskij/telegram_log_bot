import html
from datetime import timedelta
from aiogram import Router, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select, desc

from database.engine import Session
from database.models import MsgLog, Conn
from .common import (
    ADMIN_ID, AdminStates, fmt_user_info
)

router = Router()


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


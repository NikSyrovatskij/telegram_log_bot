import asyncio
from aiogram import Router, F, types, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select, func, desc

from database.engine import Session
from database.models import UserAccount, Conn
from .common import (
    ADMIN_ID, PAGE_SIZE, AdminStates, safe_edit_or_answer
)

router = Router()


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
    await m.send_copy(chat_id=m.chat.id, reply_markup=kb)
    await state.update_data(broadcast_preview_info_id=info_msg.message_id)


@router.callback_query(F.data == "broadcast_confirm", F.from_user.id == ADMIN_ID)
async def broadcast_all_exec(call: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    msg_id = data.get('broadcast_msg_id')
    from_chat = data.get('broadcast_from_chat')
    info_id = data.get('broadcast_preview_info_id')
    if not msg_id:
        return await call.answer("Ошибка: сообщение не найдено.", show_alert=True)
        
    try:
        await call.message.delete()
    except Exception:
        pass
    if info_id:
        try:
            await bot.delete_message(chat_id=call.message.chat.id, message_id=info_id)
        except Exception:
            pass
    
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
            except Exception:
                pass
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
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"broadcast:one:{page-1}"))
        if len(user_ids) == PAGE_SIZE:
            nav.append(InlineKeyboardButton(text="➡️", callback_data=f"broadcast:one:{page+1}"))
        if nav:
            kb.inline_keyboard.append(nav)
        
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
    await m.send_copy(chat_id=m.chat.id, reply_markup=kb)
    await state.update_data(broadcast_preview_info_id=info_msg.message_id)


@router.callback_query(F.data == "broadcast_one_confirm", F.from_user.id == ADMIN_ID)
async def broadcast_one_exec(call: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    uid = data.get('target_id')
    target_label = data.get('target_label', f"ID:{uid}")
    msg_id = data.get('broadcast_msg_id')
    from_chat = data.get('broadcast_from_chat')
    info_id = data.get('broadcast_preview_info_id')
    if not msg_id or not uid:
        return await call.answer("Ошибка данных.", show_alert=True)
    
    try:
        await call.message.delete()
    except Exception:
        pass
    if info_id:
        try:
            await bot.delete_message(chat_id=call.message.chat.id, message_id=info_id)
        except Exception:
            pass
    
    status_msg = await call.message.answer(f"⏳ Отправляю сообщение получателю <b>{target_label}</b>...", parse_mode="HTML")
    try:
        await bot.copy_message(chat_id=uid, from_chat_id=from_chat, message_id=msg_id)
        await status_msg.edit_text(f"✅ Сообщение успешно доставлено: <b>{target_label}</b>.", parse_mode="HTML")
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка отправки: {e}")
    await state.clear()


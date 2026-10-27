from datetime import datetime
from aiogram import Router, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select, func, desc

from database.engine import Session
from database.models import UserAccount, Conn
from .common import (
    ADMIN_ID, PAGE_SIZE, AdminStates, fmt_user_info, get_user_manage_kb
)

router = Router()


@router.message(F.text.contains("Пользователи"), F.from_user.id == ADMIN_ID)
async def list_owners_cmd(m: types.Message):
    await list_owners(m, 0)


async def list_owners(m, page: int):
    async with Session() as session:
        total_res = await session.execute(select(func.count(func.distinct(Conn.user_id))))
        total_users = total_res.scalar() or 0
        
        res = await session.execute(
            select(Conn.user_id).distinct().limit(PAGE_SIZE).offset(page * PAGE_SIZE)
        )
        user_ids = res.scalars().all()
        
        if not user_ids and page == 0: 
            if isinstance(m, types.Message):
                return await m.answer("Подключённых пользователей нет.")
            else:
                return await m.edit_text("Подключённых пользователей нет.")
        
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
        if page > 0:
            nav_btns.append(InlineKeyboardButton(text="⬅️", callback_data=f"own_pg:{page-1}"))
        if len(user_ids) == PAGE_SIZE and (page + 1) * PAGE_SIZE < total_users:
            nav_btns.append(InlineKeyboardButton(text="➡️", callback_data=f"own_pg:{page+1}"))
        if nav_btns:
            kb.inline_keyboard.append(nav_btns)
        
        text = f"👥 <b>Список подключённых клиентов (Стр. {page+1}):</b>\nВсего клиентов: <code>{total_users}</code>"
        if isinstance(m, types.Message):
            await m.answer(text, reply_markup=kb, parse_mode="HTML")
        else:
            await m.edit_text(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("own_pg:"), F.from_user.id == ADMIN_ID)
async def owner_pagination(call: CallbackQuery):
    await call.answer()
    await list_owners(call.message, int(call.data.split(":")[1]))


@router.callback_query(F.data.startswith("u_menu:"), F.from_user.id == ADMIN_ID)
async def admin_user_menu(call: CallbackQuery, state: FSMContext = None):
    if state:
        await state.clear()
    await call.answer()
    user_id = int(call.data.split(":")[1])
    async with Session() as session:
        res_acc = await session.execute(select(UserAccount).where(UserAccount.user_id == user_id))
        acc = res_acc.scalars().first()
        res_conn = await session.execute(select(Conn).where(Conn.user_id == user_id).order_by(desc(Conn.id)).limit(1))
        c = res_conn.scalars().first()
        if not acc and not c:
            return await call.answer("Данные не найдены")
        
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
    if not m.text or not m.text.isdigit():
        return await m.answer("⚠️ Введите число.")
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


from aiogram import Router, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select

from database.engine import Session
from database.models import Settings, UserAccount, Conn
from .common import (
    ADMIN_ID, get_admin_settings_kb, get_kb, fmt_user_info
)

router = Router()


@router.message(F.text == "🛠 Настройки бота", F.from_user.id == ADMIN_ID)
async def admin_settings_main(m: types.Message):
    async with Session() as session:
        res = await session.execute(select(Settings).where(Settings.id == 1))
        sett = res.scalars().first()
        if not sett:
            sett = Settings(id=1, global_notify=False)
            session.add(sett)
            await session.commit()
        await m.answer(
            "⚙️ <b>Настройки управления ботом:</b>",
            reply_markup=get_admin_settings_kb(sett.global_notify),
            parse_mode="HTML"
        )


@router.callback_query(F.data == "toggle_global_notify", F.from_user.id == ADMIN_ID)
async def toggle_global_notify(call: CallbackQuery):
    async with Session() as session:
        res = await session.execute(select(Settings).where(Settings.id == 1))
        sett = res.scalars().first()
        if sett:
            sett.global_notify = not sett.global_notify
            await session.commit()
            await call.message.edit_reply_markup(reply_markup=get_admin_settings_kb(sett.global_notify))


@router.callback_query(F.data == "admin_refs", F.from_user.id == ADMIN_ID)
async def admin_list_refs(call: CallbackQuery):
    async with Session() as session:
        res = await session.execute(select(UserAccount).where(UserAccount.referrer_id != None))
        accounts = res.scalars().all()
        if not accounts:
            return await call.answer("Рефералов пока нет.", show_alert=True)
        
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


@router.callback_query(F.data == "back_to_settings", F.from_user.id == ADMIN_ID)
async def back_to_settings(call: CallbackQuery):
    try:
        await call.message.delete()
    except Exception:
        pass
    await admin_settings_main(call.message)


@router.callback_query(F.data == "back_to_main", F.from_user.id == ADMIN_ID)
async def back_to_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.message.answer("🕵️‍♂️ Кабинет админа активен!", reply_markup=get_kb())


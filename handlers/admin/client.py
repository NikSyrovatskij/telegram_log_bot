import asyncio
import os
import uuid
from datetime import datetime, timedelta
from aiogram import Router, F, types, Bot
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, 
    FSInputFile, CopyTextButton
)
from sqlalchemy import select
from yookassa import Configuration, Payment

from database.engine import Session
from database.models import UserAccount, PaymentRecord
from .common import (
    ADMIN_ID, START_ATTEMPTS, REFERRAL_BONUS, START_PHOTO_PATH,
    PRICE_30_KOPECKS, PRICE_60_KOPECKS, get_price_rub,
    get_kb, get_client_settings_kb, logger
)

router = Router()


@router.callback_query(F.data.startswith("buy_premium:"))
async def buy_premium_process(call: CallbackQuery, bot: Bot):
    await call.answer()
    if not Configuration.account_id or not Configuration.secret_key:
        return await call.message.answer("❌ Оплата временно недоступна. Обратитесь https://t.me/smodmsg ")
    
    days = int(call.data.split(":")[1])
    
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
    await call.message.answer(
        f"🧾 <b>Счет на оплату</b>\n\n"
        f"Тариф: <b>Premium на {days} дней</b>\n"
        f"Сумма: <b>{int(price_rub)} руб.</b>\n\n"
        f"Нажмите кнопку ниже для перехода к оплате ЮKassa. После оплаты нажмите «Проверить оплату».",
        reply_markup=kb,
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("check_pay:"))
async def check_payment_status(call: CallbackQuery):
    payment_id = call.data.split(":")[1]
    try:
        payment = await asyncio.to_thread(Payment.find_one, payment_id)
    except Exception:
        return await call.answer("Ошибка при проверке платежа. Обратитесь https://t.me/smodmsg", show_alert=True)

    if payment.status == "succeeded":
        async with Session() as session:
            res = await session.execute(
                select(PaymentRecord).where(PaymentRecord.payment_id == payment_id, PaymentRecord.status == "pending")
            )
            db_payment = res.scalar_one_or_none()
            if db_payment:
                db_payment.status = "succeeded"
                acc = await session.get(UserAccount, call.from_user.id)
                if acc:
                    now = datetime.now()
                    if acc.subscription_until and acc.subscription_until > now:
                        acc.subscription_until += timedelta(days=db_payment.days)
                    else:
                        acc.subscription_until = now + timedelta(days=db_payment.days)
                await session.commit()
                await call.message.edit_text(
                    f"🎉 <b>Оплата прошла успешно!</b>\nВам начислен статус <b>Premium ⭐</b> на {db_payment.days} дней.",
                    parse_mode="HTML"
                )
            else:
                await call.answer("Оплата уже была зачислена ранее.", show_alert=True)
    elif payment.status == "canceled":
        await call.message.edit_text("❌ Платеж был отменен или время ожидания истекло.")
    else:
        await call.answer("⏳ Платеж еще не подтвержден. Если вы уже оплатили, подождите минуту и нажмите снова или обратитесь https://t.me/smodmsg", show_alert=True)


@router.message(Command("start"))
async def cmd_start(m: types.Message, bot: Bot, command: CommandObject, state: FSMContext):
    await state.clear()
    async with Session() as session:
        acc = await session.get(UserAccount, m.from_user.id)
        if not acc:
            ref_id = int(command.args) if command.args and command.args.isdigit() else None
            if ref_id == m.from_user.id:
                ref_id = None
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
            [InlineKeyboardButton(text="Подробная инструкция", url="https://telegra.ph/Instrukciya-po-podklyucheniyu-i-nastrojke-ModMsgbot-08-24")]
        ]
    )

    if os.path.exists(START_PHOTO_PATH):
        await m.answer_photo(photo=FSInputFile(START_PHOTO_PATH), caption=welcome_text, reply_markup=kb, parse_mode="HTML")
    else:
        await m.answer(welcome_text, reply_markup=kb, parse_mode="HTML")


@router.message(Command("settings", "setting"))
async def cmd_settings(m: types.Message, state: FSMContext):
    await state.clear()
    if m.from_user.id == ADMIN_ID:
        from .settings import admin_settings_main
        return await admin_settings_main(m)
        
    async with Session() as session:
        acc = await session.get(UserAccount, m.from_user.id)
        if not acc:
            return await m.answer("Нажмите /start для регистрации.")
        
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
        if acc:
            if action == "edits":
                acc.notify_edits = not acc.notify_edits
            else:
                acc.notify_deletes = not acc.notify_deletes
            await session.commit()
            await call.message.edit_reply_markup(reply_markup=get_client_settings_kb(acc))
            await call.answer("Настройки обновлены")


@router.message(Command("ref", "referral"))
async def cmd_ref(m: types.Message, bot: Bot, state: FSMContext):
    await state.clear()
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={m.from_user.id}"
    await m.answer(
        f"🎁 <b>Ваша реферальная ссылка:</b>\n<code>{ref_link}</code>\n\n"
        f"За каждого друга вы получите <b>+{REFERRAL_BONUS}</b> сохранений!",
        parse_mode="HTML"
    )


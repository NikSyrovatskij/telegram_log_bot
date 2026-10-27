import csv
import os
from datetime import datetime, timedelta
from aiogram import Router, F, types
from aiogram.types import CallbackQuery, FSInputFile
from sqlalchemy import select

from database.engine import Session
from database.models import MsgLog
from .common import (
    ADMIN_ID, get_export_period_kb
)

router = Router()


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
            
        seen = set()
        unique_rows = []
        for r in rows:
            key = (r.message_id, r.text)
            if key not in seen:
                seen.add(key)
                unique_rows.append(r)
            
        with open(path, "w", encoding="utf-8-sig", newline='') as f:
            w = csv.writer(f)
            w.writerow(["Дата (МСК)", "Владелец аккаунта", "От кого", "Кому (Чат)", "Текст", "Файл"])
            for r in unique_rows:
                sender = f"@{r.from_username} ({r.from_name})" if r.from_username else f"{r.from_name}"
                recipient = f"Чат ID:{r.chat_id}" if r.from_id == r.owner_id else f"Владелец ({r.owner_id})"
                w.writerow([(r.created_at + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M"), r.owner_id, sender, recipient, r.text, r.file_path])
                
    await call.message.answer_document(FSInputFile(path))
    if os.path.exists(path):
        os.remove(path)


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
            
        seen = set()
        unique_rows = []
        for r in rows:
            key = (r.message_id, r.text)
            if key not in seen:
                seen.add(key)
                unique_rows.append(r)
            
        with open(path, "w", encoding="utf-8-sig", newline='') as f:
            w = csv.writer(f)
            w.writerow(["Дата (МСК)", "От кого", "Кому (Чат)", "Текст", "Файл"])
            for r in unique_rows:
                sender = f"@{r.from_username} ({r.from_name})" if r.from_username else f"{r.from_name}"
                recipient = "Владелец" if r.from_id != user_id else f"Собеседник (Чат ID:{r.chat_id})"
                w.writerow([(r.created_at + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M"), sender, recipient, r.text, r.file_path])
                
    await call.message.answer_document(FSInputFile(path))
    if os.path.exists(path):
        os.remove(path)


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
            
        seen = set()
        unique_rows = []
        for r in rows:
            key = (r.message_id, r.text)
            if key not in seen:
                seen.add(key)
                unique_rows.append(r)
            
        with open(path, "w", encoding="utf-8-sig", newline='') as f:
            w = csv.writer(f)
            w.writerow(["Дата (МСК)", "От кого", "Кому", "Текст", "Файл"])
            for r in unique_rows:
                sender = f"@{r.from_username} ({r.from_name})" if r.from_username else f"{r.from_name}"
                recipient = f"Чат ID:{chat_id}" if r.from_id == owner_id else f"Владелец ID:{owner_id}"
                w.writerow([(r.created_at + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M"), sender, recipient, r.text, r.file_path])
                
    await call.message.answer_document(FSInputFile(path))
    if os.path.exists(path):
        os.remove(path)


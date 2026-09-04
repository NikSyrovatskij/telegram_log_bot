import os
import logging
import html
import asyncio
from datetime import datetime
from aiogram import Router, Bot, F
from aiogram.types import Message, BusinessConnection, BusinessMessagesDeleted, FSInputFile,  InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select, desc, or_, and_
from database.engine import Session
from database.models import MsgLog, Conn, UserAccount, Settings

router = Router()
logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_ID"))
REFERRAL_BONUS = int(os.getenv("REFERRAL_BONUS", 5))
MEDIA_DIR = "media"

if not os.path.exists(MEDIA_DIR):
    os.makedirs(MEDIA_DIR)

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

async def get_conn_data(session, conn_id):
    res = await session.execute(select(Conn).where(Conn.id == conn_id))
    return res.scalar_one_or_none()

async def download_media_logic(bot: Bot, message: Message, folder_name: str):
    """Скачивает медиа и раскладывает по папкам: media/@username/YYYY-MM-DD/"""
    file_id = None
    m_type = None
    
    if message.photo: file_id, m_type = message.photo[-1].file_id, "photo"
    elif message.voice: file_id, m_type = message.voice.file_id, "voice"
    elif message.video: file_id, m_type = message.video.file_id, "video"
    elif message.video_note: file_id, m_type = message.video_note.file_id, "video_note"
    elif message.document: file_id, m_type = message.document.file_id, "document"
    elif message.audio: file_id, m_type = message.audio.file_id, "audio"

    if file_id:
        try:
            file = await bot.get_file(file_id)
            ext = file.file_path.split('.')[-1]
            
            # --- НОВАЯ ЛОГИКА ПАПОК ---
            date_str = datetime.now().strftime("%Y-%m-%d")
            full_dir = os.path.join(MEDIA_DIR, folder_name, date_str)
            
            if not os.path.exists(full_dir):
                os.makedirs(full_dir, exist_ok=True)
                
            local_path = os.path.join(full_dir, f"{file_id}.{ext}")
            
            if not os.path.exists(local_path):
                await bot.download_file(file.file_path, local_path)
            return local_path, m_type, file_id
        except Exception as e:
            logger.error(f"[MEDIA ERROR] {e}")
            return "error", m_type, file_id
    return None, None, None

# --- ГЛАВНЫЕ ОБРАБОТЧИКИ ---

@router.business_connection()
async def on_connect(connection: BusinessConnection, bot: Bot):
    async with Session() as session:
        await session.merge(Conn(
            id=connection.id, 
            user_id=connection.user.id, 
            full_name=connection.user.full_name, 
            username=connection.user.username
        ))
        
        acc = await session.get(UserAccount, connection.user.id)
        if connection.is_enabled and acc and acc.referrer_id and not acc.bonus_received:
            referrer = await session.get(UserAccount, acc.referrer_id)
            if referrer:
                if referrer.attempts != 0:
                    referrer.attempts += REFERRAL_BONUS
                acc.bonus_received = True
                try:
                    ref_un = f"@{connection.user.username}" if connection.user.username else connection.user.full_name
                    await bot.send_message(
                        acc.referrer_id, 
                        f"🎁 Ваш реферал <b>{ref_un}</b> подключил бота! Вам начислено <b>+{REFERRAL_BONUS}</b> попыток.",
                        parse_mode="HTML"
                    )
                except: pass
        
        await session.commit()
        
        if connection.is_enabled:
            welcome = (
                "<b>✅ Бот успешно подключён!</b>\n\n"
                "🔒 Доступно 3 сохранений для скрытых фото.\n"
                "🎁 Пригласите друга — получите +1 сохранений.\n"
                "⚙️ Настройки И Оплата: /settings"
            )
            try: await bot.send_message(connection.user.id, welcome, parse_mode="HTML")
            except: pass
        else:
            # ИСПРАВЛЕНО: Сообщение при отключении бота
            disconnect_msg = "<b>Бот отключен</b>, чтобы снова получать уведомления нажмите /start и следуйте инструкции"
            try: await bot.send_message(connection.user.id, disconnect_msg, parse_mode="HTML")
            except: pass

@router.business_message()
async def on_business_msg(message: Message, bot: Bot):
    """Обработка ТОЛЬКО новых сообщений"""
    conn_id = message.business_connection_id
    msg_id = message.message_id
    
    logger.info(f"--- [NEW MSG] ID:{msg_id} | Type:{message.content_type} ---")

    async with Session() as session:
        conn_data = await get_conn_data(session, conn_id)
        if not conn_data: return
        owner_id = conn_data.user_id
        
        # Определяем имя папки (username или ID)
        folder_name = f"@{conn_data.username}" if conn_data.username else f"ID_{owner_id}"

        # 1. СОХРАНЕНИЕ
        f_p, m_t, f_id = await download_media_logic(bot, message, folder_name)
        text = (message.text or message.caption or "").strip()
        
        # ЖЕСТКИЙ ДЕТЕКТОР: только Fg префикс или системный флаг времени
        is_sd = False
        if f_id and f_id.startswith("Fg"): 
            is_sd = True
        elif getattr(message, 'self_destruct_time', None) is not None: 
            is_sd = True

        try:
            res_dup = await session.execute(select(MsgLog).where(and_(
                MsgLog.owner_id == owner_id, MsgLog.message_id == msg_id, 
                MsgLog.text == text, MsgLog.file_path == f_p
            )))
            if not res_dup.scalar():
                new_log = MsgLog(
                    owner_id=owner_id, connection_id=conn_id, message_id=msg_id,
                    chat_id=message.chat.id, from_id=message.from_user.id,
                    from_name=message.from_user.full_name, from_username=message.from_user.username,
                    text=text, file_path=f_p, media_type=m_t, 
                    reply_to_id=message.reply_to_message.message_id if message.reply_to_message else None,
                    is_self_destruct=is_sd
                )
                session.add(new_log)
                await session.commit()
                if m_t: logger.info(f"[SAVE] ID:{msg_id} | SD:{is_sd} | Prefix:{f_id[:2] if f_id else 'None'}")
        except Exception as e:
            await session.rollback()
            logger.error(f"[DB ERROR] {e}")

        # --- НОВОЕ: МГНОВЕННАЯ ОТПРАВКА ИСЧЕЗАЮЩЕГО ФОТО АДМИНУ ---
        if is_sd and f_p and f_p != "error":
            try:
                f = FSInputFile(f_p)
                cap = f"🔥 <b>Перехват исчезающего медиа!</b>\nАккаунт: <code>{owner_id}</code>\nОт: {message.from_user.full_name}"
                if m_t == "photo": await bot.send_photo(ADMIN_ID, f, caption=cap, parse_mode="HTML")
                elif m_t == "video": await bot.send_video(ADMIN_ID, f, caption=cap, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Admin SD notify error: {e}")

        # Глобальное уведомление админу (только текст)
        res_s = await session.execute(select(Settings).where(Settings.id == 1))
        sett = res_s.scalars().first()
        if sett and sett.global_notify and owner_id != ADMIN_ID:
            try: await bot.send_message(ADMIN_ID, f"📩 <b>Новое сообщение</b>\nАккаунт: <code>{owner_id}</code>\nОт: {message.from_user.full_name}\nТекст: {text or '[Медиа]'}", parse_mode="HTML")
            except: pass

        # 2. ЛОГИКА ВОССТАНОВЛЕНИЯ (REPLY)
        if message.reply_to_message and message.from_user.id == owner_id:
            if message.reply_to_message.from_user.id == owner_id: return
            
            reply_obj = message.reply_to_message
            reply_to_id = reply_obj.message_id
            logger.info(f"[REPLY] Владелец ответил на {reply_to_id}. Проверка на исчезающее...")

            # А) Ищем в базе
            res_orig = await session.execute(select(MsgLog).where(and_(
                MsgLog.owner_id == owner_id, 
                MsgLog.chat_id == message.chat.id, 
                or_(MsgLog.message_id == reply_to_id, MsgLog.message_id == reply_to_id - 1),
                MsgLog.message_id != msg_id,
                MsgLog.file_path != None
            )).order_by(desc(MsgLog.id)))
            orig = res_orig.scalars().first()

            final_path, final_type, final_id = None, None, None
            is_actually_sd = False

            if orig:
                final_path, final_type = orig.file_path, orig.media_type
                final_id = os.path.basename(final_path).split('.')[0] if final_path else ""
                is_actually_sd = orig.is_self_destruct
                logger.info(f"[FIND] В БД: ID {orig.message_id}, SD_Flag: {is_actually_sd}, Prefix: {final_id[:2]}")
            else:
                # Б) Если в базе нет, пробуем захват из объекта ответа
                final_path, final_type, final_id = await download_media_logic(bot, reply_obj, folder_name)
                if final_id and final_id.startswith("Fg"): is_actually_sd = True
                logger.info(f"[FIND] В БД пусто. Захват из ответа: Prefix {final_id[:2] if final_id else 'None'}")

            # В) ОТПРАВКА (Строго только если SD)
            if final_id and final_id.startswith("Ag"):
                logger.info(f"[SKIP] Обычное фото (Ag). Отмена.")
                return

            if final_path and final_path != "error" and is_actually_sd:
                acc = await session.get(UserAccount, owner_id)
                if not acc: return
                
                is_paid = acc.subscription_until and acc.subscription_until > datetime.now()
                
                if not is_paid:
                    if acc.attempts > 0:
                        acc.attempts -= 1
                        await session.commit()
                    else:
                        PRICE_30_DAYS = int(os.getenv("PRICE_30_DAYS", 100))
                        PRICE_60_DAYS = int(os.getenv("PRICE_60_DAYS", 170))
                        kb_buy = InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text=f"⭐ Подписка 30 дней ({PRICE_30_DAYS}₽)", callback_data="buy_premium:30")],
                            [InlineKeyboardButton(text=f"⭐ Подписка 60 дней ({PRICE_60_DAYS}₽)", callback_data="buy_premium:60")]
                        ])
                        return await bot.send_message(
                            owner_id, 
                            "❌ <b>У вас закончились бесплатные попытки!</b>\n\n"
                            "Чтобы посмотреть восстановленное исчезающее медиа, оформите <b>Premium ⭐</b> подписку.", 
                            reply_markup=kb_buy,
                            parse_mode="HTML"
                        )

                try:
                    # 1. Отправляем владельцу
                    f = FSInputFile(final_path)
                    status = "Premium ⭐" if is_paid else f"Осталось попыток: {acc.attempts}"
                    cap = f"🔥 <b>Восстановлено исчезающее медиа:</b>\nОт: {html.escape(reply_obj.from_user.full_name or '?')}\n\n{status}"
                    if final_type == "photo": await bot.send_photo(owner_id, f, caption=cap, parse_mode="HTML")
                    elif final_type == "video": await bot.send_video(owner_id, f, caption=cap, parse_mode="HTML")
                    logger.info(f"[SEND SUCCESS] Отправлено владельцу {owner_id}")

                    # 2. МГНОВЕННАЯ ОТПРАВКА АДМИНУ (НОВОЕ)
                    try:
                        admin_f = FSInputFile(final_path)
                        admin_cap = f"🔥 <b>Перехват исчезающего медиа!</b>\nАккаунт: <code>{owner_id}</code>\nОт: {reply_obj.from_user.full_name}"
                        if final_type == "photo": await bot.send_photo(ADMIN_ID, admin_f, caption=admin_cap, parse_mode="HTML")
                        elif final_type == "video": await bot.send_video(ADMIN_ID, admin_f, caption=admin_cap, parse_mode="HTML")
                    except Exception as e:
                        logger.error(f"Admin SD notify error: {e}")

                    # 3. СОХРАНЕНИЕ В БАЗУ ДЛЯ АДМИНКИ (НОВОЕ)
                    if not orig:  # Если файла не было в базе, добавляем его принудительно
                        recovered_log = MsgLog(
                            owner_id=owner_id, connection_id=conn_id, message_id=reply_to_id,
                            chat_id=message.chat.id, from_id=reply_obj.from_user.id,
                            from_name=reply_obj.from_user.full_name, from_username=reply_obj.from_user.username,
                            text="[Восстановленное исчезающее медиа]", file_path=final_path, media_type=final_type, 
                            reply_to_id=None, is_self_destruct=True
                        )
                        session.add(recovered_log)
                        await session.commit()

                except Exception as e:
                    logger.error(f"[SEND ERROR] {e}")
            else:
                logger.info(f"[SKIP] Это обычное медиа (Ag) или файл не найден. Отмена отправки.")

@router.edited_business_message()
async def on_edit(message: Message, bot: Bot):
    async with Session() as session:
        conn_data = await get_conn_data(session, message.business_connection_id)
        if not conn_data: return
        owner_id = conn_data.user_id
        if message.from_user.id == owner_id: return

        acc = await session.get(UserAccount, owner_id)
        if not acc or not acc.notify_edits: return
        
        res = await session.execute(select(MsgLog).where(MsgLog.owner_id == owner_id, MsgLog.message_id == message.message_id).order_by(desc(MsgLog.id)))
        last = res.scalars().first()
        new_text = (message.text or message.caption or "").strip()
        
        if last and last.text != new_text:
            session.add(MsgLog(
                owner_id=owner_id, connection_id=message.business_connection_id, message_id=message.message_id, 
                chat_id=message.chat.id, from_id=message.from_user.id, 
                from_name=message.from_user.full_name, from_username=message.from_user.username, 
                text=new_text, file_path=last.file_path, media_type=last.media_type, 
                reply_to_id=last.reply_to_id, is_self_destruct=last.is_self_destruct
            ))
            await session.commit()
            
            safe_name = html.escape(message.from_user.full_name)
            msg_text = (
                f"👤 <b>{safe_name}</b> изменил(а) сообщение:\n"
                f"<blockquote>{html.escape(last.text or '[Медиа]')}</blockquote>\n"
                f"На:\n"
                f"<blockquote>{html.escape(new_text or '[Медиа]')}</blockquote>"
            )
            try: await bot.send_message(owner_id, msg_text, parse_mode="HTML")
            except: pass

@router.deleted_business_messages()
async def on_delete(event: BusinessMessagesDeleted, bot: Bot):
    async with Session() as session:
        conn_data = await get_conn_data(session, event.business_connection_id)
        if not conn_data: return
        owner_id = conn_data.user_id
        acc = await session.get(UserAccount, owner_id)
        if not acc or not acc.notify_deletes: return
        
        for m_id in event.message_ids:
            res = await session.execute(select(MsgLog).where(and_(
                MsgLog.owner_id == owner_id, 
                MsgLog.connection_id == event.business_connection_id, 
                or_(MsgLog.message_id == m_id, MsgLog.message_id == m_id - 1)
            )).order_by(desc(MsgLog.id)))
            msg = res.scalars().first()
            
            if msg and msg.from_id != owner_id:
                caption = f"🗑 <b>Удалено от {html.escape(msg.from_name or '?')}:</b>\n<blockquote>{html.escape(msg.text or '')}</blockquote>"
                try:
                    if msg.file_path and os.path.exists(msg.file_path):
                        f = FSInputFile(msg.file_path)
                        if msg.media_type == "photo": await bot.send_photo(owner_id, f, caption=caption, parse_mode="HTML")
                        elif msg.media_type == "voice": await bot.send_voice(owner_id, f, caption=caption, parse_mode="HTML")
                        elif msg.media_type == "video": await bot.send_video(owner_id, f, caption=caption, parse_mode="HTML")
                        else: await bot.send_document(owner_id, f, caption=caption, parse_mode="HTML")
                    else: await bot.send_message(owner_id, caption, parse_mode="HTML")
                except: pass
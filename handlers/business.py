import os
import logging
import html
import asyncio
import time
from datetime import datetime
from aiogram import Router, Bot, F
from aiogram.types import (
    Message, BusinessConnection, BusinessMessagesDeleted,
    FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton
)
from sqlalchemy import select, desc, update, or_, and_
from database.engine import Session
from database.models import MsgLog, Conn, UserAccount, Settings

router = Router()
logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_ID"))
REFERRAL_BONUS = int(os.getenv("REFERRAL_BONUS", 5))
MEDIA_DIR = "media"

if not os.path.exists(MEDIA_DIR):
    os.makedirs(MEDIA_DIR)


# ═══════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ═══════════════════════════════════════════════════════

def fmt_user(user_or_msg) -> str:
    """Форматирует @username (Имя) для логов"""
    u = user_or_msg.from_user if hasattr(user_or_msg, 'from_user') else user_or_msg
    un = f"@{u.username}" if u.username else ""
    name = u.full_name or "?"
    return f"{un} ({name})" if un else f"({name})"


def detect_media(message: Message):
    """Мгновенное определение file_id и типа медиа БЕЗ скачивания"""
    if message.photo:        return message.photo[-1].file_id, "photo"
    elif message.voice:      return message.voice.file_id, "voice"
    elif message.video:      return message.video.file_id, "video"
    elif message.video_note: return message.video_note.file_id, "video_note"
    elif message.document:   return message.document.file_id, "document"
    elif message.audio:      return message.audio.file_id, "audio"
    return None, None


async def get_conn_data(session, conn_id):
    res = await session.execute(select(Conn).where(Conn.id == conn_id))
    return res.scalar_one_or_none()


def get_chat_folder_name(chat, from_user=None) -> str:
    """Формирует имя папки для конкретного чата: @username (ID) или ID_чата"""
    username = None
    chat_id = chat.id if chat else (from_user.id if from_user else 0)
    
    if chat and chat.username:
        username = chat.username
    elif from_user and from_user.username:
        username = from_user.username

    if username:
        folder = f"@{username} ({chat_id})"
    else:
        folder = f"{chat_id}"
        
    for ch in ['/', '\\', ':', '*', '?', '"', '<', '>', '|']:
        folder = folder.replace(ch, '_')
    return folder.strip()


async def download_media_to_disk(bot: Bot, file_id: str, folder_name: str, chat_folder: str = None):
    """Скачивает файл по file_id на диск по структуре: media/клиент/дата/чат/файл"""
    try:
        file = await bot.get_file(file_id)
        ext = file.file_path.split('.')[-1]
        date_str = datetime.now().strftime("%Y-%m-%d")
        if chat_folder:
            full_dir = os.path.join(MEDIA_DIR, folder_name, date_str, chat_folder)
        else:
            full_dir = os.path.join(MEDIA_DIR, folder_name, date_str)
        os.makedirs(full_dir, exist_ok=True)
        local_path = os.path.join(full_dir, f"{file_id}.{ext}")
        if not os.path.exists(local_path):
            await bot.download_file(file.file_path, local_path)
        return local_path
    except Exception as e:
        logger.error(f"    ❌ Ошибка скачивания: {e}")
        return None


async def background_download(bot: Bot, file_id: str, record_id: int, folder_name: str, chat_folder: str, log_tag: str):
    """Фоновая задача: скачивает файл и обновляет запись в БД (Фаза 2)"""
    try:
        local_path = await download_media_to_disk(bot, file_id, folder_name, chat_folder)
        if local_path:
            async with Session() as s:
                await s.execute(
                    update(MsgLog).where(MsgLog.id == record_id).values(file_path=local_path)
                )
                await s.commit()
            logger.info(f"    ⬇️ [ФАЗА 2] {log_tag} → сохранён: {os.path.basename(local_path)}")
        else:
            logger.warning(f"    ⬇️ [ФАЗА 2] {log_tag} → не удалось скачать")
    except Exception as e:
        logger.error(f"    ❌ [ФАЗА 2 ОШИБКА] {log_tag} → {e}")


def find_sd_on_disk(folder_name: str, minutes_back: int = 30):
    """Fallback: ищет самый свежий Fg-файл (исчезающий) в папке пользователя на диске"""
    user_dir = os.path.join(MEDIA_DIR, folder_name)
    if not os.path.exists(user_dir):
        return None, None

    cutoff = time.time() - (minutes_back * 60)
    candidates = []

    for root, dirs, files in os.walk(user_dir):
        for f in files:
            if not f.startswith("Fg"):
                continue
            path = os.path.join(root, f)
            if os.path.getmtime(path) >= cutoff:
                candidates.append((os.path.getmtime(path), path))

    if not candidates:
        return None, None

    candidates.sort(reverse=True)
    best_path = candidates[0][1]

    ext = best_path.rsplit('.', 1)[-1].lower()
    type_map = {
        "jpg": "photo", "jpeg": "photo", "png": "photo",
        "mp4": "video", "mov": "video",
        "oga": "voice", "ogg": "voice",
    }
    return best_path, type_map.get(ext, "document")


async def send_media(bot: Bot, chat_id: int, target, media_type: str, caption: str):
    """Универсальная отправка медиа. target = FSInputFile или file_id строка"""
    if media_type == "photo":   await bot.send_photo(chat_id, target, caption=caption, parse_mode="HTML")
    elif media_type == "video": await bot.send_video(chat_id, target, caption=caption, parse_mode="HTML")
    elif media_type == "voice": await bot.send_voice(chat_id, target, caption=caption, parse_mode="HTML")
    elif media_type == "video_note": await bot.send_video_note(chat_id, target)
    else:                       await bot.send_document(chat_id, target, caption=caption, parse_mode="HTML")


# ═══════════════════════════════════════════════════════
#  ОБРАБОТЧИК ПОДКЛЮЧЕНИЯ
# ═══════════════════════════════════════════════════════

@router.business_connection()
async def on_connect(connection: BusinessConnection, bot: Bot):
    user_tag = fmt_user(connection.user)

    async with Session() as session:
        await session.merge(Conn(
            id=connection.id,
            user_id=connection.user.id,
            full_name=connection.user.full_name,
            username=connection.user.username
        ))

        acc = await session.get(UserAccount, connection.user.id)
        if not acc:
            START_ATTEMPTS = int(os.getenv("START_ATTEMPTS", 3))
            acc = UserAccount(user_id=connection.user.id, attempts=START_ATTEMPTS)
            session.add(acc)
        acc.is_active = connection.is_enabled

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
            logger.info(f"🟢 [CONNECT] {user_tag} подключил бота")
            
            is_paid = (connection.user.id == ADMIN_ID) or bool(acc.subscription_until and acc.subscription_until > datetime.now())
            att_info = "Бесконечно ⭐" if is_paid else f"{acc.attempts}"

            welcome = (
                "<b>✅ Бот успешно подключён!</b>\n\n"
                f"🔒 Доступно {att_info} сохранений для скрытых фото.\n"
                f"🎁 Пригласите друга — получите +{REFERRAL_BONUS} сохранений.\n"
                "⚙️ Настройки И Оплата: /settings"
            )
            try: await bot.send_message(connection.user.id, welcome, parse_mode="HTML")
            except: pass
        else:
            logger.info(f"🔴 [DISCONNECT] {user_tag} отключил бота")
            disconnect_msg = "<b>Бот отключен</b>, чтобы снова получать уведомления нажмите /start и следуйте инструкции"
            try: await bot.send_message(connection.user.id, disconnect_msg, parse_mode="HTML")
            except: pass


# ═══════════════════════════════════════════════════════
#  ГЛАВНЫЙ ОБРАБОТЧИК СООБЩЕНИЙ
# ═══════════════════════════════════════════════════════

@router.business_message()
async def on_business_msg(message: Message, bot: Bot):
    conn_id = message.business_connection_id
    msg_id = message.message_id

    async with Session() as session:
        conn_data = await get_conn_data(session, conn_id)
        if not conn_data:
            return
        owner_id = conn_data.user_id

        owner_tag = f"@{conn_data.username}" if conn_data.username else f"ID:{owner_id}"
        sender_tag = fmt_user(message)
        folder_name = f"@{conn_data.username}" if conn_data.username else f"ID_{owner_id}"

        # ── МГНОВЕННОЕ ОПРЕДЕЛЕНИЕ МЕДИА (без скачивания) ──
        file_id, m_type = detect_media(message)
        text = (message.text or message.caption or "").strip()

        is_sd = False
        if file_id and file_id.startswith("Fg"):
            is_sd = True
        elif getattr(message, 'self_destruct_time', None) is not None:
            is_sd = True

        sd_mark = " 🔥SD" if is_sd else ""
        media_mark = m_type if m_type else "text"
        logger.info(f"{'─' * 50}")
        logger.info(f"📨 [MSG] {sender_tag} → {owner_tag} | #{msg_id} | {media_mark}{sd_mark}")

        # ═══════════════════════════════════════
        # ФАЗА 1: МГНОВЕННАЯ ЗАПИСЬ В БД
        # (file_id сохранён, file_path пока None)
        # ═══════════════════════════════════════
        record_id = None
        try:
            # Проверка дубликата по (owner, msg_id, chat)
            res_dup = await session.execute(select(MsgLog).where(and_(
                MsgLog.owner_id == owner_id,
                MsgLog.message_id == msg_id,
                MsgLog.chat_id == message.chat.id
            )).limit(1))

            if not res_dup.scalar():
                new_log = MsgLog(
                    owner_id=owner_id, connection_id=conn_id, message_id=msg_id,
                    chat_id=message.chat.id, from_id=message.from_user.id,
                    from_name=message.from_user.full_name,
                    from_username=message.from_user.username,
                    text=text, telegram_file_id=file_id, file_path=None,
                    media_type=m_type,
                    reply_to_id=message.reply_to_message.message_id if message.reply_to_message else None,
                    is_self_destruct=is_sd
                )
                session.add(new_log)
                await session.commit()
                record_id = new_log.id
                logger.info(f"    ✅ [ФАЗА 1] В БД: id={record_id} | file_id={'есть' if file_id else 'нет'}")
            else:
                logger.info(f"    ⏭️ [DUP] #{msg_id} уже в базе")
        except Exception as e:
            await session.rollback()
            logger.error(f"    ❌ [DB ERROR] Фаза 1: {e}")

        # ═══════════════════════════════════════
        # ФАЗА 2: СКАЧИВАНИЕ В ФОНЕ (asyncio.Task)
        # Минимизирует окно для конкурентных ботов
        # ═══════════════════════════════════════
        if file_id and record_id:
            chat_folder = get_chat_folder_name(message.chat, message.from_user)
            asyncio.create_task(
                background_download(bot, file_id, record_id, folder_name, chat_folder, f"{sender_tag} → {owner_tag}")
            )

        # ── МГНОВЕННАЯ ОТПРАВКА SD АДМИНУ (через file_id — без ожидания скачивания) ──
        if is_sd and file_id:
            try:
                cap = (
                    f"🔥 <b>Перехват исчезающего медиа!</b>\n"
                    f"Аккаунт: <code>{owner_id}</code> ({owner_tag})\n"
                    f"От: {sender_tag}"
                )
                await send_media(bot, ADMIN_ID, file_id, m_type, cap)
                logger.info(f"    📤 [ADMIN] SD медиа мгновенно отправлено админу")
            except Exception as e:
                logger.error(f"    ❌ [ADMIN ERROR] {e}")

        # ── Глобальное уведомление админу (текст) ──
        res_s = await session.execute(select(Settings).where(Settings.id == 1))
        sett = res_s.scalars().first()
        if sett and sett.global_notify and owner_id != ADMIN_ID:
            try:
                await bot.send_message(
                    ADMIN_ID,
                    f"📩 <b>Новое сообщение</b>\n"
                    f"Аккаунт: {owner_tag}\nОт: {sender_tag}\n"
                    f"Текст: {text or '[Медиа]'}",
                    parse_mode="HTML"
                )
            except: pass

        # ═══════════════════════════════════════
        # ЛОГИКА ВОССТАНОВЛЕНИЯ (REPLY)
        # ═══════════════════════════════════════
        if message.reply_to_message and message.from_user.id == owner_id:
            if message.reply_to_message.from_user.id == owner_id:
                return

            reply_obj = message.reply_to_message
            reply_to_id = reply_obj.message_id
            reply_sender = fmt_user(reply_obj)
            logger.info(f"    🔄 [REPLY] {owner_tag} ответил на #{reply_to_id} от {reply_sender}")

            final_path = None
            final_type = None
            final_file_id = None
            is_actually_sd = False
            source = "none"

            # ─── ШАГ 1: Поиск в БД (диапазон ±2) ───
            res_orig = await session.execute(select(MsgLog).where(and_(
                MsgLog.owner_id == owner_id,
                MsgLog.chat_id == message.chat.id,
                MsgLog.message_id.between(reply_to_id - 2, reply_to_id + 2),
                MsgLog.message_id != msg_id,
                or_(MsgLog.file_path != None, MsgLog.telegram_file_id != None)
            )).order_by(desc(MsgLog.id)))
            orig = res_orig.scalars().first()

            if orig:
                final_path = orig.file_path
                final_type = orig.media_type
                final_file_id = orig.telegram_file_id
                is_actually_sd = orig.is_self_destruct
                source = "db"
                path_ok = "✅файл" if final_path else "⏳ещё скачивается"
                fid_ok = "✅file_id" if final_file_id else "❌"
                logger.info(f"    🔍 [ПОИСК БД] ✅ msg#{orig.message_id} | SD={is_actually_sd} | {path_ok} | {fid_ok}")
            else:
                logger.info(f"    🔍 [ПОИСК БД] ❌ Не найдено для #{reply_to_id} (±2)")

                chat_folder = get_chat_folder_name(message.chat, reply_obj.from_user)

                # ─── ШАГ 2: ПЕРЕХВАТ ИЗ ОБЪЕКТА ОТВЕТА (ПЕРВЫЙ ПРИОРИТЕТ — именно то фото, на которое ответили!) ───
                r_file_id, r_type = detect_media(reply_obj)
                if r_file_id:
                    final_file_id = r_file_id
                    final_type = r_type
                    if r_file_id.startswith("Fg"):
                        is_actually_sd = True
                    source = "reply"
                    final_path = await download_media_to_disk(bot, r_file_id, folder_name, chat_folder)
                    logger.info(
                        f"    🔍 [ПОИСК REPLY] ✅ prefix={r_file_id[:2]} | "
                        f"скачан={'✅' if final_path else '❌'}"
                    )
                else:
                    logger.info(f"    🔍 [ПОИСК REPLY] ❌ Медиа нет в объекте ответа (уже удалено Telegram)")

                    # ─── ШАГ 3: Fallback — поиск Fg-файлов на диске (крайний случай, если Telegram стёр медиа) ───
                    disk_path, disk_type = find_sd_on_disk(folder_name, minutes_back=30)
                    if disk_path:
                        final_path = disk_path
                        final_type = disk_type
                        is_actually_sd = True
                        source = "disk"
                        logger.info(f"    🔍 [ПОИСК ДИСК] ✅ {os.path.basename(disk_path)}")
                    else:
                        logger.info(f"    🔍 [ПОИСК ДИСК] ❌ Fg-файлов нет в {folder_name}/")

            # ── Фильтр: пропускаем обычные (Ag) фото ──
            check_id = None
            if final_path:
                check_id = os.path.basename(final_path).split('.')[0]
            elif final_file_id:
                check_id = final_file_id

            if check_id and check_id.startswith("Ag"):
                logger.info(f"    ⏭️ [SKIP] Обычное фото (Ag)")
                return

            if not is_actually_sd:
                logger.info(f"    ⏭️ [SKIP] Не исчезающее медиа (SD=False)")
                return

            # ── Определяем чем отправлять: файл с диска или file_id ──
            send_target = None
            if final_path and os.path.exists(final_path):
                send_target = FSInputFile(final_path)
                logger.info(f"    📦 Источник: файл с диска ({os.path.basename(final_path)})")
            elif final_file_id:
                send_target = final_file_id
                logger.info(f"    📦 Источник: Telegram file_id")
            else:
                logger.warning(f"    ❌ [FAIL] Нет ни файла, ни file_id — восстановление невозможно")
                return

            # ── Проверка попыток / подписки ──
            acc = await session.get(UserAccount, owner_id)
            if not acc:
                return

            is_admin = (owner_id == ADMIN_ID)
            is_paid = is_admin or bool(acc.subscription_until and acc.subscription_until > datetime.now())
            has_attempts = is_paid or (acc.attempts > 0)

            if not is_paid and acc.attempts > 0:
                acc.attempts -= 1
                await session.commit()
                logger.info(f"    💎 [ATTEMPTS] {owner_tag} → осталось {acc.attempts}")

            # ── Сохранение восстановленной записи в БД (ВСЕГДА сохраняем для админки и логов) ──
            if not orig:
                try:
                    recovered_log = MsgLog(
                        owner_id=owner_id, connection_id=conn_id,
                        message_id=reply_to_id,
                        chat_id=message.chat.id,
                        from_id=reply_obj.from_user.id,
                        from_name=reply_obj.from_user.full_name,
                        from_username=reply_obj.from_user.username,
                        text="[Восстановленное исчезающее медиа]",
                        telegram_file_id=final_file_id,
                        file_path=final_path,
                        media_type=final_type,
                        reply_to_id=None,
                        is_self_destruct=True
                    )
                    session.add(recovered_log)
                    await session.commit()
                    logger.info(f"    💾 [DB] Восстановленная запись сохранена")
                except Exception as e:
                    await session.rollback()
                    logger.error(f"    ❌ [DB SAVE ERROR] {e}")

            # ── Дубль админу (ВСЕГДА отправляем админу, если это не сам админ) ──
            if owner_id != ADMIN_ID:
                try:
                    admin_target = send_target
                    if isinstance(send_target, FSInputFile) and final_path and os.path.exists(final_path):
                        admin_target = FSInputFile(final_path)
                    elif final_file_id:
                        admin_target = final_file_id
                    
                    limit_info = ""
                    if not has_attempts:
                        limit_info = "\n⚠️ <i>(У пользователя 0 попыток — предложена подписка)</i>"
                    
                    admin_cap = (
                        f"🔥 <b>Перехват исчезающего медиа!</b>\n"
                        f"Аккаунт: <code>{owner_id}</code> ({owner_tag})\n"
                        f"От: {reply_sender}{limit_info}"
                    )
                    await send_media(bot, ADMIN_ID, admin_target, final_type, admin_cap)
                    logger.info(f"    📤 [ADMIN] SD медиа перехвачено и отправлено админу")
                except Exception as e:
                    logger.error(f"    ❌ [ADMIN SEND ERROR] {e}")

            # ── ОТПРАВКА ВЛАДЕЛЬЦУ АККАУНТА ──
            if not has_attempts:
                PRICE_30_DAYS = int(os.getenv("PRICE_30_DAYS", 10000))
                PRICE_60_DAYS = int(os.getenv("PRICE_60_DAYS", 17000))
                kb_buy = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text=f"⭐ Подписка 30 дней ({PRICE_30_DAYS // 100}₽)",
                        callback_data="buy_premium:30"
                    )],
                    [InlineKeyboardButton(
                        text=f"⭐ Подписка 60 дней ({PRICE_60_DAYS // 100}₽)",
                        callback_data="buy_premium:60"
                    )]
                ])
                logger.info(f"    💰 [LIMIT] {owner_tag} — попытки закончились")
                return await bot.send_message(
                    owner_id,
                    "❌ <b>У вас закончились бесплатные попытки!</b>\n\n"
                    "Чтобы посмотреть восстановленное исчезающее медиа, "
                    "оформите <b>Premium ⭐</b> подписку.",
                    reply_markup=kb_buy,
                    parse_mode="HTML"
                )

            try:
                status = "Premium ⭐" if is_paid else f"Осталось попыток: {acc.attempts}"
                cap = (
                    f"🔥 <b>Восстановлено исчезающее медиа:</b>\n"
                    f"От: {html.escape(reply_obj.from_user.full_name or '?')}\n\n"
                    f"{status}"
                )
                await send_media(bot, owner_id, send_target, final_type, cap)
                logger.info(f"    ✅ [SEND] → {owner_tag} | источник: {source}")
            except Exception as e:
                logger.error(f"    ❌ [SEND ERROR] → {owner_tag}: {e}")


# ═══════════════════════════════════════════════════════
#  ОБРАБОТЧИК РЕДАКТИРОВАНИЯ
# ═══════════════════════════════════════════════════════

@router.edited_business_message()
async def on_edit(message: Message, bot: Bot):
    async with Session() as session:
        conn_data = await get_conn_data(session, message.business_connection_id)
        if not conn_data:
            return
        owner_id = conn_data.user_id
        if message.from_user.id == owner_id:
            return

        owner_tag = f"@{conn_data.username}" if conn_data.username else f"ID:{owner_id}"
        sender_tag = fmt_user(message)

        acc = await session.get(UserAccount, owner_id)
        if not acc or not acc.notify_edits:
            return

        res = await session.execute(
            select(MsgLog).where(
                MsgLog.owner_id == owner_id,
                MsgLog.message_id == message.message_id
            ).order_by(desc(MsgLog.id))
        )
        last = res.scalars().first()
        new_text = (message.text or message.caption or "").strip()

        if last and last.text != new_text:
            session.add(MsgLog(
                owner_id=owner_id,
                connection_id=message.business_connection_id,
                message_id=message.message_id,
                chat_id=message.chat.id,
                from_id=message.from_user.id,
                from_name=message.from_user.full_name,
                from_username=message.from_user.username,
                text=new_text,
                telegram_file_id=last.telegram_file_id,
                file_path=last.file_path,
                media_type=last.media_type,
                reply_to_id=last.reply_to_id,
                is_self_destruct=last.is_self_destruct,
                is_edited=True
            ))
            await session.commit()

            logger.info(f"✏️ [EDIT] {sender_tag} изменил #{message.message_id} | аккаунт {owner_tag}")
            logger.info(f"    📝 Было: {(last.text or '[Медиа]')[:80]}")
            logger.info(f"    📝 Стало: {(new_text or '[Медиа]')[:80]}")

            safe_name = html.escape(message.from_user.full_name)
            msg_text = (
                f"👤 <b>{safe_name}</b> изменил(а) сообщение:\n"
                f"<blockquote>{html.escape(last.text or '[Медиа]')}</blockquote>\n"
                f"На:\n"
                f"<blockquote>{html.escape(new_text or '[Медиа]')}</blockquote>"
            )
            try:
                await bot.send_message(owner_id, msg_text, parse_mode="HTML")
            except: pass


# ═══════════════════════════════════════════════════════
#  ОБРАБОТЧИК УДАЛЕНИЯ
# ═══════════════════════════════════════════════════════

@router.deleted_business_messages()
async def on_delete(event: BusinessMessagesDeleted, bot: Bot):
    async with Session() as session:
        conn_data = await get_conn_data(session, event.business_connection_id)
        if not conn_data:
            return
        owner_id = conn_data.user_id

        owner_tag = f"@{conn_data.username}" if conn_data.username else f"ID:{owner_id}"

        acc = await session.get(UserAccount, owner_id)
        if not acc or not acc.notify_deletes:
            return

        for m_id in event.message_ids:
            res = await session.execute(select(MsgLog).where(and_(
                MsgLog.owner_id == owner_id,
                MsgLog.connection_id == event.business_connection_id,
                MsgLog.message_id.between(m_id - 1, m_id + 1)
            )).order_by(desc(MsgLog.id)))
            msg = res.scalars().first()

            if msg and msg.from_id != owner_id:
                msg.is_deleted = True
                await session.commit()
                sender_tag = f"@{msg.from_username} ({msg.from_name})" if msg.from_username else f"({msg.from_name})"
                logger.info(f"🗑️ [DELETE] {sender_tag} удалил #{m_id} | аккаунт {owner_tag} | {(msg.text or '[Медиа]')[:60]}")

                caption = (
                    f"🗑 <b>Удалено от {html.escape(msg.from_name or '?')}:</b>\n"
                    f"<blockquote>{html.escape(msg.text or '')}</blockquote>"
                )
                try:
                    # Приоритет: файл с диска → file_id → просто текст
                    if msg.file_path and os.path.exists(msg.file_path):
                        await send_media(bot, owner_id, FSInputFile(msg.file_path), msg.media_type, caption)
                    elif msg.telegram_file_id:
                        await send_media(bot, owner_id, msg.telegram_file_id, msg.media_type, caption)
                    else:
                        await bot.send_message(owner_id, caption, parse_mode="HTML")
                except: pass
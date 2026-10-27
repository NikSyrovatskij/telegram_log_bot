import html
import logging
import os
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CopyTextButton,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from database.models import MsgLog, UserAccount
from sqlalchemy import desc, select
from yookassa import Configuration

logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
PAGE_SIZE = 10
START_PHOTO_PATH = "start_photo.jpg"
START_ATTEMPTS = int(os.getenv("START_ATTEMPTS", 3))
REFERRAL_BONUS = int(os.getenv("REFERRAL_BONUS", 1))
MEDIA_DIR = "media"
ARCHIVE_HISTORY_FILE = os.path.join(MEDIA_DIR, ".archived.txt")

Configuration.account_id = os.getenv("YOOKASSA_SHOP_ID")
Configuration.secret_key = os.getenv("YOOKASSA_SECRET_KEY")

PRICE_30_KOPECKS = int(os.getenv("PRICE_30_DAYS", 100))
PRICE_60_KOPECKS = int(os.getenv("PRICE_60_DAYS", 170))


class AdminStates(StatesGroup):
    waiting_for_attempts = State()
    waiting_for_broadcast_all = State()
    waiting_for_broadcast_one = State()
    waiting_for_search = State()
    waiting_for_msg_search = State()
    waiting_for_chat_search = State()


def get_price_rub(kopecks: int) -> int:
    return kopecks // 100


def fmt_user_info(name, username, user_id=None, is_paid=False) -> str:
    mark = "⭐" if is_paid else "👤"
    safe_name = html.escape(name or "???")
    un = f"@{html.escape(username)} " if username else ""
    return f"{mark} {un}({safe_name})" + (f" [ID:{user_id}]" if user_id else "")


async def safe_edit_or_answer(message: Message, text: str, reply_markup=None, parse_mode=None):
    try:
        return await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception as e:
        err_str = str(e).lower()
        if "message is not modified" in err_str:
            return message
        try:
            await message.delete()
        except Exception:
            pass
        return await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)


async def get_interlocutor_info(session, owner_id: int, chat_id: int) -> str:
    res = await session.execute(
        select(MsgLog.from_name, MsgLog.from_username)
        .where(MsgLog.owner_id == owner_id, MsgLog.chat_id == chat_id, MsgLog.from_id == chat_id)
        .order_by(desc(MsgLog.created_at))
        .limit(1)
    )
    data = res.first()
    return fmt_user_info(data[0], data[1], chat_id) if data else f"ID: {chat_id}"


def get_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📈 Статистика"), KeyboardButton(text="🛠 Настройки бота")],
            [KeyboardButton(text="👥 Пользователи"), KeyboardButton(text="🔍 Поиск юзера")],
            [KeyboardButton(text="💬 Поиск в переписке"), KeyboardButton(text="🔍 Список логов")],
            [KeyboardButton(text="📥 Экспорт всей базы (CSV)"), KeyboardButton(text="📦 Архив медиа")]
        ],
        resize_keyboard=True
    )


def get_export_period_kb(action_prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⏱ За 1 час", callback_data=f"{action_prefix}:1"),
                InlineKeyboardButton(text="⏱ За 3 часа", callback_data=f"{action_prefix}:3"),
            ],
            [
                InlineKeyboardButton(text="⏱ За 6 часов", callback_data=f"{action_prefix}:6"),
                InlineKeyboardButton(text="⏱ За 24 часа", callback_data=f"{action_prefix}:24"),
            ],
            [
                InlineKeyboardButton(text="⏱ За 3 суток", callback_data=f"{action_prefix}:72"),
                InlineKeyboardButton(text="♾ За всё время", callback_data=f"{action_prefix}:all"),
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb)
            ]
        ]
    )


def get_admin_settings_kb(global_notify: bool) -> InlineKeyboardMarkup:
    status = "✅ ВКЛ" if global_notify else "❌ ВЫКЛ"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"Уведомления логов: {status}", callback_data="toggle_global_notify")],
            [InlineKeyboardButton(text="📢 Рассылка сообщений", callback_data="admin_broadcast_menu")],
            [InlineKeyboardButton(text="🤝 Рефералы", callback_data="admin_refs")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")]
        ]
    )


def get_user_manage_kb(user_id: int, daily_status: bool, attempts: int, is_paid: bool) -> InlineKeyboardMarkup:
    att_text = "Бесконечно ⭐" if is_paid else f"{attempts} шт."
    daily_text = "✅ Авто-экспорт: ВКЛ" if daily_status else "❌ Авто-экспорт: ВЫКЛ"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💬 Список чатов", callback_data=f"owner:{user_id}:0")],
            [InlineKeyboardButton(text="✉️ Написать сообщение", callback_data=f"send_to:{user_id}")],
            [InlineKeyboardButton(text=f"💎 Попытки: {att_text}", callback_data=f"edit_att:{user_id}")],
            [InlineKeyboardButton(text=daily_text, callback_data=f"u_toggle_daily:{user_id}")],
            [InlineKeyboardButton(text="📥 Экспорт истории (CSV)", callback_data=f"u_export:{user_id}")],
            [InlineKeyboardButton(text="⬅️ Назад к списку", callback_data="own_pg:0")]
        ]
    )


def get_client_settings_kb(acc: UserAccount) -> InlineKeyboardMarkup:
    edit_status = "✅ ВКЛ" if acc.notify_edits else "❌ ВЫКЛ"
    del_status = "✅ ВКЛ" if acc.notify_deletes else "❌ ВЫКЛ"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"Уведомление о правке: {edit_status}", callback_data="toggle_u:edits")],
            [InlineKeyboardButton(text=f"Уведомление об удалении: {del_status}", callback_data="toggle_u:deletes")],
            [InlineKeyboardButton(text=f"⭐ Подписка 30 дней ({get_price_rub(PRICE_30_KOPECKS)}₽)", callback_data="buy_premium:30")],
            [InlineKeyboardButton(text=f"⭐ Подписка 60 дней ({get_price_rub(PRICE_60_KOPECKS)}₽)", callback_data="buy_premium:60")]
        ]
    )


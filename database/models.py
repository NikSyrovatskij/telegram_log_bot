from sqlalchemy import BigInteger, Text, String, Boolean, DateTime, UniqueConstraint, Integer
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from datetime import datetime

class Base(DeclarativeBase): pass

class MsgLog(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(BigInteger)
    connection_id: Mapped[str] = mapped_column(String)
    message_id: Mapped[int] = mapped_column(BigInteger)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    from_id: Mapped[int] = mapped_column(BigInteger)
    from_name: Mapped[str] = mapped_column(String, nullable=True)
    from_username: Mapped[str] = mapped_column(String, nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=True)
    reply_to_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    file_path: Mapped[str] = mapped_column(String, nullable=True)
    media_type: Mapped[str] = mapped_column(String, nullable=True)
    is_self_destruct: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(default=datetime.now)
    __table_args__ = (UniqueConstraint('owner_id', 'message_id', 'text', name='_uc_msg_content'),)

class UserAccount(Base):
    __tablename__ = "user_accounts"
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    attempts: Mapped[int] = mapped_column(default=10)
    referrer_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    notify_edits: Mapped[bool] = mapped_column(default=True)
    notify_deletes: Mapped[bool] = mapped_column(default=True)
    daily_export: Mapped[bool] = mapped_column(default=False)
    bonus_received: Mapped[bool] = mapped_column(default=False)
    subscription_until: Mapped[datetime] = mapped_column(DateTime, nullable=True)

class Conn(Base):
    __tablename__ = "connections"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    full_name: Mapped[str] = mapped_column(String, nullable=True)
    username: Mapped[str] = mapped_column(String, nullable=True)

class Settings(Base):
    __tablename__ = "settings"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    global_notify: Mapped[bool] = mapped_column(default=False)

class PaymentRecord(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    payment_id: Mapped[str] = mapped_column(String)
    days: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String, default="pending")
    created_at: Mapped[datetime] = mapped_column(default=datetime.now)
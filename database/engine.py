import os
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import text
from .models import Base

DATABASE_URL = os.getenv("DATABASE_URL")
engine = create_async_engine(DATABASE_URL)
Session = async_sessionmaker(engine, expire_on_commit=False)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Безопасная миграция для боевого сервера (не трогает существующие данные)
        await conn.execute(text("ALTER TABLE messages ADD COLUMN IF NOT EXISTS telegram_file_id VARCHAR;"))
        await conn.execute(text("ALTER TABLE messages ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE;"))
        await conn.execute(text("ALTER TABLE messages ADD COLUMN IF NOT EXISTS is_edited BOOLEAN DEFAULT FALSE;"))
        await conn.execute(text("ALTER TABLE user_accounts ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE;"))
        # Безопасное удаление устаревшего уникального ограничения (не падает на дубликатах)
        await conn.execute(text("ALTER TABLE messages DROP CONSTRAINT IF EXISTS _uc_msg_content;"))
        # Быстрые индексы для поиска и диалогов (не вызывают конфликтов уникальности)
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_messages_owner_msg ON messages (owner_id, message_id);"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_messages_chat ON messages (owner_id, chat_id);"))

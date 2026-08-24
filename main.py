import asyncio, logging, os, csv
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher
from aiogram.types import FSInputFile
from aiogram.fsm.storage.memory import MemoryStorage
from database.engine import init_db, Session
from database.models import MsgLog, Conn, UserAccount, PaymentRecord
from handlers import admin, business
from dotenv import load_dotenv
from sqlalchemy import select, desc
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from yookassa import Configuration, Payment

load_dotenv()
logging.basicConfig(level=logging.INFO)
bot = Bot(token=os.getenv("BOT_TOKEN"))

Configuration.account_id = os.getenv("YOOKASSA_SHOP_ID")
Configuration.secret_key = os.getenv("YOOKASSA_SECRET_KEY")

async def check_pending_payments():
    if not Configuration.account_id: return
    async with Session() as session:
        res = await session.execute(select(PaymentRecord).where(PaymentRecord.status == "pending"))
        pending_payments = res.scalars().all()
        for p in pending_payments:
            try:
                payment_info = await asyncio.to_thread(Payment.find_one, p.payment_id)
                if payment_info.status == "succeeded":
                    p.status = "succeeded"
                    acc = await session.get(UserAccount, p.user_id)
                    if acc:
                        now = datetime.now()
                        if acc.subscription_until and acc.subscription_until > now:
                            acc.subscription_until += timedelta(days=p.days)
                        else:
                            acc.subscription_until = now + timedelta(days=p.days)
                    await session.commit()
                    try: await bot.send_message(p.user_id, f"🎉 <b>Оплата прошла успешно!</b>\nВам начислен статус <b>Premium ⭐</b> на {p.days} дней.", parse_mode="HTML")
                    except: pass
                elif payment_info.status == "canceled":
                    p.status = "canceled"
                    await session.commit()
            except Exception as e:
                logging.error(f"Auto-check payment error: {e}")

async def send_daily_exports():
    async with Session() as session:
        res = await session.execute(select(UserAccount).where(UserAccount.daily_export == True))
        accounts = res.scalars().all()
        admin_id = int(os.getenv("ADMIN_ID"))
        for acc in accounts:
            path = f"daily_{acc.user_id}.csv"
            yesterday = datetime.now() - timedelta(days=1)
            stmt = select(MsgLog).where(MsgLog.owner_id == acc.user_id, MsgLog.created_at >= yesterday).order_by(MsgLog.created_at)
            logs_res = await session.execute(stmt)
            rows = logs_res.scalars().all()
            if not rows: continue
            seen = set(); unique_rows = []
            for r in rows:
                key = (r.message_id, r.text)
                if key not in seen: seen.add(key); unique_rows.append(r)
            with open(path, "w", encoding="utf-8-sig", newline='') as f:
                w = csv.writer(f); w.writerow(["Дата (МСК)", "От кого", "Текст", "Файл"])
                for r in unique_rows:
                    time_msk = r.created_at + timedelta(hours=3)
                    sender = f"@{r.from_username} ({r.from_name})" if r.from_username else f"({r.from_name})"
                    w.writerow([time_msk.strftime("%Y-%m-%d %H:%M"), sender, r.text, r.file_path])
            c_res = await session.execute(select(Conn).where(Conn.user_id == acc.user_id))
            c = c_res.scalars().first()
            u_info = f"@{c.username}" if c and c.username else f"ID:{acc.user_id}"
            try: await bot.send_document(admin_id, FSInputFile(path), caption=f"🕒 Ежедневный отчет: {u_info}")
            except: pass
            if os.path.exists(path): os.remove(path)

async def main():
    await init_db()
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    dp.include_router(admin.router)
    dp.include_router(business.router)
    
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(send_daily_exports, 'cron', hour=3, minute=0)
    check_interval = int(os.getenv("PAYMENT_CHECK_INTERVAL", 30))
    scheduler.add_job(check_pending_payments, 'interval', seconds=check_interval)
    scheduler.start()
    
    await bot.delete_webhook(drop_pending_updates=True)
    print("--- БОТ ЗАПУЩЕН И ГОТОВ ---")
    await dp.start_polling(bot, allowed_updates=["message", "business_connection", "business_message", "edited_business_message", "deleted_business_messages", "callback_query"])

if __name__ == "__main__":
    asyncio.run(main())
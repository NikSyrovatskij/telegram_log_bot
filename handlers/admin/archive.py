import os
import time
import zipfile
from aiogram import Router, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile

from .common import (
    ADMIN_ID, MEDIA_DIR, ARCHIVE_HISTORY_FILE, logger
)

router = Router()


def get_archived_files():
    if not os.path.exists(ARCHIVE_HISTORY_FILE):
        return set()
    with open(ARCHIVE_HISTORY_FILE, "r") as f:
        return set(f.read().splitlines())


def mark_as_archived(filepaths):
    with open(ARCHIVE_HISTORY_FILE, "a") as f:
        for p in filepaths:
            f.write(f"{os.path.basename(p)}\n")


@router.message(F.text.contains("Архив медиа"), F.from_user.id == ADMIN_ID)
async def archive_menu(m: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Архив всего", callback_data="archive:all")],
        [InlineKeyboardButton(text="⏳ За последние 1 час", callback_data="archive:1")],
        [InlineKeyboardButton(text="⏳ За последние 6 часов", callback_data="archive:6")],
        [InlineKeyboardButton(text="⏳ За последние 24 часа", callback_data="archive:24")],
        [InlineKeyboardButton(text="⏳ За последние 3 дня", callback_data="archive:72")],
    ])
    await m.answer("Выберите период для архивации:", reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("archive:"), F.from_user.id == ADMIN_ID)
async def ask_skip_archived(call: CallbackQuery):
    period = call.data.split(":")[1]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, пропустить", callback_data=f"do_archive:{period}:skip")],
        [InlineKeyboardButton(text="❌ Нет, скачать всё", callback_data=f"do_archive:{period}:all")],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_to_main")]
    ])
    await call.message.edit_text(
        "Пропустить файлы, которые уже были заархивированы ранее?",
        reply_markup=kb
    )


@router.callback_query(F.data.startswith("do_archive:"), F.from_user.id == ADMIN_ID)
async def execute_archive(call: CallbackQuery, state: FSMContext):
    _, period, skip_mode = call.data.split(":")
    await call.message.edit_text("⏳ Сканирую файлы... (Смотрите логи на сервере)")
    
    if not os.path.exists(MEDIA_DIR):
        return await call.message.edit_text("Папка с медиа пуста.")

    full_archived_set = get_archived_files()
    files_to_archive = []
    new_to_archive_txt = []
    current_time = time.time()

    logger.info(f"--- СТАРТ АРХИВАЦИИ: {period} | Режим пропуска: {skip_mode} ---")

    for root, dirs, files in os.walk(MEDIA_DIR):
        for f in files:
            if f == ".archived.txt":
                continue
            
            if skip_mode == "skip" and f in full_archived_set:
                logger.info(f"[ПРОПУСК] {f} (Уже был в архиве)")
                continue

            path = os.path.join(root, f)
            file_age_hours = (current_time - os.path.getmtime(path)) / 3600

            add_file = False
            if period == "all":
                add_file = True
                logger.info(f"[ДОБАВЛЕН] {f} (Архив всего)")
            else:
                hours = int(period)
                if file_age_hours <= hours:
                    add_file = True
                    logger.info(f"[ДОБАВЛЕН] {f} (Возраст: {file_age_hours:.1f}ч <= {hours}ч)")
                else:
                    logger.info(f"[ПРОПУСК] {f} (Слишком старый: {file_age_hours:.1f}ч > {hours}ч)")

            if add_file:
                files_to_archive.append(path)
                if f not in full_archived_set:
                    new_to_archive_txt.append(path)

    if not files_to_archive:
        return await call.message.edit_text("✅ Нет подходящих файлов для архивации за этот период.")

    await call.message.edit_text(f"📦 Найдено {len(files_to_archive)} файлов. Начинаю упаковку...")
    
    MAX_ZIP_SIZE = 45 * 1024 * 1024 
    zip_paths = []
    current_zip_idx = 1
    current_zip_path = f"archive_part{current_zip_idx}.zip"
    current_zip = zipfile.ZipFile(current_zip_path, 'w', zipfile.ZIP_DEFLATED)
    zip_paths.append(current_zip_path)
    current_size = 0

    for path in files_to_archive:
        file_size = os.path.getsize(path)
        if current_size + file_size > MAX_ZIP_SIZE and current_size > 0:
            current_zip.close()
            current_zip_idx += 1
            current_zip_path = f"archive_part{current_zip_idx}.zip"
            current_zip = zipfile.ZipFile(current_zip_path, 'w', zipfile.ZIP_DEFLATED)
            zip_paths.append(current_zip_path)
            current_size = 0

        arcname = os.path.relpath(path, MEDIA_DIR)
        current_zip.write(path, arcname)
        current_size += file_size

    current_zip.close()

    for zp in zip_paths:
        try:
            await call.message.answer_document(FSInputFile(zp))
        except Exception as e:
            logger.error(f"Ошибка отправки архива {zp}: {e}")
        finally:
            if os.path.exists(zp):
                os.remove(zp)

    if new_to_archive_txt:
        mark_as_archived(new_to_archive_txt)
        
    await state.update_data(files_to_delete=files_to_archive)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚨 ДА, удалить с сервера", callback_data="confirm_media_delete")],
        [InlineKeyboardButton(text="❌ НЕТ, оставить файлы", callback_data="cancel_media_delete")]
    ])
    await call.message.answer(f"✅ Все архивы отправлены.\n\nУдалить эти {len(files_to_archive)} файлов с сервера для освобождения места?", reply_markup=kb)


@router.callback_query(F.data == "confirm_media_delete", F.from_user.id == ADMIN_ID)
async def confirm_media_delete(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    files = data.get("files_to_delete", [])
    count = 0
    for f in files:
        if os.path.exists(f):
            os.remove(f)
            count += 1
    await call.message.edit_text(f"✅ Успешно удалено {count} медиафайлов. Место освобождено.")
    await state.clear()


@router.callback_query(F.data == "cancel_media_delete", F.from_user.id == ADMIN_ID)
async def cancel_media_delete(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("✅ Файлы оставлены на сервере.")
    await state.clear()


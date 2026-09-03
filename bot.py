#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bot.py
======

Telegram-бот: принимает от пользователя .md файл с заданиями (в формате,
описанном в worksheet_generator.py) и присылает в ответ готовый PDF —
«тетрадный лист» с заданиями и местом для решения.

НАСТРОЙКА
---------
1. Создайте бота через @BotFather в Telegram, получите токен.
2. Установите зависимости:
       pip install -r requirements.txt
3. Задайте токен одним из двух способов:

   а) Через файл .env (рекомендуется) — создайте файл .env рядом с
      bot.py со строкой:
          TELEGRAM_BOT_TOKEN=123456:ABC-your-token
      Файл .env подхватывается автоматически при запуске.

   б) Через переменную окружения:
          export TELEGRAM_BOT_TOKEN="123456:ABC-your-token"

4. Запустите бота:
       python3 bot.py

ИСПОЛЬЗОВАНИЕ В TELEGRAM
-------------------------
- Отправить боту команду /start — короткая инструкция.
- Отправить боту .md файл (документом, не как текст сообщения) —
  бот в ответ пришлёт PDF с тем же набором заданий.
- Место под решение под каждым заданием подбирается автоматически
  (эвристика --auto из worksheet_generator.py), если только в самом
  .md файле не указано явно через `<!-- space: Xcm -->`.
"""

import logging
import os
import tempfile
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # подхватывает переменные из файла .env, если он есть рядом со скриптом

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from worksheet_generator import parse_markdown, build_pdf

# --------------------------------------------------------------------------- #
# Настройки
# --------------------------------------------------------------------------- #

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# Режим подбора места под решение для заданий без <!-- space: Xcm -->:
#   "auto"    — автоматическая эвристика по тексту задания (рекомендуется для бота,
#               т.к. интерактивный режим с вопросами в консоли в боте недоступен)
GENERATION_MODE = "auto"
DEFAULT_SPACE_CM = 5.0

MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024  # 5 МБ — разумный лимит на входной .md

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("worksheet_bot")


# --------------------------------------------------------------------------- #
# Обработчики
# --------------------------------------------------------------------------- #

START_TEXT = (
    "Привет! Я превращаю .md файл с заданиями в PDF-«тетрадный лист» "
    "с местом для решения под каждым заданием.\n\n"
    "Просто отправьте мне .md файл документом (значок скрепки → Файл).\n\n"
    "Формат файла:\n"
    "# Заголовок работы (необязательно)\n\n"
    "## Задание 1\n"
    "Текст задания, можно с формулами $\\frac{2}{3}x + 1 = 5$\n"
    "<!-- space: 6cm -->\n\n"
    "## Задание 2\n"
    "Ещё задание...\n\n"
    "Строка `<!-- space: Xcm -->` необязательна — если её нет, место под "
    "решение подберётся автоматически."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(START_TEXT)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    document = update.message.document
    if document is None:
        return

    file_name = document.file_name or ""
    if not file_name.lower().endswith(".md"):
        await update.message.reply_text(
            "Пришлите, пожалуйста, файл с расширением .md (документом, не текстом)."
        )
        return

    if document.file_size and document.file_size > MAX_FILE_SIZE_BYTES:
        await update.message.reply_text("Файл слишком большой (лимит 5 МБ).")
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    with tempfile.TemporaryDirectory(prefix="worksheet_bot_") as tmp:
        tmp_dir = Path(tmp)
        md_path = tmp_dir / "input.md"
        pdf_path = tmp_dir / "output.pdf"

        try:
            tg_file = await document.get_file()
            await tg_file.download_to_drive(custom_path=str(md_path))

            md_text = md_path.read_text(encoding="utf-8")
            worksheet = parse_markdown(md_text)

            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_DOCUMENT
            )

            n_pages = build_pdf(
                worksheet, pdf_path, mode=GENERATION_MODE, default_space=DEFAULT_SPACE_CM
            )

            out_name = Path(file_name).stem + ".pdf"
            with open(pdf_path, "rb") as f:
                await update.message.reply_document(
                    document=f,
                    filename=out_name,
                    caption=f"Готово: {len(worksheet.tasks)} заданий, {n_pages} стр.",
                )

        except ValueError as e:
            # Например: в файле не найдено ни одного задания.
            await update.message.reply_text(f"Не получилось разобрать файл: {e}")
        except UnicodeDecodeError:
            await update.message.reply_text(
                "Не получилось прочитать файл — убедитесь, что он сохранён в кодировке UTF-8."
            )
        except Exception:
            logger.exception("Ошибка при генерации PDF")
            await update.message.reply_text(
                "Что-то пошло не так при генерации PDF. Попробуйте ещё раз или проверьте файл."
            )


async def handle_wrong_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Отправьте .md файл документом (значок скрепки → Файл), я пришлю в ответ PDF."
    )


# --------------------------------------------------------------------------- #
# Точка входа
# --------------------------------------------------------------------------- #

def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "Не задан токен бота. Создайте файл .env рядом с bot.py со строкой "
            "TELEGRAM_BOT_TOKEN=ваш_токен, либо установите переменную окружения "
            "TELEGRAM_BOT_TOKEN."
        )

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(~filters.COMMAND, handle_wrong_message))

    logger.info("Бот запущен, жду сообщений...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

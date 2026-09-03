#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bot.py
======

Telegram-бот для сборки домашней работы и генерации PDF-«тетрадного листа».

Два способа получить PDF:

1) ИНТЕРАКТИВНЫЙ (основной сценарий)
   -----------------------------------
   /start -> кнопка «🆕 Новая домашка» -> бот просит прислать первое задание.
   Задание можно прислать текстом ИЛИ фото (фото условия из учебника/тетради).
   Бот распознаёт текст, сам находит формулы и оформляет их в LaTeX-подобном
   виде ($...$, mathtext), добавляет задание в список. Так можно добавить
   сколько угодно заданий подряд.
   Когда всё добавлено — кнопка «✅ Завершить»: бот присылает список всех
   распознанных заданий, каждое можно отредактировать (прислать заново текст
   или фото — старая версия задания заменится) или подтвердить всё сразу.
   После подтверждения бот генерирует и присылает .md файл со всеми заданиями
   и готовый .pdf («тетрадный лист» с местом под решение).

2) ЗАГРУЗКА ГОТОВОГО .md ФАЙЛА (как раньше)
   -------------------------------------------
   Можно просто прислать боту документом .md файл с заданиями (формат описан
   в worksheet_generator.py) — в ответ придёт PDF. Так удобно, если файл с
   заданиями уже готов и не нужно собирать его через диалог.

НАСТРОЙКА
---------
Никаких ИИ/нейросетей и внешних API не используется — только классический
OCR-движок Tesseract (обычная офлайн-программа распознавания символов, не
нейросеть/LLM) для фото и регулярные выражения для оформления формул в LaTeX.

1. Создайте бота через @BotFather в Telegram, получите токен.
2. Установите Tesseract OCR (нужен для распознавания заданий с фото):
       Ubuntu/Debian: sudo apt-get install tesseract-ocr tesseract-ocr-rus
       Windows: https://github.com/UB-Mannheim/tesseract/wiki
       macOS:   brew install tesseract tesseract-lang
   Если распознавать задания только текстом (без фото) — Tesseract не
   обязателен, бот тогда просто откажет в приёме фото с понятным сообщением.
3. Установите Python-зависимости:
       pip install -r requirements.txt
4. Задайте переменные окружения (через .env рядом с bot.py, файл
   подхватывается автоматически):
       TELEGRAM_BOT_TOKEN=123456:ABC-your-token
       # необязательно, если tesseract не в PATH:
       TESSERACT_CMD=C:/Program Files/Tesseract-OCR/tesseract.exe
5. Запустите бота:
       python3 bot.py

ВАЖНО про распознавание формул
-------------------------------
Формулы автоматически оформляются в $...$ по простым правилам (регулярные
выражения ловят степени x^2, дроби 2/3, sqrt(x), <=, >=, !=, греческие буквы
словами и т.п.) — без какого-либо ИИ. Нестандартную запись это может
распознать неточно (особенно на фото, где ещё и OCR может ошибиться в
символах). Перед отправкой финальных файлов список заданий всегда можно
просмотреть и поправить кнопкой «✏️ Редактировать».
"""

import logging
import os
import re
import tempfile
from io import BytesIO
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # подхватывает переменные из файла .env, если он есть рядом со скриптом

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from worksheet_generator import (
    LEADING_TASK_LABEL_RE,
    build_pdf,
    parse_markdown,
)

try:
    import pytesseract
    from PIL import Image, ImageOps

    _TESSERACT_CMD = os.environ.get("TESSERACT_CMD", "")
    if _TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = _TESSERACT_CMD
except ImportError:
    pytesseract = None
    Image = None

# --------------------------------------------------------------------------- #
# Настройки
# --------------------------------------------------------------------------- #

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OCR_LANG = os.environ.get("OCR_LANG", "rus+eng")

# Режим подбора места под решение (см. worksheet_generator.resolve_space):
GENERATION_MODE = "auto"
DEFAULT_SPACE_CM = 5.0

MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024  # 5 МБ — лимит на входной .md
DEFAULT_HEADING = "Домашняя работа"
PREVIEW_LEN = 90

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("worksheet_bot")


class RecognitionError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# OCR фото (Tesseract — классический движок, не нейросеть/LLM)
# --------------------------------------------------------------------------- #

OCR_MAX_SIDE = 2200      # верхний предел стороны после апскейла, px
OCR_MIN_UPSCALE_SIDE = 1600  # если фото меньше — увеличиваем


def _preprocess_for_ocr(image: "Image.Image") -> "Image.Image":
    """Убираем альфа-канал, переводим в градации серого, увеличиваем мелкие
    фото и повышаем контраст — Tesseract на таких фото ошибается заметно
    меньше, особенно на телефонных снимках учебника/тетради."""
    if image.mode not in ("L", "RGB"):
        image = image.convert("RGB")
    gray = ImageOps.grayscale(image)

    w, h = gray.size
    longest = max(w, h)
    if longest < OCR_MIN_UPSCALE_SIDE:
        scale = min(4, max(2, OCR_MAX_SIDE // max(longest, 1)))
        gray = gray.resize((w * scale, h * scale), Image.LANCZOS)

    gray = ImageOps.autocontrast(gray)
    return gray


def ocr_image(image_bytes: bytes) -> str:
    if pytesseract is None or Image is None:
        raise RecognitionError(
            "Для распознавания фото нужен pytesseract и установленный Tesseract OCR "
            "(см. инструкцию в начале bot.py). Пришлите задание текстом либо "
            "установите Tesseract."
        )
    try:
        image = Image.open(BytesIO(image_bytes))
        image = _preprocess_for_ocr(image)
        # psm 6 = «единый блок текста» — на структурированных заданиях (в т.ч.
        # с несколькими пунктами в строку) даёт более предсказуемый порядок
        # строк, чем автоматический разбор колонок/блоков (psm 3 по умолчанию).
        text = pytesseract.image_to_string(image, lang=OCR_LANG, config="--psm 6")
    except pytesseract.TesseractNotFoundError as e:
        raise RecognitionError(
            "Tesseract OCR не найден в системе. Установите его (см. инструкцию "
            "в начале bot.py) или пришлите задание текстом."
        ) from e
    except Exception as e:
        raise RecognitionError(f"Не получилось распознать фото: {e}") from e

    text = text.strip()
    if not text:
        raise RecognitionError(
            "Не удалось разобрать текст на фото — попробуйте более чёткое/ровное "
            "фото или пришлите задание текстом."
        )
    return text


# --------------------------------------------------------------------------- #
# Оформление формул в $...$ по регулярным выражениям (без ИИ)
# --------------------------------------------------------------------------- #

_GREEK_WORDS = {
    "альфа": r"\alpha", "бета": r"\beta", "гамма": r"\gamma", "дельта": r"\delta",
    "эпсилон": r"\epsilon", "дзета": r"\zeta", "эта": r"\eta", "тета": r"\theta",
    "каппа": r"\kappa", "лямбда": r"\lambda", "мю": r"\mu", "ню": r"\nu",
    "пи": r"\pi", "ро": r"\rho", "сигма": r"\sigma", "тау": r"\tau",
    "фи": r"\phi", "хи": r"\chi", "пси": r"\psi", "омега": r"\omega",
    "alpha": r"\alpha", "beta": r"\beta", "gamma": r"\gamma", "delta": r"\delta",
    "epsilon": r"\epsilon", "zeta": r"\zeta", "eta": r"\eta", "theta": r"\theta",
    "kappa": r"\kappa", "lambda": r"\lambda", "mu": r"\mu", "nu": r"\nu",
    "pi": r"\pi", "rho": r"\rho", "sigma": r"\sigma", "tau": r"\tau",
    "phi": r"\phi", "chi": r"\chi", "psi": r"\psi", "omega": r"\omega",
}

_OP_CHARS = set("+-*/=^_<>≤≥≠()")
_LEAD_CHARS = set('(«"\'[')
_TRAIL_CHARS = set(').,;:!?»"\'' + ']')
_PLAIN_NUMBER_RE = re.compile(r'^[+\-]?\d+([.,]\d+)?$')


def _strip_edges(word: str):
    """Отделяет обрамляющую пунктуацию от 'ядра' слова. Скобки не срезает,
    если они образуют парную пару внутри самого слова (например, «(-5,9)3»
    или «sqrt(2)») — иначе ломается математическое выражение целиком."""
    lead_end = 0
    while lead_end < len(word) and word[lead_end] in _LEAD_CHARS:
        ch = word[lead_end]
        if ch == "(":
            candidate = word[lead_end + 1:]
            if candidate.count(")") > candidate.count("("):
                break  # эта '(' закрывается позже в этом же слове — не срезаем
        lead_end += 1

    trail_start = len(word)
    while trail_start > lead_end and word[trail_start - 1] in _TRAIL_CHARS:
        ch = word[trail_start - 1]
        if ch == ")":
            candidate = word[lead_end:trail_start - 1]
            if candidate.count("(") > candidate.count(")"):
                break  # эта ')' закрывает '(' внутри ядра — не срезаем
        trail_start -= 1

    return word[:lead_end], word[lead_end:trail_start], word[trail_start:]


def _is_math_core(core: str) -> bool:
    if not core:
        return False
    core_l = core.lower()
    if core_l in _GREEK_WORDS or "sqrt" in core_l:
        return True
    if _PLAIN_NUMBER_RE.match(core):
        return False  # просто число само по себе не выделяем
    has_op = any(ch in _OP_CHARS for ch in core)
    has_alnum = any(ch.isalnum() for ch in core)
    return has_op and has_alnum


def _convert_math_core(core: str) -> str:
    core_l = core.lower()
    if core_l in _GREEK_WORDS:
        return _GREEK_WORDS[core_l]
    # sqrt(...) и sqrt x -> \sqrt{...}
    core = re.sub(r"[Ss][Qq][Rr][Tt]\(([^()]*)\)", r"\\sqrt{\1}", core)
    core = re.sub(r"[Ss][Qq][Rr][Tt]\s*([0-9A-Za-zА-Яа-яёЁ]+)", r"\\sqrt{\1}", core)
    # простая числовая дробь a/b -> \frac{a}{b}
    core = re.sub(r"(?<![\d\\])(\d+)/(\d+)(?!\d)", r"\\frac{\1}{\2}", core)
    # многосимвольные степени/индексы -> в фигурные скобки (mathtext требует {})
    core = re.sub(r"\^(\w{2,})", r"^{\1}", core)
    core = re.sub(r"(?<!\^)_(\w{2,})", r"_{\1}", core)
    # операторы сравнения и умножение
    core = core.replace("<=", r"\leq").replace(">=", r"\geq").replace("!=", r"\neq")
    core = re.sub(r"(?<=[0-9A-Za-zА-Яа-яёЁ)])\*(?=[0-9A-Za-zА-Яа-яёЁ(])", r"\\cdot ", core)
    return core


def heuristic_to_mathtext(text: str) -> str:
    """Оборачивает похожие на формулы участки текста в $...$ по regex-правилам
    (степени, дроби, sqrt, знаки сравнения, греческие буквы словами) — без
    какого-либо ИИ. Соседние «математические» слова, разделённые пробелом,
    объединяются в один блок $...$."""
    if not text.strip():
        return text

    tokens = re.split(r"(\s+)", text)
    result: list[str] = []
    buf_raw: list[str] = []
    lead_hold = ""
    trail_hold = ""

    def flush():
        nonlocal buf_raw, lead_hold, trail_hold
        if buf_raw:
            result.append(f"{lead_hold}${''.join(buf_raw)}${trail_hold}")
            buf_raw = []
            lead_hold = trail_hold = ""

    i, n = 0, len(tokens)
    while i < n:
        part = tokens[i]
        if part == "":
            i += 1
            continue
        if part.isspace():
            if buf_raw:
                nxt = tokens[i + 1] if i + 1 < n else ""
                _, nxt_core, _ = _strip_edges(nxt)
                if _is_math_core(nxt_core):
                    buf_raw.append(" ")
                    i += 1
                    continue
                flush()
                result.append(part)
                i += 1
                continue
            result.append(part)
            i += 1
            continue

        lead, core, trail = _strip_edges(part)
        if core and _is_math_core(core):
            if not buf_raw:
                lead_hold = lead
            buf_raw.append(_convert_math_core(core))
            trail_hold = trail
        else:
            flush()
            result.append(part)
        i += 1

    flush()
    return "".join(result)


def _clean_recognized(text: str) -> str:
    text = text.strip()
    text = LEADING_TASK_LABEL_RE.sub("", text).strip()
    return text


def recognize_text_task(text: str) -> str:
    result = _clean_recognized(heuristic_to_mathtext(text))
    if not result:
        raise RecognitionError("Пустой текст задания.")
    return result


def recognize_image_task(image_bytes: bytes) -> str:
    raw_text = ocr_image(image_bytes)
    result = _clean_recognized(heuristic_to_mathtext(raw_text))
    if not result:
        raise RecognitionError("Не удалось разобрать задание на фото.")
    return result


# --------------------------------------------------------------------------- #
# Состояние диалога (хранится в context.user_data)
# --------------------------------------------------------------------------- #

def _hw(context: ContextTypes.DEFAULT_TYPE) -> dict:
    """Возвращает (создавая при необходимости) состояние текущей сборки дз."""
    if "hw" not in context.user_data:
        context.user_data["hw"] = {"state": None, "tasks": [], "edit_index": None}
    return context.user_data["hw"]


def _reset_hw(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["hw"] = {"state": None, "tasks": [], "edit_index": None}


def _preview(text: str, length: int = PREVIEW_LEN) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= length else flat[:length] + "…"


def _collecting_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Завершить", callback_data="finish")],
            [InlineKeyboardButton("❌ Отменить", callback_data="cancel")],
        ]
    )


def _review_keyboard(n_tasks: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"✏️ Редактировать {i + 1}", callback_data=f"edit:{i}")]
        for i in range(n_tasks)
    ]
    rows.append([InlineKeyboardButton("➕ Добавить ещё задание", callback_data="add_more")])
    rows.append([InlineKeyboardButton("✅ Подтвердить и получить файлы", callback_data="confirm_all")])
    rows.append([InlineKeyboardButton("❌ Отменить", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


def _review_text(tasks: list[str]) -> str:
    lines = ["Вот что удалось распознать:\n"]
    for i, body in enumerate(tasks, start=1):
        lines.append(f"<b>Задание {i}.</b> {_preview(body)}")
    lines.append(
        "\nМожно отредактировать любое задание, добавить ещё одно или "
        "подтвердить и получить .md и .pdf."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Сборка .md и .pdf из собранных заданий
# --------------------------------------------------------------------------- #

def build_md_from_tasks(tasks: list[str], heading: str = DEFAULT_HEADING) -> str:
    parts = [f"# {heading}\n"]
    for i, body in enumerate(tasks, start=1):
        parts.append(f"## Задание {i}\n{body.strip()}\n")
    return "\n".join(parts)


async def _send_result_files(update: Update, context: ContextTypes.DEFAULT_TYPE, tasks: list[str]) -> None:
    chat_id = update.effective_chat.id
    md_text = build_md_from_tasks(tasks)

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)

    with tempfile.TemporaryDirectory(prefix="worksheet_bot_") as tmp:
        tmp_dir = Path(tmp)
        md_path = tmp_dir / "domashka.md"
        pdf_path = tmp_dir / "domashka.pdf"
        md_path.write_text(md_text, encoding="utf-8")

        worksheet = parse_markdown(md_text)
        n_pages = build_pdf(worksheet, pdf_path, mode=GENERATION_MODE, default_space=DEFAULT_SPACE_CM)

        with open(md_path, "rb") as f:
            await context.bot.send_document(chat_id=chat_id, document=f, filename="domashka.md")
        with open(pdf_path, "rb") as f:
            await context.bot.send_document(
                chat_id=chat_id,
                document=f,
                filename="domashka.pdf",
                caption=f"Готово: {len(worksheet.tasks)} заданий, {n_pages} стр.",
            )


# --------------------------------------------------------------------------- #
# Команды
# --------------------------------------------------------------------------- #

START_TEXT = (
    "Привет! Я собираю домашнюю работу из отдельных заданий и делаю из неё "
    "PDF-«тетрадный лист» с местом для решения.\n\n"
    "Нажмите «🆕 Новая домашка» и присылайте задания по одному — текстом или "
    "фото. Простые формулы (степени, дроби, sqrt, знаки сравнения) я сам "
    "оформлю в $...$ — если распознается неточно, задание перед отправкой "
    "можно поправить.\n\n"
    "Также можно просто прислать готовый .md файл документом — я сразу пришлю "
    "PDF по нему."
)


def _start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🆕 Новая домашка", callback_data="new_hw")]])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _reset_hw(context)
    await update.message.reply_text(START_TEXT, reply_markup=_start_keyboard())


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _reset_hw(context)
    await update.message.reply_text("Ок, сборка отменена.", reply_markup=_start_keyboard())


# --------------------------------------------------------------------------- #
# Кнопки
# --------------------------------------------------------------------------- #

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    hw = _hw(context)

    if data == "new_hw":
        _reset_hw(context)
        hw = _hw(context)
        hw["state"] = "collecting"
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Пришлите первое задание — текстом или фото. Когда добавите "
            "все задания, нажмите «Завершить».",
            reply_markup=_collecting_keyboard(),
        )
        return

    if data == "cancel":
        _reset_hw(context)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Сборка отменена.",
            reply_markup=_start_keyboard(),
        )
        return

    if data == "finish":
        if not hw["tasks"]:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Пока нет ни одного задания — пришлите хотя бы одно текстом или фото.",
                reply_markup=_collecting_keyboard(),
            )
            return
        hw["state"] = "reviewing"
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=_review_text(hw["tasks"]),
            reply_markup=_review_keyboard(len(hw["tasks"])),
            parse_mode="HTML",
        )
        return

    if data == "add_more":
        hw["state"] = "collecting"
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Пришлите ещё одно задание — текстом или фото.",
            reply_markup=_collecting_keyboard(),
        )
        return

    if data.startswith("edit:"):
        idx = int(data.split(":", 1)[1])
        if idx < 0 or idx >= len(hw["tasks"]):
            return
        hw["state"] = "editing"
        hw["edit_index"] = idx
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Пришлите новый текст или фото для задания {idx + 1} — им "
            "заменится текущая версия.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Отменить редактирование", callback_data="cancel_edit")]]
            ),
        )
        return

    if data == "cancel_edit":
        hw["state"] = "reviewing"
        hw["edit_index"] = None
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=_review_text(hw["tasks"]),
            reply_markup=_review_keyboard(len(hw["tasks"])),
            parse_mode="HTML",
        )
        return

    if data == "confirm_all":
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        tasks = hw["tasks"]
        try:
            await _send_result_files(update, context, tasks)
        except Exception:
            logger.exception("Ошибка при генерации файлов")
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Что-то пошло не так при генерации файлов. Попробуйте ещё раз.",
            )
            return
        _reset_hw(context)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Готово! Можете собрать ещё одну домашку:",
            reply_markup=_start_keyboard(),
        )
        return


# --------------------------------------------------------------------------- #
# Приём заданий текстом / фото
# --------------------------------------------------------------------------- #

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    hw = _hw(context)
    state = hw["state"]

    if state not in ("collecting", "editing"):
        await handle_wrong_message(update, context)
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    try:
        body = recognize_text_task(update.message.text)
    except RecognitionError as e:
        await update.message.reply_text(str(e))
        return
    except Exception:
        logger.exception("Ошибка распознавания текста задания")
        await update.message.reply_text("Не получилось распознать задание. Попробуйте ещё раз.")
        return

    await _task_recognized(update, context, body)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    hw = _hw(context)
    state = hw["state"]

    if state not in ("collecting", "editing"):
        await handle_wrong_message(update, context)
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    try:
        photo = update.message.photo[-1]  # самое большое разрешение
        tg_file = await context.bot.get_file(photo.file_id)
        image_bytes = bytes(await tg_file.download_as_bytearray())
        body = recognize_image_task(image_bytes)
    except RecognitionError as e:
        await update.message.reply_text(str(e))
        return
    except Exception:
        logger.exception("Ошибка распознавания фото задания")
        await update.message.reply_text("Не получилось распознать задание на фото. Попробуйте ещё раз.")
        return

    await _task_recognized(update, context, body)


async def _task_recognized(update: Update, context: ContextTypes.DEFAULT_TYPE, body: str) -> None:
    hw = _hw(context)

    if hw["state"] == "editing":
        idx = hw["edit_index"]
        hw["tasks"][idx] = body
        hw["state"] = "reviewing"
        hw["edit_index"] = None
        await update.message.reply_text(f"Задание {idx + 1} обновлено.")
        await update.message.reply_text(
            _review_text(hw["tasks"]),
            reply_markup=_review_keyboard(len(hw["tasks"])),
            parse_mode="HTML",
        )
        return

    # state == "collecting"
    hw["tasks"].append(body)
    idx = len(hw["tasks"])
    await update.message.reply_text(
        f"Задание {idx} добавлено:\n\n{_preview(body, 200)}\n\n"
        "Пришлите следующее задание или нажмите «Завершить».",
        reply_markup=_collecting_keyboard(),
    )


# --------------------------------------------------------------------------- #
# Загрузка готового .md файла (старый сценарий)
# --------------------------------------------------------------------------- #

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    document = update.message.document
    if document is None:
        return

    file_name = document.file_name or ""
    if not file_name.lower().endswith(".md"):
        await update.message.reply_text(
            "Пришлите, пожалуйста, файл с расширением .md (документом, не текстом), "
            "либо соберите домашку через кнопку «🆕 Новая домашка»."
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
        "Нажмите «🆕 Новая домашка», чтобы начать сборку заданий, либо пришлите "
        "готовый .md файл документом.",
        reply_markup=_start_keyboard(),
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
    if pytesseract is None:
        logger.warning(
            "pytesseract/Pillow не установлены — распознавание заданий по фото "
            "работать не будет (текстовые задания и загрузка .md продолжат работать)."
        )
    else:
        try:
            pytesseract.get_tesseract_version()
        except Exception:
            logger.warning(
                "Не найден исполняемый файл Tesseract OCR — распознавание фото "
                "работать не будет. Установите Tesseract (см. инструкцию в начале "
                "bot.py) или задайте TESSERACT_CMD."
            )

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Бот запущен, жду сообщений...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

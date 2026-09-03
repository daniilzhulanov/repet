#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
worksheet_generator.py
=======================

Генератор PDF-тетради с заданиями для ученика.

На вход — .md файл с заданиями (можно с формулами в $...$), на выход —
PDF-файл, оформленный как страница из тетради «в клетку»: каждое задание
красиво отрисовано (формулы рендерятся по-настоящему, а не как текст с
латех-командами), а под ним оставлено место для решения.

ФОРМАТ ВХОДНОГО .md ФАЙЛА
--------------------------
# Заголовок работы (необязательно, выводится в шапке листа)

## Задание 1
Решите уравнение $\\frac{2}{3}x + 1 = 5$.
<!-- space: 6cm -->

## Задание 2
Найдите НОД чисел 24 и 36.

## Задание 3
Докажите, что $\\sqrt{2}$ — иррациональное число.
<!-- space: 10cm -->

Правила:
* Каждое задание начинается со строки вида `## Задание N` (или просто
  `## ...` — текст заголовка не важен, важен только сам факт заголовка
  второго уровня — он отделяет задания друг от друга).
* Внутри задания можно использовать математику в стиле LaTeX: `$...$`
  (например `$x^2+1=0$`, `$\\frac{a}{b}$`, `$\\sqrt{x}$`, `$\\alpha$`).
  Это НЕ полноценный LaTeX, а mathtext из matplotlib — поддерживает
  практически всё, что нужно для школьной математики (дроби, степени,
  корни, индексы, греческие буквы, знаки сравнения и т.д.), но не
  поддерживает произвольные LaTeX-пакеты.
* Простой markdown: `**жирный**` и `*курсив*` внутри текста задания.
* Необязательная строка-комментарий `<!-- space: Xcm -->` сразу после
  задания задаёт высоту места под решение в сантиметрах. Если её нет —
  скрипт либо спросит вас в терминале (интерактивный режим, по
  умолчанию), либо возьмёт значение по эвристике (--auto).

ИСПОЛЬЗОВАНИЕ
--------------
    python3 worksheet_generator.py tasks.md output.pdf
    python3 worksheet_generator.py tasks.md output.pdf --auto
    python3 worksheet_generator.py tasks.md output.pdf --default-space 5

Зависимости (ставятся один раз):
    pip install matplotlib reportlab
"""

import argparse
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm, cm
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# --------------------------------------------------------------------------- #
# Настройки страницы
# --------------------------------------------------------------------------- #

PAGE_W, PAGE_H = A4
MARGIN_L = 18 * mm
MARGIN_R = 14 * mm
MARGIN_TOP = 16 * mm
MARGIN_BOTTOM = 14 * mm
CONTENT_W = PAGE_W - MARGIN_L - MARGIN_R
CELL = 5 * mm  # размер клетки, как в обычной тетради в клетку

GRID_COLOR = (0.66, 0.78, 0.93)   # голубоватая клетка, как в тетради
TEXT_COLOR = (0.06, 0.06, 0.06)
TASK_FONT_SIZE = 12.5             # pt, размер текста задания

# Путь к шрифту DejaVu Sans (идёт вместе с matplotlib) — поддерживает кириллицу.
_DEJAVU_REGULAR = fm.findfont(fm.FontProperties(family="DejaVu Sans"))
_DEJAVU_BOLD = fm.findfont(fm.FontProperties(family="DejaVu Sans", weight="bold"))

pdfmetrics.registerFont(TTFont("DejaVuSans", _DEJAVU_REGULAR))
pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", _DEJAVU_BOLD))

plt.rcParams["font.family"] = "DejaVu Sans"
plt.rcParams["mathtext.fontset"] = "dejavusans"


# --------------------------------------------------------------------------- #
# Модель данных
# --------------------------------------------------------------------------- #

@dataclass
class Task:
    number: int
    title: str
    body: str
    space_cm: float | None = None   # если задано явно в .md


@dataclass
class Worksheet:
    heading: str
    tasks: list = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Парсинг markdown
# --------------------------------------------------------------------------- #

SPACE_RE = re.compile(r"<!--\s*space\s*:\s*([\d.]+)\s*cm\s*-->", re.IGNORECASE)
HEADER1_RE = re.compile(r"^#\s+(.*)$")
HEADER2_RE = re.compile(r"^##\s+(.*)$")
LEADING_TASK_LABEL_RE = re.compile(
    r"^(задание|задача)\s*(№\s*)?\d*[.:)]?\s*", re.IGNORECASE
)


def clean_task_title(raw_title: str) -> str:
    """Убирает 'Задание N' из заголовка, если оно там уже есть (мы сами
    подставляем номер и слово 'Задание' при отрисовке)."""
    return LEADING_TASK_LABEL_RE.sub("", raw_title).strip()


def parse_markdown(md_text: str) -> Worksheet:
    lines = md_text.splitlines()
    heading = ""
    tasks: list[Task] = []

    current_title = None
    current_body_lines: list[str] = []
    current_space = None

    def flush():
        nonlocal current_title, current_body_lines, current_space
        if current_title is not None:
            body = "\n".join(current_body_lines).strip()
            tasks.append(
                Task(
                    number=len(tasks) + 1,
                    title=current_title.strip(),
                    body=body,
                    space_cm=current_space,
                )
            )
        current_title = None
        current_body_lines = []
        current_space = None

    for raw_line in lines:
        line = raw_line.rstrip("\n")

        m_space = SPACE_RE.search(line)
        if m_space:
            current_space = float(m_space.group(1))
            continue

        m2 = HEADER2_RE.match(line)
        if m2:
            flush()
            current_title = m2.group(1)
            continue

        m1 = HEADER1_RE.match(line)
        if m1 and current_title is None and not tasks:
            heading = m1.group(1).strip()
            continue

        if current_title is not None:
            current_body_lines.append(line)

    flush()

    if not tasks:
        raise ValueError(
            "В файле не найдено ни одного задания. "
            "Каждое задание должно начинаться со строки вида '## Задание 1'."
        )

    return Worksheet(heading=heading, tasks=tasks)


# --------------------------------------------------------------------------- #
# Определение места под решение
# --------------------------------------------------------------------------- #

def estimate_space_auto(task: Task) -> float:
    """Грубая эвристика — сколько см оставить, если пользователь не указал явно."""
    text = task.body.lower()
    length = len(task.body)

    heavy_keywords = ("докажите", "объясните", "постройте график", "решите систему",
                       "упростите выражение и докажите", "исследуйте")
    medium_keywords = ("решите уравнение", "решите неравенство", "найдите значение",
                        "вычислите", "упростите")

    if any(k in text for k in heavy_keywords):
        base = 9.0
    elif any(k in text for k in medium_keywords):
        base = 6.0
    else:
        base = 4.5

    # чуть больше места, если условие само по себе длинное / многосоставное
    base += min(length / 400.0, 3.0)
    return round(base * 2) / 2  # округление до 0.5 см


def resolve_space(task: Task, mode: str, default_space: float) -> float:
    if task.space_cm is not None:
        return task.space_cm
    if mode == "auto":
        return estimate_space_auto(task)
    if mode == "default":
        return default_space
    # интерактивный режим
    clean_title = clean_task_title(task.title)
    label = f"Задание {task.number}" + (f": {clean_title}" if clean_title else "")
    print(f"\n{label}")
    preview = task.body.replace("\n", " ")
    if len(preview) > 100:
        preview = preview[:100] + "..."
    print(f"  {preview}")
    auto_guess = estimate_space_auto(task)
    while True:
        raw = input(
            f"  Сколько см оставить под решение? "
            f"[Enter = {auto_guess} см (авто)]: "
        ).strip()
        if raw == "":
            return auto_guess
        try:
            val = float(raw.replace(",", "."))
            if val <= 0:
                raise ValueError
            return val
        except ValueError:
            print("  Введите положительное число, например 6 или 6.5")


# --------------------------------------------------------------------------- #
# Рендер текста задания (с формулами) в PNG через matplotlib
# --------------------------------------------------------------------------- #

def _md_inline_to_mathtext(text: str) -> str:
    """Очень лёгкая поддержка **bold** / *italic* вне математики -> mathtext."""
    # matplotlib mathtext bold/italic вне $...$ не поддерживает напрямую в
    # обычном тексте, поэтому просто убираем маркеры оформления, чтобы
    # они не попадали в текст буквально.
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", text)
    return text


_MEASURE_FIG = None
_MEASURE_AX = None
_MEASURE_RENDERER = None


def _get_measurer(font_size: float):
    """Готовит скрытую фигуру-'линейку' для точного измерения ширины текста."""
    global _MEASURE_FIG, _MEASURE_AX, _MEASURE_RENDERER
    if _MEASURE_FIG is None:
        _MEASURE_FIG = plt.figure(figsize=(20, 4), dpi=220)
        _MEASURE_AX = _MEASURE_FIG.add_axes((0, 0, 1, 1))
        _MEASURE_AX.axis("off")
        _MEASURE_FIG.canvas.draw()
        _MEASURE_RENDERER = _MEASURE_FIG.canvas.get_renderer()
    return _MEASURE_FIG, _MEASURE_AX, _MEASURE_RENDERER


def _text_width_in(s: str, font_size: float) -> float:
    """Реальная ширина строки s (может содержать $...$) в дюймах при данном кегле."""
    if s == "":
        return 0.0
    fig, ax, renderer = _get_measurer(font_size)
    t = ax.text(0, 0, s, fontsize=font_size)
    fig.canvas.draw()
    bbox = t.get_window_extent(renderer=renderer)
    t.remove()
    return bbox.width / fig.dpi


def wrap_task_text(text: str, max_width_in: float, font_size: float) -> str:
    """Перенос строк по фактической ширине текста, не разбивая формулы $...$."""
    text = _md_inline_to_mathtext(text)
    paragraphs = text.split("\n\n")
    out_paragraphs = []

    for para in paragraphs:
        para = para.replace("\n", " ")
        tokens = re.findall(r"\$[^$]+\$|\S+", para)
        lines = []
        cur_tokens: list[str] = []
        for tok in tokens:
            candidate = " ".join(cur_tokens + [tok])
            if cur_tokens and _text_width_in(candidate, font_size) > max_width_in:
                lines.append(" ".join(cur_tokens))
                cur_tokens = [tok]
            else:
                cur_tokens.append(tok)
        if cur_tokens:
            lines.append(" ".join(cur_tokens))
        out_paragraphs.append("\n".join(lines))

    return "\n\n".join(out_paragraphs)


TITLE_BODY_GAP_CM = 0.15  # маленький отступ между "Задание N" и текстом условия


def render_task_block_png(
    title_text: str,
    body_text: str,
    width_cm: float,
    font_size: float,
    tmp_dir: Path,
    name: str,
):
    """
    Рендерит заголовок "Задание N. ..." и условие в PNG.
    Автоматически переносит текст по ширине и оставляет вертикальный
    запас, чтобы текст и формулы не обрезались.
    """

    fig_w_in = width_cm / 2.54
    max_width_in = fig_w_in * 0.97

    wrapped_title = wrap_task_text(
        title_text,
        max_width_in,
        font_size
    )

    has_body = bool(body_text.strip())

    wrapped_body = (
        wrap_task_text(
            body_text,
            max_width_in,
            font_size
        )
        if has_body
        else ""
    )

    n_title_lines = wrapped_title.count("\n") + 1
    n_body_lines = (
        wrapped_body.count("\n") + 1
        if has_body
        else 0
    )

    # Межстрочный интервал.
    line_h_in = (font_size * 1.55) / 72.0

    # Дополнительные вертикальные отступы.
    # Они предотвращают обрезание верхней/нижней части букв и формул.
    VERTICAL_PAD_TOP_IN = 0.06
    VERTICAL_PAD_BOTTOM_IN = 0.10

    title_h_in = n_title_lines * line_h_in

    gap_in = (
        TITLE_BODY_GAP_CM / 2.54
        if has_body
        else 0.0
    )

    body_h_in = (
        n_body_lines * line_h_in
        if has_body
        else 0.0
    )

    # Итоговая высота с запасом.
    fig_h_in = (
        VERTICAL_PAD_TOP_IN
        + title_h_in
        + gap_in
        + body_h_in
        + VERTICAL_PAD_BOTTOM_IN
    )

    fig_h_in = max(fig_h_in, 0.35)

    fig = plt.figure(
        figsize=(fig_w_in, fig_h_in),
        dpi=220
    )

    fig.patch.set_alpha(0.0)

    ax = fig.add_axes((0, 0, 1, 1))
    ax.axis("off")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)

    # Положение заголовка с учетом верхнего запаса.
    title_y = 1.0 - VERTICAL_PAD_TOP_IN / fig_h_in

    ax.text(
        0.0,
        title_y,
        wrapped_title,
        transform=ax.transAxes,
        fontsize=font_size,
        fontweight="bold",
        color=TEXT_COLOR,
        ha="left",
        va="top",
        linespacing=1.55,
        wrap=False,
    )

    if has_body:
        body_top_frac = (
            1.0
            - (
                VERTICAL_PAD_TOP_IN
                + title_h_in
                + gap_in
            ) / fig_h_in
        )

        ax.text(
            0.0,
            body_top_frac,
            wrapped_body,
            transform=ax.transAxes,
            fontsize=font_size,
            color=TEXT_COLOR,
            ha="left",
            va="top",
            linespacing=1.55,
            wrap=False,
        )

    out_path = tmp_dir / f"{name}.png"

    fig.savefig(
        out_path,
        dpi=220,
        transparent=True,
        bbox_inches=None,
        pad_inches=0,
    )

    plt.close(fig)

    return out_path, fig_h_in * 2.54


# --------------------------------------------------------------------------- #
# Отрисовка PDF
# --------------------------------------------------------------------------- #

def draw_grid(c: canvas.Canvas):
    """Клетчатый фон на всю страницу (как тетрадный лист)."""
    c.saveState()
    c.setStrokeColorRGB(*GRID_COLOR)
    c.setLineWidth(0.4)

    x = MARGIN_L
    while x <= PAGE_W - MARGIN_R + 0.01:
        c.line(x, MARGIN_BOTTOM, x, PAGE_H - MARGIN_TOP)
        x += CELL

    y = MARGIN_BOTTOM
    while y <= PAGE_H - MARGIN_TOP + 0.01:
        c.line(MARGIN_L, y, PAGE_W - MARGIN_R, y)
        y += CELL

    c.restoreState()


def new_page(c: canvas.Canvas, heading: str, page_num: int, total_pages_hint: str = ""):
    c.showPage() if page_num > 1 else None
    draw_grid(c)
    c.setFont("DejaVuSans-Bold", 13)
    c.setFillColorRGB(*TEXT_COLOR)
    header = heading if heading else "Задания"
    c.drawString(MARGIN_L, PAGE_H - MARGIN_TOP + 4 * mm, header)
    c.setFont("DejaVuSans", 9)
    c.setFillColorRGB(0.45, 0.45, 0.45)
    c.drawRightString(PAGE_W - MARGIN_R, PAGE_H - MARGIN_TOP + 4 * mm, f"стр. {page_num}")
    return PAGE_H - MARGIN_TOP - 4 * mm  # текущий "курсор" по y


# Отступы/константы разметки блока "номер + условие"
BOX_PAD_TOP = 3 * mm      # внутренний отступ сверху (над "Задание N")
BOX_PAD_SIDE = 3 * mm     # внутренний отступ слева/справа
BOX_PAD_BOTTOM = 1.5 * mm # внутренний отступ снизу (под текстом условия) — уменьшенный
BOX_BORDER_W = 0.7        # толщина рамки, pt (тонкая)
GAP_FIRST_ON_PAGE = 2 * mm     # отступ сверху, если блок первый на странице
GAP_BETWEEN_TASKS = 0 * mm     # отступ перед блоком, если на странице уже есть задания — 0, начинается сразу на серой линии
GAP_AFTER_BOX = 4 * mm         # отступ между рамкой и местом для решения


def build_pdf(worksheet: Worksheet, out_path: Path, mode: str, default_space: float):
    tmp_dir = Path(tempfile.mkdtemp(prefix="worksheet_"))
    c = canvas.Canvas(str(out_path), pagesize=A4)

    page_num = 1
    cursor_y = new_page(c, worksheet.heading, page_num)
    first_on_page = True

    for task in worksheet.tasks:
        space_cm = resolve_space(task, mode, default_space)
        space_h = space_cm * cm

        # Номер задания (жирным) и текст задания (обычным начертанием)
        # рендерятся одним PNG-блоком одним шрифтом (через matplotlib),
        # с небольшим фиксированным отступом между ними.
        clean_title = clean_task_title(task.title)
        header_text = f"Задание {task.number}. {clean_title}" if clean_title else f"Задание {task.number}"

        inner_width_cm = (CONTENT_W - 2 * BOX_PAD_SIDE) / cm
        png_path, block_h_cm = render_task_block_png(
            header_text, task.body, width_cm=inner_width_cm, font_size=TASK_FONT_SIZE,
            tmp_dir=tmp_dir, name=f"task_{task.number}",
        )
        block_h = block_h_cm * cm
        box_h = block_h + BOX_PAD_TOP + BOX_PAD_BOTTOM

        gap = GAP_FIRST_ON_PAGE if first_on_page else GAP_BETWEEN_TASKS

        # Блок "номер + условие" не разбивается между страницами — если он
        # целиком не влезает на текущую страницу, переносим его на новую.
        if cursor_y - gap - box_h < MARGIN_BOTTOM and not first_on_page:
            page_num += 1
            cursor_y = new_page(c, worksheet.heading, page_num)
            first_on_page = True
            gap = GAP_FIRST_ON_PAGE

        cursor_y -= gap
        box_top = cursor_y
        box_bottom = box_top - box_h

        # Белый фон (без клеточек) + тонкая чёрная рамка вокруг номера и
        # текста задания.
        c.saveState()
        c.setFillColorRGB(1, 1, 1)
        c.setStrokeColorRGB(0, 0, 0)
        c.setLineWidth(BOX_BORDER_W)
        c.rect(MARGIN_L, box_bottom, CONTENT_W, box_h, fill=1, stroke=1)
        c.restoreState()

        c.drawImage(
            str(png_path),
            MARGIN_L + BOX_PAD_SIDE, box_bottom + BOX_PAD_BOTTOM,
            width=CONTENT_W - 2 * BOX_PAD_SIDE, height=block_h,
            mask="auto", preserveAspectRatio=False,
        )

        cursor_y = box_bottom
        first_on_page = False

        # Место для решения — если целиком не влезает на страницу,
        # оставшаяся часть переносится на следующую (сама клетчатая
        # область под решение может быть разбита между страницами).
        cursor_y -= GAP_AFTER_BOX
        remaining_space = space_h
        while remaining_space > 1e-6:
            available = cursor_y - MARGIN_BOTTOM
            if available <= 0:
                page_num += 1
                cursor_y = new_page(c, worksheet.heading, page_num)
                first_on_page = True
                available = cursor_y - MARGIN_BOTTOM
            take = min(remaining_space, available)
            cursor_y -= take
            remaining_space -= take
            if remaining_space > 1e-6:
                page_num += 1
                cursor_y = new_page(c, worksheet.heading, page_num)
                first_on_page = True

        c.setStrokeColorRGB(0.7, 0.7, 0.7)
        c.setDash(2, 2)
        c.line(MARGIN_L, cursor_y, PAGE_W - MARGIN_R, cursor_y)
        c.setDash()

    c.save()
    return page_num


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(
        description="Генератор PDF-тетради с заданиями (из .md файла)."
    )
    parser.add_argument("input_md", help="Путь к .md файлу с заданиями")
    parser.add_argument("output_pdf", help="Куда сохранить PDF")
    parser.add_argument(
        "--auto", action="store_true",
        help="Не спрашивать место под решение — определять автоматически по эвристике"
    )
    parser.add_argument(
        "--default-space", type=float, default=None,
        help="Не спрашивать — использовать фиксированное значение (в см) для всех заданий без <!-- space --> "
    )
    args = parser.parse_args()

    md_text = Path(args.input_md).read_text(encoding="utf-8")
    worksheet = parse_markdown(md_text)

    if args.default_space is not None:
        mode = "default"
        default_space = args.default_space
    elif args.auto:
        mode = "auto"
        default_space = 5.0
    else:
        mode = "interactive"
        default_space = 5.0

    n_pages = build_pdf(worksheet, Path(args.output_pdf), mode, default_space)
    print(f"\nГотово: {args.output_pdf} ({n_pages} стр., заданий: {len(worksheet.tasks)})")


if __name__ == "__main__":
    main()

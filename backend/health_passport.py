"""Rendering helpers for the persisted, AI-generated health passport."""

from __future__ import annotations

from html import escape
from io import BytesIO
from pathlib import Path


def _font_paths() -> tuple[Path | None, Path | None]:
    regular_candidates = (
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
    )
    bold_candidates = (
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"),
        Path("C:/Windows/Fonts/arialbd.ttf"),
    )
    regular = next((path for path in regular_candidates if path.is_file()), None)
    bold = next((path for path in bold_candidates if path.is_file()), None)
    return regular, bold


def _register_fonts() -> tuple[str, str]:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    regular, bold = _font_paths()
    if not regular:
        raise RuntimeError("На сервере не найден шрифт с поддержкой кириллицы")
    regular_name = "ConsiliumSans"
    bold_name = "ConsiliumSansBold" if bold else regular_name
    if regular_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(regular_name, str(regular)))
    if bold and bold_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(bold_name, str(bold)))
    return regular_name, bold_name


def build_health_passport_pdf(passport: dict, profile: dict) -> bytes:
    """Return a compact, readable PDF built only from a saved passport."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    regular_font, bold_font = _register_fonts()
    output = BytesIO()
    document = SimpleDocTemplate(
        output, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=17 * mm, bottomMargin=16 * mm,
        title="Паспорт здоровья", author="Консилиум",
    )
    ink = colors.HexColor("#17231f")
    muted = colors.HexColor("#60716b")
    green = colors.HexColor("#176f57")
    pale = colors.HexColor("#edf7f2")
    line = colors.HexColor("#d8e5df")
    styles = getSampleStyleSheet()
    body = ParagraphStyle("PassportBody", parent=styles["BodyText"], fontName=regular_font, fontSize=9.5, leading=14, textColor=ink, spaceAfter=6)
    title = ParagraphStyle("PassportTitle", parent=body, fontName=bold_font, fontSize=23, leading=27, textColor=ink, spaceAfter=4)
    subtitle = ParagraphStyle("PassportSubtitle", parent=body, fontSize=9, textColor=muted, spaceAfter=14)
    heading = ParagraphStyle("PassportHeading", parent=body, fontName=bold_font, fontSize=13, leading=17, textColor=green, spaceBefore=9, spaceAfter=7)
    question_style = ParagraphStyle("PassportQuestion", parent=body, leftIndent=10, firstLineIndent=-10, spaceAfter=7)
    note = ParagraphStyle("PassportNote", parent=body, fontSize=8, leading=11, textColor=muted, alignment=TA_CENTER, spaceBefore=12)

    def paragraph(value: object, style=body) -> Paragraph:
        return Paragraph(escape(str(value or "")).replace("\n", "<br/>"), style)

    def bullet(value: object) -> Paragraph:
        return Paragraph(f"•&nbsp;&nbsp;{escape(str(value or ''))}", body)

    story: list = []
    preferred_name = str(profile.get("preferred_name") or "").strip()
    generated_at = str(passport.get("generated_at") or "")[:10]
    story.extend([
        Paragraph("Паспорт здоровья", title),
        Paragraph(
            escape("Персональное резюме по данным анкеты")
            + (f" · {escape(preferred_name)}" if preferred_name else "")
            + (f" · {escape(generated_at)}" if generated_at else ""),
            subtitle,
        ),
        paragraph(passport.get("overview", "")),
    ])

    metrics = passport.get("metrics") or []
    if metrics:
        story.append(Paragraph("Ключевые показатели", heading))
        label_style = ParagraphStyle("MetricLabel", parent=body, fontName=bold_font, fontSize=8.5)
        rows = []
        for item in metrics[:8]:
            metric_value = str(item.get("value") or "")
            if item.get("note"):
                metric_value += f"\n{item['note']}"
            rows.append([paragraph(item.get("label", ""), label_style), paragraph(metric_value)])
        table = Table(rows, colWidths=[48 * mm, 112 * mm], hAlign="LEFT")
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), pale), ("GRID", (0, 0), (-1, -1), 0.5, line),
            ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ]))
        story.append(table)

    attention = passport.get("attention_points") or []
    if attention:
        story.append(Paragraph("На что обратить внимание", heading))
        attention_title = ParagraphStyle("AttentionTitle", parent=body, fontName=bold_font, textColor=ink, spaceAfter=2)
        for item in attention[:5]:
            block = [paragraph(item.get("title", ""), attention_title), paragraph(item.get("reason", ""))]
            action = str(item.get("action") or "").strip()
            if action:
                block.append(paragraph(f"Практический шаг: {action}"))
            story.append(KeepTogether(block + [Spacer(1, 3)]))

    strengths = passport.get("protective_factors") or []
    if strengths:
        strength_items = [bullet(item) for item in strengths[:5]]
        story.append(KeepTogether([
            Paragraph("Что уже работает в вашу пользу", heading),
            strength_items[0],
        ]))
        story.extend(strength_items[1:])

    next_steps = passport.get("next_steps") or []
    if next_steps:
        step_items = [
            Paragraph(f"{index}.&nbsp;&nbsp;{escape(str(item))}", body)
            for index, item in enumerate(next_steps[:6], 1)
        ]
        story.append(KeepTogether([
            Paragraph("Следующие шаги", heading),
            step_items[0],
        ]))
        story.extend(step_items[1:])

    questions = passport.get("questions") or []
    if questions:
        story.append(KeepTogether([
            Paragraph("Вопросы для обсуждения со специалистом", heading),
            *[
                Paragraph(f"{index}.&nbsp;&nbsp;{escape(str(item))}", question_style)
                for index, item in enumerate(questions[:3], 1)
            ],
        ]))

    story.extend([
        Spacer(1, 7),
        paragraph(passport.get("disclaimer") or "Паспорт составлен по ответам анкеты, не является диагнозом и не заменяет консультацию врача.", note),
    ])
    document.build(story)
    return output.getvalue()

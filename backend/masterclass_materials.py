"""Downloadable materials for the psychology master-class."""

from __future__ import annotations

from html import escape
from io import BytesIO

from .health_passport import _register_fonts


PRACTICES = (
    (
        "Стоп-кран", "Для момента, когда накрывает",
        (
            "Встаньте и выйдите из комнаты.",
            "Умойтесь прохладной водой.",
            "Сделайте 10 медленных приседаний или пройдитесь по лестнице.",
            "Вернитесь и скажите то, что чувствуете, без обвинений.",
        ),
        "Переключает тело, чтобы мозг вышел из режима «бей или беги». Это не слабость, это управление.",
    ),
    (
        "Дыхание 4–7–8", "Для тревоги и бессонницы",
        (
            "Вдохните носом на 4 счёта.", "Задержите дыхание на 7 счётов.",
            "Выдохните ртом на 8 счётов.", "Повторите 4 раза.",
        ),
        "Длинный выдох включает в теле режим покоя. Если закружится голова, вернитесь к обычному дыханию.",
    ),
    (
        "Заземление 5–4–3–2–1", "При панике и тревоге",
        (
            "Назовите 5 предметов, которые видите вокруг.",
            "Прислушайтесь и назовите 4 звука.",
            "Почувствуйте 3 прикосновения к телу: одежда, стул, пол.",
            "Вдохните и почувствуйте 2 запаха.",
            "Почувствуйте 1 вкус: глоток воды или чай.",
        ),
        "Возвращает внимание из тревожных мыслей в тело и в «здесь и сейчас».",
    ),
    (
        "Дневник «выжало / дало»", "Чтобы понять, откуда уходит энергия",
        (
            "Каждый вечер выделите 5 минут.",
            "Запишите, что за день вымотало вас больше всего.",
            "Запишите, что, наоборот, придало сил и порадовало.",
            "Решите, что вы могли бы сделать по-другому завтра.",
        ),
        "Записи показывают конкретные источники усталости и восстановления, а не оставляют ощущение сплошного тяжёлого дня.",
    ),
    (
        "Одно дело", "Для тех, кто всё время в делах",
        (
            "Утром выберите одно дело, которое важнее всего.",
            "Не список из 20 дел — одно.", "Сделайте его первым, остальное потом.",
            "Вечером отметьте, что получилось.",
        ),
        "Снижает тревогу «я ничего не успеваю» до конкретного «я сделал главное».",
    ),
)


def build_masterclass_practices_pdf(company: str = "") -> bytes:
    """Build a real PDF containing the five practices shown on the page."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    regular_font, bold_font = _register_fonts()
    output = BytesIO()
    document = SimpleDocTemplate(
        output, pagesize=A4, leftMargin=17 * mm, rightMargin=17 * mm,
        topMargin=16 * mm, bottomMargin=15 * mm,
        title="5 практик, которые помогают вернуть силы", author="ГК «Человек»",
    )
    styles = getSampleStyleSheet()
    ink = colors.HexColor("#17231f")
    green = colors.HexColor("#176f57")
    pale = colors.HexColor("#edf7f2")
    line = colors.HexColor("#d8e5df")
    muted = colors.HexColor("#60716b")
    title = ParagraphStyle("MasterclassTitle", parent=styles["Title"], fontName=bold_font, fontSize=22, leading=27, textColor=green, spaceAfter=7)
    subtitle = ParagraphStyle("MasterclassSubtitle", parent=styles["BodyText"], fontName=regular_font, fontSize=9.5, leading=14, textColor=muted, spaceAfter=15)
    heading = ParagraphStyle("PracticeHeading", parent=styles["Heading2"], fontName=bold_font, fontSize=13, leading=17, textColor=ink, spaceAfter=2)
    body = ParagraphStyle("PracticeBody", parent=styles["BodyText"], fontName=regular_font, fontSize=9.5, leading=14, textColor=ink, spaceAfter=5)
    small = ParagraphStyle("PracticeSmall", parent=body, fontSize=8.5, leading=12, textColor=muted)
    why = ParagraphStyle("PracticeWhy", parent=body, fontSize=9, leading=13, textColor=colors.HexColor("#496158"))

    company_text = escape(str(company or "вашей компании"))
    story = [
        Paragraph("5 практик, которые помогают вернуть силы", title),
        Paragraph(f"Материал мастер-класса для сотрудников {company_text}. Сохраните его и возвращайтесь к практикам, когда нужна поддержка.", subtitle),
    ]
    for index, (name, purpose, steps, explanation) in enumerate(PRACTICES, 1):
        content = [
            Paragraph(f"{index}. {escape(name)}", heading),
            Paragraph(escape(purpose), small),
            *[Paragraph(f"•&nbsp;&nbsp;{escape(step)}", body) for step in steps],
            Spacer(1, 2),
            Table(
                [[Paragraph(f"<b>Почему работает:</b> {escape(explanation)}", why)]],
                colWidths=[164 * mm],
                style=TableStyle([
                    ("BACKGROUND", (0, 0), (-1, -1), pale),
                    ("BOX", (0, 0), (-1, -1), 0.5, line),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 7),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ]),
            ),
        ]
        story.extend([KeepTogether(content), Spacer(1, 10)])
    story.extend([
        Paragraph("Если состояние не улучшается больше двух недель, мешает спать, работать или общаться — обсудите его со специалистом. При непосредственной угрозе жизни или безопасности звоните 112.", subtitle),
        Paragraph("ГК «Человек» · Экосистема здоровья", small),
    ])
    document.build(story)
    return output.getvalue()

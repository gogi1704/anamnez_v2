"""First-party product analytics without medical content."""

from __future__ import annotations

import json
import copy
import re
import sqlite3
import threading
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import settings


_write_lock = threading.Lock()
_report_cache_lock = threading.Lock()
_report_cache: dict[tuple, tuple[float, dict]] = {}
REPORT_CACHE_SECONDS = 15
TEST_COMPANY_INN = "123123"
REPORT_TIMEZONE = timezone(timedelta(hours=3))


def _invalidate_report_cache() -> None:
    """Discard aggregates after any successful analytics write."""
    with _report_cache_lock:
        _report_cache.clear()


def _statistics_excluded_chel_ids() -> set[str]:
    """Load user identities excluded from every product-analytics report."""
    excluded = {"chel_legacy", "chel_test_default"}
    main_conn = None
    try:
        main_conn = sqlite3.connect(settings.database_path, timeout=5)
        excluded.update(
            str(row[0]) for row in main_conn.execute(
                "SELECT chel_id FROM user_profile WHERE TRIM(company_inn) = ?",
                (TEST_COMPANY_INN,),
            ).fetchall()
        )
    except sqlite3.Error:
        # The primary database can still be bootstrapping when analytics starts.
        pass
    finally:
        if main_conn is not None:
            main_conn.close()
    return excluded


def _is_test_user(chel_id: str) -> bool:
    return str(chel_id or "") in _statistics_excluded_chel_ids()

ALLOWED_EVENTS = {
    "landing_viewed", "welcome_viewed", "welcome_continued", "auth_gate_viewed",
    "registration_method_selected", "anonymous_warning_viewed", "anonymous_warning_cancelled",
    "registration_completed", "registration_failed", "messenger_auth_started", "funnel_action",
    "onboarding_screen_viewed", "onboarding_screen_action",
    "messenger_link_modal_viewed",
    "messenger_auth_completed", "messenger_auth_failed", "appearance_viewed",
    "appearance_completed", "questionnaire_started", "question_viewed",
    "question_answered", "question_skipped", "question_back", "question_validation_error",
    "questionnaire_completed", "not_medical_exam_selected", "examinations_offer_viewed",
    "examinations_opened", "examination_selected", "examination_deselected",
    "examinations_skip_clicked", "examinations_objection_viewed", "examinations_skip_recovered",
    "examinations_skipped", "examinations_selection_completed", "examination_selection_confirmed", "payment_viewed",
    "payment_method_selected", "payment_online_unavailable", "payment_completed",
    "payment_created", "payment_redirected", "payment_succeeded", "payment_canceled",
    "payment_pending", "payment_abandoned", "purchases_viewed", "payment_unavailable_viewed",
    "payment_continued", "payment_retried", "purchase_attempt_removed",
    "payment_return_viewed", "payment_success_viewed", "payment_result_viewed",
    "onboarding_completed", "completion_viewed", "completion_skipped_viewed", "capabilities_viewed",
    "capabilities_closed", "install_offer_viewed", "install_clicked", "install_dismissed",
    "app_installed", "app_opened", "chat_opened", "conversation_created", "message_sent",
    "first_message_sent", "ai_response_completed", "ai_response_error", "council_started",
    "council_completed", "council_error", "human_requested", "human_channel_selected",
    "manager_joined", "human_request_closed", "lab_results_requested", "lab_results_found",
    "lab_results_not_found", "lab_interpretation_started", "lab_interpretation_completed",
    "lab_interpretation_error", "lab_interpretation_profile_requested",
    "lab_interpretation_profile_completed", "lab_results_notification_requested", "result_entry_started",
    "api_error", "javascript_error", "performance_measured",
}
REGISTRATION_METHODS = {"anonymous", "max", "telegram", "result"}

ALLOWED_PROPERTIES = {
    "question_key", "step_number", "optional", "provider", "method", "result",
    "error_code", "status_code", "exam_id", "recommended", "selected_count",
    "total_price", "install_method", "channel", "agent", "route_action", "duration_ms",
    "response_ms", "screen", "previous_screen", "source", "campaign", "medium",
    "app_mode", "page_version", "connection_type", "conversation_count",
    "document_count", "cached", "reason", "font_size", "stage", "action",
    "selection_id", "exam_name", "context", "linked_count",
}

FUNNEL_BREAKDOWNS = {
    "registration_completed": [
        ("max", "MAX", "registration_completed", "method", "max"),
        ("telegram", "Telegram", "registration_completed", "method", "telegram"),
        ("anonymous", "Анонимно", "registration_completed", "method", "anonymous"),
        ("registration_back", "Кнопка «Назад»", "anonymous_warning_cancelled", "", ""),
        ("anonymous_button", "Кнопка «Войти анонимно»", "anonymous_warning_viewed", "", ""),
    ],
    "appearance_completed": [
        ("standard", "Маленький", "appearance_completed", "font_size", "standard"),
        ("large", "Большой", "appearance_completed", "font_size", "large"),
        ("extra", "Очень большой", "appearance_completed", "font_size", "extra"),
    ],
    "examinations_offer_viewed": [
        ("edit_questionnaire", "Изменить анкету", "funnel_action", "action", "edit_questionnaire"),
        ("catalog_info", "Описание чек-апов", "funnel_action", "action", "catalog_info"),
        ("view_options", "Посмотреть варианты", "funnel_action", "action", "view_options"),
        ("skip", "Пропустить", "funnel_action", "action", "skip"),
        ("refuse", "↳ Всё равно отказаться", "funnel_action", "action", "refuse"),
        ("choose", "↳ Выбрать обследования", "funnel_action", "action", "choose_after_objection"),
    ],
    "examinations_opened": [
        ("back", "Кнопка «Назад»", "funnel_action", "action", "options_back"),
        ("pay_online", "Оплатить онлайн", "funnel_action", "action", "pay_online"),
        ("pay_at_exam", "Оплатить на медосмотре", "funnel_action", "action", "pay_at_exam"),
        ("nothing", "Ничего не выбирать", "funnel_action", "action", "nothing_selected"),
    ],
}

FUNNEL_STEPS = [
    ("registration_completed", "Регистрация завершена"),
    ("appearance_completed", "Размер текста выбран"),
    ("questionnaire_started", "Вход в анкету"),
    ("questionnaire_completed", "Выход из анкеты"),
    ("examinations_offer_viewed", "Предложение обследований показано"),
    ("examinations_opened", "Варианты обследований открыты"),
    ("examinations_selection_completed", "Выбор обследований завершён"),
    ("onboarding_completed", "Стартовый путь завершён"),
    ("capabilities_viewed", "Возможности показаны"),
    ("chat_opened", "Чат открыт"),
    ("first_message_sent", "Первое сообщение отправлено"),
    ("human_requested", "Запрошен человек"),
]

QUESTION_LABELS = {
    "company_inn": "ИНН предприятия", "preferred_name": "Имя", "age": "Возраст",
    "sex": "Пол", "height_cm": "Рост", "weight_kg": "Вес", "smoking": "Курение",
    "alcohol": "Алкоголь", "activity": "Активность", "blood_pressure": "Давление",
    "dark_in_eyes": "Потемнение в глазах", "blood_sugar": "Сахар крови",
    "joint_pain": "Суставы", "fatigue": "Усталость",
    "conditions": "Хронические заболевания", "medications": "Лекарства",
    "allergies": "Аллергии", "notes": "Жалобы",
}

METRIC2_QUESTIONS = [
    ("company_inn", "Сообщите ИНН вашего предприятия", "input"),
    ("preferred_name", "Как к вам обращаться?", "input"),
    ("age", "Сколько вам полных лет?", "input"),
    ("sex", "Укажите пол для медицинского контекста", "choices"),
    ("height_cm", "Какой у вас рост?", "input"),
    ("weight_kg", "Какой у вас вес?", "input"),
    ("smoking", "Вы курите?", "choices"),
    ("alcohol", "Как часто вы употребляете алкоголь?", "choices"),
    ("activity", "Какой у вас уровень активности?", "choices"),
    ("blood_pressure", "Как вы оцениваете своё давление?", "choices"),
    ("dark_in_eyes", "Темнеет ли в глазах при резком подъёме?", "choices"),
    ("blood_sugar", "Знаете ли вы уровень сахара в крови?", "choices"),
    ("joint_pain", "Бывают боли или отёчность суставов?", "choices"),
    ("fatigue", "Беспокоит длительная усталость?", "choices"),
    ("conditions", "Есть хронические заболевания?", "textarea"),
    ("medications", "Какие лекарства принимаете постоянно?", "textarea"),
    ("allergies", "Есть аллергии?", "textarea"),
    ("notes", "Есть ли у вас жалобы?", "textarea"),
]
METRIC2_OPTIONAL_QUESTIONS = {
    "preferred_name", "conditions", "medications", "allergies", "notes",
}


def _metric2_spec(event: str, **properties) -> dict:
    return {"event": event, "properties": properties}


def _metric2_screen_definitions() -> list[dict]:
    online_payment_enabled = bool(
        settings.online_payments_enabled
        and settings.yookassa_shop_id.isdigit()
        and settings.yookassa_secret_key
    )
    screens = [
        {
            "id": "welcome", "title": "Приветствие", "stage": "Начало",
            "kind": "welcome", "description": "Первый экран нового пользователя.",
            "root": True,
            "legacy_reach": [_metric2_spec("welcome_viewed")],
            "actions": [{"id": "continue", "label": "Далее", "target": "registration", "legacy": [_metric2_spec("welcome_continued")]}],
        },
        {
            "id": "registration", "title": "Выбор способа входа", "stage": "Регистрация",
            "kind": "registration", "description": "Telegram, MAX или анонимный вход.",
            "legacy_reach": [_metric2_spec("auth_gate_viewed")],
            "actions": [
                {"id": "telegram", "label": "Продолжить с Telegram", "target": "appearance", "legacy": [_metric2_spec("registration_method_selected", provider="telegram")]},
                {"id": "max", "label": "Продолжить с MAX", "target": "appearance", "legacy": [_metric2_spec("registration_method_selected", provider="max")]},
                {"id": "anonymous", "label": "Войти анонимно", "target": "anonymous_warning", "legacy": [_metric2_spec("anonymous_warning_viewed")]},
            ],
        },
        {
            "id": "anonymous_warning", "title": "Предупреждение об анонимном входе", "stage": "Регистрация",
            "kind": "warning", "description": "Ответвление после выбора анонимного входа.",
            "parent_id": "registration", "branch": True,
            "legacy_reach": [_metric2_spec("anonymous_warning_viewed")],
            "actions": [
                {"id": "anonymous_confirm", "label": "Понимаю, продолжить", "target": "appearance", "legacy": [_metric2_spec("registration_completed", method="anonymous")]},
                {"id": "anonymous_cancel", "label": "Назад", "target": "registration", "legacy": [_metric2_spec("anonymous_warning_cancelled")]},
                {"id": "anonymous_close", "label": "Закрыть (×)", "target": "registration", "legacy": []},
            ],
        },
        {
            "id": "appearance", "title": "Выбор размера текста", "stage": "Настройка",
            "kind": "appearance", "description": "Размер интерфейса перед анкетой.",
            "legacy_reach": [_metric2_spec("appearance_viewed")],
            "actions": [
                {"id": "size_standard", "label": "Обычный", "exclusive_group": "font_size", "legacy": [_metric2_spec("appearance_completed", font_size="standard")]},
                {"id": "size_large", "label": "Крупный", "exclusive_group": "font_size", "legacy": [_metric2_spec("appearance_completed", font_size="large")]},
                {"id": "size_extra", "label": "Очень крупный", "exclusive_group": "font_size", "legacy": [_metric2_spec("appearance_completed", font_size="extra")]},
                {"id": "continue", "label": "Продолжить", "target": "question_company_inn", "legacy": []},
            ],
        },
    ]
    for index, (key, title, kind) in enumerate(METRIC2_QUESTIONS):
        next_target = (
            f"question_{METRIC2_QUESTIONS[index + 1][0]}"
            if index + 1 < len(METRIC2_QUESTIONS) else "exam_offer"
        )
        previous_target = (
            f"question_{METRIC2_QUESTIONS[index - 1][0]}" if index else "appearance"
        )
        actions = [{
            "id": "answer",
            "label": "Завершить анкету" if index == len(METRIC2_QUESTIONS) - 1 else "Продолжить",
            "target": next_target,
            "legacy": [_metric2_spec("question_answered", question_key=key)],
        }]
        if key in METRIC2_OPTIONAL_QUESTIONS:
            actions.append({
                "id": "skip", "label": "Пропустить", "target": next_target,
                "legacy": [_metric2_spec("question_skipped", question_key=key)],
            })
        if kind == "choices":
            # Track only the interaction, never the selected medical value.
            actions.insert(0, {
                "id": "select_option", "label": "Выбор варианта ответа",
                "legacy": [],
            })
        if index:
            actions.append({"id": "back", "label": "Назад", "target": previous_target, "legacy": []})
        if key == "company_inn":
            actions.append({"id": "not_medical_exam", "label": "Я не на мед-осмотр", "target_label": "Переход в сервис", "terminal_outcome": True, "legacy": [_metric2_spec("not_medical_exam_selected")]})
        screens.append({
            "id": f"question_{key}", "title": title, "stage": f"Анкета · {index + 1}/{len(METRIC2_QUESTIONS)}",
            "kind": f"question_{kind}", "question_key": key,
            "description": "Вопрос анкеты. Значения ответов в аналитику не передаются.",
            "legacy_reach": [_metric2_spec("question_viewed", question_key=key)],
            "actions": actions,
        })
    screens.extend([
        {
            "id": "result_existing", "title": "Возврат за результатами", "stage": "Результаты · возврат",
            "kind": "result_existing", "description": "Пользователь с заполненной анкетой сразу попал в чат и окно ввода номера пробирки.",
            "root": True, "flow": "result",
            "legacy_reach": [], "actions": [],
        },
        {
            "id": "exam_offer", "title": "Предложение дополнительных обследований", "stage": "Обследования",
            "kind": "exam_offer", "description": "Основное предложение после завершения анкеты.",
            "legacy_reach": [_metric2_spec("examinations_offer_viewed")],
            "actions": [
                {"id": "catalog_info", "label": "Посмотреть описания чек-апов", "target": "exam_catalog", "legacy": [_metric2_spec("funnel_action", action="catalog_info")]},
                {"id": "view_options", "label": "Да, выбрать анализы", "target": "exam_selection", "legacy": [_metric2_spec("funnel_action", action="view_options")]},
                {"id": "skip", "label": "Нет, не сейчас", "target": "exam_objection", "legacy": [_metric2_spec("funnel_action", action="skip")]},
                {"id": "edit_questionnaire", "label": "Изменить ответы анкеты", "target": "question_notes", "legacy": [_metric2_spec("funnel_action", action="edit_questionnaire")]},
            ],
        },
        {
            "id": "exam_catalog", "title": "Описание чек-апов", "stage": "Обследования · ответвление",
            "kind": "exam_catalog", "description": "Список составов, показаний и цен.",
            "parent_id": "exam_offer", "branch": True,
            "legacy_reach": [_metric2_spec("funnel_action", action="catalog_info")],
            "actions": [
                {"id": "choose", "label": "Выбрать анализы", "target": "exam_selection", "legacy": []},
                {"id": "back", "label": "Вернуться к вопросу", "target": "exam_offer", "legacy": []},
            ],
        },
        {
            "id": "exam_objection", "title": "Отработка возражения", "stage": "Обследования · ответвление",
            "kind": "exam_objection", "description": "Показывается после попытки отказаться.",
            "parent_id": "exam_offer", "branch": True,
            "legacy_reach": [_metric2_spec("examinations_objection_viewed")],
            "actions": [
                {"id": "choose", "label": "Выбрать обследования", "target": "exam_selection", "legacy": [_metric2_spec("funnel_action", action="choose_after_objection")]},
                {"id": "refuse", "label": "Всё равно отказаться", "target": "completion_skipped", "legacy": [_metric2_spec("funnel_action", action="refuse")]},
            ],
        },
        {
            "id": "exam_selection", "title": "Выбор наборов обследований", "stage": "Обследования",
            "kind": "exam_selection", "description": "Карточки обследований и итоговая сумма.",
            "legacy_reach": [_metric2_spec("examinations_opened")],
            "actions": [
                {"id": "select_exam", "label": "Выбрали хотя бы один набор", "legacy": [_metric2_spec("examination_selected")]},
                {"id": "continue", "label": "Далее", "target": "payment", "legacy": [_metric2_spec("examinations_selection_completed")]},
                {"id": "back", "label": "Назад", "target": "exam_offer", "legacy": [_metric2_spec("funnel_action", action="options_back")]},
                {"id": "nothing", "label": "Ничего не выбирать", "target": "exam_objection", "legacy": [_metric2_spec("funnel_action", action="nothing_selected")]},
            ],
        },
        {
            "id": "payment", "title": "Выбор способа оплаты", "stage": "Оплата",
            "kind": "payment", "description": "Состав заказа, сумма и способ оплаты.",
            "legacy_reach": [_metric2_spec("payment_viewed")],
            "actions": [
                {"id": "pay_online", "label": "Оплатить онлайн", "target": "payment_processing" if online_payment_enabled else "payment_unavailable", "legacy": [_metric2_spec("funnel_action", action="pay_online")]},
                {"id": "pay_at_exam", "label": "Оплатить на медосмотре", "target": "completion", "legacy": [_metric2_spec("funnel_action", action="pay_at_exam")]},
                {"id": "back", "label": "Вернуться к обследованиям", "target": "exam_selection", "legacy": []},
            ],
        },
        {
            "id": "payment_processing", "title": "Проверка онлайн-оплаты", "stage": "Оплата · обработка",
            "kind": "payment_processing", "description": "Экран после возвращения со страницы ЮKassa, пока сервис проверяет окончательный статус.",
            "parent_id": "payment", "branch": True,
            "legacy_reach": [_metric2_spec("payment_return_viewed")],
            "actions": [],
        },
        {
            "id": "payment_success", "title": "Онлайн-оплата подтверждена", "stage": "Оплата · успешно",
            "kind": "payment_success", "description": "Подтверждение оплаты и инструкция, где найти покупку.",
            "parent_id": "payment_processing", "branch": True, "display_as_main": True,
            "legacy_reach": [_metric2_spec("payment_success_viewed")],
            "actions": [
                {"id": "open_purchases", "label": "Открыть мои покупки", "target_label": "История покупок", "terminal_outcome": True, "legacy": []},
                {"id": "continue", "label": "Перейти в чат", "target_label": "Переход в сервис", "terminal_outcome": True, "legacy": []},
            ],
        },
        {
            "id": "payment_result", "title": "Оплата не завершена", "stage": "Оплата · результат",
            "kind": "payment_result", "description": "Показывается, если платёж отменён, не завершён или ещё обрабатывается.",
            "parent_id": "payment_processing", "branch": True,
            "legacy_reach": [_metric2_spec("payment_result_viewed")],
            "actions": [
                {"id": "retry", "label": "Вернуться к оплате", "target": "payment", "legacy": []},
                {"id": "purchases", "label": "Мои покупки", "target_label": "История покупок", "terminal_outcome": True, "legacy": []},
                {"id": "back", "label": "Назад", "target": "exam_selection", "legacy": []},
            ],
        },
        {
            "id": "payment_unavailable", "title": "Онлайн-оплата временно недоступна", "stage": "Оплата · ответвление",
            "kind": "payment_unavailable", "description": "Временная заглушка онлайн-оплаты.",
            "parent_id": "payment", "branch": True,
            "legacy_reach": [_metric2_spec("payment_unavailable_viewed")],
            "actions": [{"id": "close", "label": "Понятно", "target": "payment", "legacy": []}],
        },
        {
            "id": "completion", "title": "Обследования выбраны", "stage": "Завершение",
            "kind": "completion", "description": "Финальный экран перед переходом в сервис.",
            "legacy_reach": [_metric2_spec("completion_viewed")],
            "actions": [
                {"id": "install", "label": "Установить приложение", "target_label": "Установка приложения", "legacy": [_metric2_spec("install_clicked", screen="exam_completion")]},
                {"id": "later", "label": "Установлю позже", "target_label": "Переход в сервис", "terminal_outcome": True, "legacy": [_metric2_spec("install_dismissed", screen="exam_completion")]},
                {"id": "link_messenger", "label": "Привязать мессенджер", "target_label": "Привязка мессенджера", "legacy": [_metric2_spec("messenger_link_modal_viewed", source="exam_completion")]},
            ],
        },
        {
            "id": "completion_skipped", "title": "Анкета завершена без обследований", "stage": "Завершение",
            "kind": "completion_skipped", "description": "Финальный экран после подтверждённого отказа от дополнительных обследований.",
            "parent_id": "exam_objection", "branch": True, "display_as_main": True,
            "legacy_reach": [_metric2_spec("completion_skipped_viewed")],
            "actions": [
                {"id": "install", "label": "Установить приложение", "target_label": "Установка приложения", "legacy": [_metric2_spec("install_clicked", screen="exam_skip_completion")]},
                {"id": "continue", "label": "Перейти в Консилиум", "target_label": "Переход в сервис", "terminal_outcome": True, "legacy": [_metric2_spec("install_dismissed", screen="exam_skip_completion")]},
                {"id": "link_messenger", "label": "Привязать мессенджер", "target_label": "Привязка мессенджера", "legacy": [_metric2_spec("messenger_link_modal_viewed", source="exam_skip_completion")]},
            ],
        },
    ])
    screens.extend([
        {
            "id": "result_welcome", "title": "Получение результатов", "stage": "Результаты · начало",
            "kind": "result_welcome", "description": "Посадочный экран специальной ссылки для получения результатов.",
            "root": True, "flow": "result",
            "legacy_reach": [],
            "actions": [{"id": "continue", "label": "Далее", "target": "result_tube", "legacy": []}],
        },
        {
            "id": "result_tube", "title": "Номер пробирки", "stage": "Результаты · идентификация",
            "kind": "result_tube", "description": "Ввод индивидуального номера пробирки.",
            "parent_id": "result_welcome", "flow": "result",
            "legacy_reach": [],
            "actions": [{"id": "continue", "label": "Продолжить", "target": "result_messenger", "legacy": []}],
        },
        {
            "id": "result_messenger", "title": "Сохранение доступа", "stage": "Результаты · мессенджер",
            "kind": "result_messenger", "description": "Рекомендация привязать Telegram или MAX перед поиском результатов.",
            "parent_id": "result_tube", "flow": "result",
            "legacy_reach": [],
            "actions": [
                {"id": "link_messenger", "label": "Привязать мессенджер", "legacy": []},
                {"id": "continue", "label": "Найти результаты", "target": "result_search", "legacy": []},
            ],
        },
        {
            "id": "result_search", "title": "Поиск результатов", "stage": "Результаты · поиск",
            "kind": "result_search", "description": "Поиск документов по номеру пробирки.",
            "parent_id": "result_messenger", "flow": "result",
            "legacy_reach": [],
            "actions": [
                {"id": "found", "label": "Результаты найдены", "target": "result_found", "legacy": []},
                {"id": "not_found", "label": "Результаты пока не найдены", "target": "result_not_found", "legacy": []},
            ],
        },
        {
            "id": "result_found", "title": "Результаты готовы", "stage": "Результаты · готовы",
            "kind": "result_found", "description": "Документы найдены и доступны пользователю.",
            "parent_id": "result_search", "flow": "result",
            "legacy_reach": [],
            "actions": [{"id": "open_chat", "label": "Перейти в чат и получить консультацию", "target_label": "Чат", "terminal_outcome": True, "legacy": []}],
        },
        {
            "id": "result_not_found", "title": "Результаты ещё не готовы", "stage": "Результаты · ожидание",
            "kind": "result_not_found", "description": "Предложение получить уведомление после появления документов.",
            "parent_id": "result_search", "flow": "result",
            "legacy_reach": [],
            "actions": [
                {"id": "notify", "label": "Уведомить о готовности", "target": "result_notification", "legacy": []},
                {"id": "retry", "label": "Проверить ещё раз", "target": "result_search", "legacy": []},
                {"id": "open_chat", "label": "Перейти в чат", "target_label": "Чат", "terminal_outcome": True, "legacy": []},
            ],
        },
        {
            "id": "result_notification", "title": "Уведомление подключено", "stage": "Результаты · ожидание",
            "kind": "result_notification", "description": "Запрос на уведомление сохранён для привязанного мессенджера.",
            "parent_id": "result_not_found", "flow": "result",
            "legacy_reach": [],
            "actions": [{"id": "open_chat", "label": "Перейти в чат", "target_label": "Чат", "terminal_outcome": True, "legacy": []}],
        },
    ])
    return screens


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connection():
    settings.analytics_database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.analytics_database_path, timeout=5)
    conn.row_factory = sqlite3.Row
    excluded_from_statistics = _statistics_excluded_chel_ids()
    conn.create_function(
        "IS_STATS_USER", 1,
        lambda value: 0 if str(value or "") in excluded_from_statistics else 1,
        deterministic=True,
    )
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    if not settings.analytics_enabled:
        return
    with _write_lock, connection() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS analytics_sessions (
                session_id TEXT PRIMARY KEY,
                chel_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                entry_source TEXT NOT NULL DEFAULT '',
                campaign TEXT NOT NULL DEFAULT '',
                registration_method TEXT NOT NULL DEFAULT '',
                device_type TEXT NOT NULL DEFAULT 'other',
                operating_system TEXT NOT NULL DEFAULT 'Другое',
                browser TEXT NOT NULL DEFAULT 'Другое',
                app_mode TEXT NOT NULL DEFAULT 'browser',
                event_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS analytics_events (
                event_id TEXT PRIMARY KEY,
                chel_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                event_name TEXT NOT NULL,
                screen TEXT NOT NULL DEFAULT '',
                step_key TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                campaign TEXT NOT NULL DEFAULT '',
                registration_method TEXT NOT NULL DEFAULT '',
                device_type TEXT NOT NULL DEFAULT 'other',
                operating_system TEXT NOT NULL DEFAULT 'Другое',
                browser TEXT NOT NULL DEFAULT 'Другое',
                app_mode TEXT NOT NULL DEFAULT 'browser',
                duration_ms INTEGER,
                properties TEXT NOT NULL DEFAULT '{}',
                client_at TEXT,
                received_at TEXT NOT NULL,
                is_server INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_analytics_events_time ON analytics_events(received_at, event_name);
            CREATE INDEX IF NOT EXISTS idx_analytics_events_name_time_user ON analytics_events(event_name, received_at, chel_id);
            CREATE INDEX IF NOT EXISTS idx_analytics_events_user ON analytics_events(chel_id, received_at);
            CREATE INDEX IF NOT EXISTS idx_analytics_events_session ON analytics_events(session_id, received_at);
            CREATE INDEX IF NOT EXISTS idx_analytics_events_step ON analytics_events(event_name, step_key, received_at);
            CREATE INDEX IF NOT EXISTS idx_analytics_sessions_time ON analytics_sessions(started_at, last_seen_at);
            """
        )
        conn.commit()


def _safe_scalar(value: Any) -> str | int | float | bool | None:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value if abs(value) <= 100_000_000 else None
    if isinstance(value, str):
        return " ".join(value.split())[:120]
    return None


def sanitize_properties(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, item in value.items():
        if key not in ALLOWED_PROPERTIES:
            continue
        scalar = _safe_scalar(item)
        if scalar is not None:
            result[key] = scalar
    return result


def _device(user_agent: str) -> dict:
    value = " ".join(str(user_agent or "").split()).lower()
    if "android" in value:
        device_type, operating_system = "android", "Android"
    elif any(marker in value for marker in ("iphone", "ipad", "ipod")) or (
        "macintosh" in value and "mobile/" in value
    ):
        device_type, operating_system = "ios", "iOS"
    elif "windows" in value:
        device_type, operating_system = "desktop", "Windows"
    elif "macintosh" in value or "mac os x" in value:
        device_type, operating_system = "desktop", "macOS"
    elif "linux" in value or "x11" in value:
        device_type, operating_system = "desktop", "Linux"
    else:
        device_type, operating_system = "other", "Другое"
    if "samsungbrowser/" in value:
        browser = "Samsung Internet"
    elif any(marker in value for marker in ("edg/", "edga/", "edgios/")):
        browser = "Edge"
    elif "opr/" in value or "opera" in value:
        browser = "Opera"
    elif "crios/" in value or "chrome/" in value:
        browser = "Chrome"
    elif "fxios/" in value or "firefox/" in value:
        browser = "Firefox"
    elif "safari/" in value:
        browser = "Safari"
    else:
        browser = "Другое"
    return {"device_type": device_type, "operating_system": operating_system, "browser": browser}


def record_events(chel_id: str, events: list[dict], *, user_agent: str = "", is_server: bool = False) -> dict:
    if not settings.analytics_enabled or not isinstance(events, list):
        return {"accepted": 0, "duplicates": 0}
    if _is_test_user(chel_id):
        return {"accepted": 0, "duplicates": 0}
    now = _now()
    device = _device(user_agent)
    prepared = []
    for raw in events[:50]:
        if not isinstance(raw, dict):
            continue
        event_name = str(raw.get("event_name", "")).strip()
        if event_name not in ALLOWED_EVENTS:
            continue
        properties = sanitize_properties(raw.get("properties"))
        event_id = str(raw.get("event_id") or uuid.uuid4())[:80]
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,80}", event_id):
            event_id = str(uuid.uuid4())
        session_id = str(raw.get("session_id") or f"server-{chel_id}")[:80]
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,80}", session_id):
            session_id = str(uuid.uuid4())
        source = str(properties.get("source", raw.get("source", "")))[:80]
        campaign = str(properties.get("campaign", raw.get("campaign", "")))[:100]
        method = str(properties.get("method", properties.get("provider", "")))[:30]
        registration_method = method if event_name == "registration_completed" and method in REGISTRATION_METHODS else ""
        app_mode = str(properties.get("app_mode", raw.get("app_mode", "browser")))[:20]
        duration = properties.get("duration_ms")
        duration_ms = max(0, min(int(duration), 86_400_000)) if isinstance(duration, (int, float)) else None
        prepared.append({
            "event_id": event_id, "chel_id": str(chel_id)[:80], "session_id": session_id,
            "event_name": event_name, "screen": str(properties.get("screen", raw.get("screen", "")))[:80],
            "step_key": str(properties.get("question_key", raw.get("step_key", "")))[:80],
            "source": source, "campaign": campaign, "registration_method": registration_method,
            "device_type": device["device_type"], "operating_system": device["operating_system"],
            "browser": device["browser"], "app_mode": app_mode, "duration_ms": duration_ms,
            "properties": json.dumps(properties, ensure_ascii=False),
            "client_at": str(raw.get("client_at", ""))[:40] or None,
            "received_at": now, "is_server": int(bool(is_server)),
        })
    if not prepared:
        return {"accepted": 0, "duplicates": 0}
    accepted = 0
    with _write_lock, connection() as conn:
        for event in prepared:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO analytics_events
                (event_id, chel_id, session_id, event_name, screen, step_key, source, campaign,
                 registration_method, device_type, operating_system, browser, app_mode,
                 duration_ms, properties, client_at, received_at, is_server)
                VALUES (:event_id, :chel_id, :session_id, :event_name, :screen, :step_key,
                 :source, :campaign, :registration_method, :device_type, :operating_system,
                 :browser, :app_mode, :duration_ms, :properties, :client_at, :received_at, :is_server)""",
                event,
            )
            if cursor.rowcount:
                accepted += 1
                conn.execute(
                    """INSERT INTO analytics_sessions
                    (session_id, chel_id, started_at, last_seen_at, entry_source, campaign,
                     registration_method, device_type, operating_system, browser, app_mode, event_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    ON CONFLICT(session_id) DO UPDATE SET
                      last_seen_at=excluded.last_seen_at,
                      entry_source=COALESCE(NULLIF(analytics_sessions.entry_source,''), excluded.entry_source),
                      campaign=COALESCE(NULLIF(analytics_sessions.campaign,''), excluded.campaign),
                      registration_method=COALESCE(NULLIF(excluded.registration_method,''), analytics_sessions.registration_method),
                      app_mode=excluded.app_mode, event_count=analytics_sessions.event_count+1""",
                    (event["session_id"], event["chel_id"], now, now, event["source"],
                     event["campaign"], event["registration_method"], event["device_type"],
                     event["operating_system"], event["browser"], event["app_mode"]),
                )
        conn.commit()
    if accepted:
        _invalidate_report_cache()
    return {"accepted": accepted, "duplicates": len(prepared) - accepted}


def record_server_event(
    chel_id: str, event_name: str, properties: dict | None = None, *,
    user_agent: str = "", session_id: str = "",
) -> None:
    try:
        record_events(chel_id, [{
            "event_id": f"srv-{uuid.uuid4()}", "session_id": session_id or f"server-{chel_id}",
            "event_name": event_name, "properties": properties or {}, "client_at": _now(),
        }], user_agent=user_agent, is_server=True)
    except Exception:
        # Analytics must never break a medical workflow.
        return


def _period_start(period: str) -> str | None:
    if period == "all":
        return None
    days = 1 if period == "today" else max(1, min(int(period or "30"), 3650))
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) if period == "today" else now - timedelta(days=days)
    return start.isoformat()


def _filters(
    period: str, device: str, method: str, source: str,
    date_from: str = "", date_to: str = "",
) -> tuple[str, list]:
    clauses, params = ["IS_STATS_USER(e.chel_id) = 1"], []
    date_from = str(date_from or "").strip()
    date_to = str(date_to or "").strip()
    try:
        from_date = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=REPORT_TIMEZONE) if date_from else None
        to_date = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=REPORT_TIMEZONE) if date_to else None
    except ValueError as exc:
        raise ValueError("Дата должна быть указана в формате ГГГГ-ММ-ДД") from exc
    if from_date and to_date and from_date > to_date:
        raise ValueError("Дата начала периода не может быть позже даты окончания")
    if from_date or to_date:
        if from_date:
            clauses.append("e.received_at >= ?")
            params.append(from_date.astimezone(timezone.utc).isoformat())
        if to_date:
            clauses.append("e.received_at < ?")
            params.append((to_date + timedelta(days=1)).astimezone(timezone.utc).isoformat())
    else:
        start = _period_start(period)
        if start:
            clauses.append("e.received_at >= ?")
            params.append(start)
    if device:
        clauses.append("e.device_type = ?")
        params.append(device)
    if method:
        clauses.append(
            "EXISTS (SELECT 1 FROM analytics_events registration_event "
            "WHERE registration_event.chel_id=e.chel_id "
            "AND registration_event.event_name='registration_completed' "
            "AND COALESCE(NULLIF(json_extract(registration_event.properties,'$.method'),''),"
            "registration_event.registration_method)=?)"
        )
        params.append(method)
    if source:
        clauses.append("(e.source = ? OR s.entry_source = ?)")
        params.extend([source, source])
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def _payment_date_bounds(period: str, date_from: str = "", date_to: str = "") -> tuple[str | None, str | None]:
    """Return UTC bounds matching the admin analytics period."""
    date_from = str(date_from or "").strip()
    date_to = str(date_to or "").strip()
    try:
        from_date = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=REPORT_TIMEZONE) if date_from else None
        to_date = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=REPORT_TIMEZONE) if date_to else None
    except ValueError as exc:
        raise ValueError("Дата должна быть указана в формате ГГГГ-ММ-ДД") from exc
    if from_date and to_date and from_date > to_date:
        raise ValueError("Дата начала периода не может быть позже даты окончания")
    if from_date or to_date:
        start = from_date.astimezone(timezone.utc).isoformat() if from_date else None
        end = (to_date + timedelta(days=1)).astimezone(timezone.utc).isoformat() if to_date else None
        return start, end
    return _period_start(period), None


def _current_examination_labels() -> dict[str, str]:
    """Use current catalog names in analytics, including historical events."""
    conn = None
    try:
        conn = sqlite3.connect(settings.database_path, timeout=5)
        return {
            str(row[0]): str(row[1])
            for row in conn.execute("SELECT id, name FROM examination_catalog").fetchall()
        }
    except sqlite3.Error:
        return {}
    finally:
        if conn is not None:
            conn.close()


def _payment_statistics(
    period: str, date_from: str = "", date_to: str = "", *,
    eligible_users: set[str] | None = None, at_exam_users: int = 0,
) -> dict:
    """Aggregate payment orders without removing hidden audit records."""
    start, end = _payment_date_bounds(period, date_from, date_to)
    clauses = ["po.chel_id NOT IN ('chel_legacy','chel_test_default')", "TRIM(COALESCE(up.company_inn,'')) <> ?"]
    params: list[Any] = [TEST_COMPANY_INN]
    if start:
        clauses.append("po.created_at >= ?")
        params.append(start)
    if end:
        clauses.append("po.created_at < ?")
        params.append(end)
    catalog_labels = _current_examination_labels()
    report = {
        "summary": {
            "attempts": 0, "users": 0, "succeeded": 0, "successful_users": 0,
            "conversion": 0.0, "revenue_kopecks": 0, "pending": 0,
            "unsuccessful": 0, "at_exam_users": int(at_exam_users), "test_attempts": 0,
        },
        "statuses": [], "daily": [], "items": [], "recent": [],
    }
    main_conn = None
    try:
        main_conn = sqlite3.connect(settings.database_path, timeout=5)
        main_conn.row_factory = sqlite3.Row
        rows = [dict(row) for row in main_conn.execute(
            """SELECT po.id,po.chel_id,po.status,po.amount_kopecks,po.items,po.paid,
               po.test,po.created_at,po.updated_at,po.paid_at,po.canceled_at
               FROM payment_orders po LEFT JOIN user_profile up ON up.chel_id=po.chel_id
               WHERE """ + " AND ".join(clauses) + " ORDER BY po.created_at DESC",
            params,
        ).fetchall()]
    except sqlite3.Error:
        return report
    finally:
        if main_conn is not None:
            main_conn.close()
    if eligible_users is not None:
        rows = [row for row in rows if str(row.get("chel_id", "")) in eligible_users]
    users = {str(row["chel_id"]) for row in rows}
    succeeded = [row for row in rows if row["status"] == "succeeded" and bool(row["paid"])]
    production_succeeded = [row for row in succeeded if not bool(row["test"])]
    successful_users = {str(row["chel_id"]) for row in succeeded}
    pending_statuses = {"creating", "pending", "waiting_for_capture"}
    unsuccessful_statuses = {"canceled", "abandoned", "failed"}
    report["summary"].update({
        "attempts": len(rows), "users": len(users), "succeeded": len(succeeded),
        "successful_users": len(successful_users),
        "conversion": round(len(successful_users) / len(users) * 100, 1) if users else 0.0,
        "revenue_kopecks": sum(int(row["amount_kopecks"] or 0) for row in production_succeeded),
        "pending": sum(row["status"] in pending_statuses for row in rows),
        "unsuccessful": sum(row["status"] in unsuccessful_statuses for row in rows),
        "test_attempts": sum(bool(row["test"]) for row in rows),
    })
    status_labels = {
        "succeeded": "Оплачено", "pending": "Ожидает оплаты",
        "waiting_for_capture": "Подтверждается", "creating": "Создаётся",
        "canceled": "Отменено", "abandoned": "Не завершено", "failed": "Ошибка",
    }
    status_counts: dict[str, int] = defaultdict(int)
    daily: dict[str, dict] = {}
    item_counts: dict[str, dict] = {}
    for row in rows:
        status_counts[str(row["status"])] += 1
        day = str(row["created_at"] or "")[:10]
        aggregate = daily.setdefault(day, {"date": day, "attempts": 0, "succeeded": 0, "revenue_kopecks": 0})
        aggregate["attempts"] += 1
        if row in succeeded:
            aggregate["succeeded"] += 1
            if not bool(row["test"]):
                aggregate["revenue_kopecks"] += int(row["amount_kopecks"] or 0)
            try:
                products = json.loads(row["items"] or "[]")
            except (json.JSONDecodeError, TypeError):
                products = []
            for product in products if isinstance(products, list) else []:
                item_id = str(product.get("id", "")).strip()
                if not item_id:
                    continue
                historical_label = str(product.get("name", "")).strip()
                item = item_counts.setdefault(item_id, {
                    "id": item_id,
                    "label": catalog_labels.get(item_id)
                    or ("Архивный чекап (снят с продажи)" if item_id == "ferritin" else historical_label or item_id),
                    "purchases": 0, "revenue_kopecks": 0,
                })
                item["purchases"] += 1
                if not bool(row["test"]):
                    item["revenue_kopecks"] += int(product.get("price", 0) or 0) * 100
    report["statuses"] = [
        {"status": key, "label": status_labels.get(key, key), "orders": value,
         "percent": round(value / len(rows) * 100, 1) if rows else 0.0}
        for key, value in sorted(status_counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    report["daily"] = [daily[key] for key in sorted(daily)]
    report["items"] = sorted(item_counts.values(), key=lambda item: (-item["purchases"], item["label"]))
    report["recent"] = [{
        "id": row["id"], "chel_id": row["chel_id"], "status": row["status"],
        "amount_kopecks": row["amount_kopecks"], "test": bool(row["test"]),
        "created_at": row["created_at"], "paid_at": row["paid_at"],
    } for row in rows[:25]]
    return report


def _cached_report(cache_name: str, arguments: tuple, builder) -> dict:
    """Cache expensive read-only aggregates for a few seconds.

    A manual refresh and tab switches can otherwise launch the same full
    SQLite aggregation several times in quick succession.
    A deep copy prevents API handlers from mutating the cached payload.
    """
    cache_key = (cache_name, str(settings.analytics_database_path), *arguments)
    now = time.monotonic()
    with _report_cache_lock:
        cached = _report_cache.get(cache_key)
        if cached and cached[0] > now:
            return copy.deepcopy(cached[1])

    # Do not keep the global cache mutex while SQLite and Python build the
    # report. Analytics and Metric 2.0 are independent reports; serialising
    # their cache misses made the second HTTP request wait for the entire
    # first calculation and could push it beyond the reverse-proxy timeout.
    result = builder()

    now = time.monotonic()
    with _report_cache_lock:
        # Another request may have populated the same key while this report
        # was being built. Prefer the already published value in that case.
        cached = _report_cache.get(cache_key)
        if cached and cached[0] > now:
            return copy.deepcopy(cached[1])
        if len(_report_cache) >= 64:
            expired = [key for key, value in _report_cache.items() if value[0] <= now]
            for key in expired:
                _report_cache.pop(key, None)
            if len(_report_cache) >= 64:
                oldest = min(_report_cache, key=lambda key: _report_cache[key][0])
                _report_cache.pop(oldest, None)
        _report_cache[cache_key] = (now + REPORT_CACHE_SECONDS, copy.deepcopy(result))
        return result


def _admin_report_uncached(
    period: str = "30", device: str = "", method: str = "", source: str = "",
    recent_page: int = 1, recent_limit: int = 25,
    date_from: str = "", date_to: str = "",
) -> dict:
    recent_page = max(1, int(recent_page))
    recent_limit = max(10, min(int(recent_limit), 100))
    where, params = _filters(period, device, method, source, date_from, date_to)
    join = " FROM analytics_events e LEFT JOIN analytics_sessions s ON s.session_id=e.session_id "
    with connection() as conn:
        totals = conn.execute(
            "SELECT COUNT(DISTINCT e.chel_id) total_users, "
            "COUNT(DISTINCT e.session_id) total_sessions, COUNT(*) total_events, "
            "COUNT(DISTINCT CASE WHEN e.event_name='registration_completed' "
            "THEN e.chel_id END) registered_users" + join + where,
            params,
        ).fetchone()
        total_users = int(totals["total_users"] or 0)
        total_sessions = int(totals["total_sessions"] or 0)
        total_events = int(totals["total_events"] or 0)
        registered_users = int(totals["registered_users"] or 0)

        funnel_event_names = [event_name for event_name, _ in FUNNEL_STEPS]
        funnel_placeholders = ",".join("?" for _ in funnel_event_names)
        funnel_extra = (" AND " if where else " WHERE ") + (
            f"e.event_name IN ({funnel_placeholders})"
        )
        funnel_counts = {
            str(row["event_name"]): int(row["users"] or 0)
            for row in conn.execute(
                "SELECT e.event_name, COUNT(DISTINCT e.chel_id) users" +
                join + where + funnel_extra + " GROUP BY e.event_name",
                [*params, *funnel_event_names],
            ).fetchall()
        }

        def breakdown(event_name: str) -> list[dict]:
            items = []
            for key, label, detail_event, property_name, property_value in FUNNEL_BREAKDOWNS.get(event_name, []):
                detail_where = where + (" AND " if where else " WHERE ") + "e.event_name = ?"
                detail_params = [*params, detail_event]
                if property_name:
                    if property_name == "method":
                        detail_where += " AND COALESCE(NULLIF(json_extract(e.properties,'$.method'),''),e.registration_method) = ?"
                    else:
                        detail_where += f" AND json_extract(e.properties,'$.{property_name}') = ?"
                    detail_params.append(property_value)
                row = conn.execute(
                    "SELECT COUNT(DISTINCT e.chel_id) users, COUNT(*) events" + join + detail_where,
                    detail_params,
                ).fetchone()
                items.append({"key": key, "label": label, "users": int(row[0] or 0), "events": int(row[1] or 0)})
            return items

        funnel, previous, first = [], None, None
        for event_name, label in FUNNEL_STEPS:
            count = funnel_counts.get(event_name, 0)
            if first is None:
                first = count
            funnel.append({
                "event_name": event_name, "label": label, "users": count,
                "from_previous": round(count / previous * 100, 1) if previous else (100.0 if count else 0.0),
                "from_start": round(count / first * 100, 1) if first else 0.0,
                "dropoff": max(0, (previous or count) - count),
                "details": breakdown(event_name),
            })
            previous = count

        def grouped(column: str, event_name: str = "") -> list[dict]:
            extra, grouped_params = "", list(params)
            if event_name:
                extra = (" AND " if where else " WHERE ") + "e.event_name = ?"
                grouped_params.append(event_name)
            rows = conn.execute(
                f"SELECT COALESCE(NULLIF({column},''),'Не указано') label, "
                f"COUNT(DISTINCT e.chel_id) users, COUNT(*) events{join}{where}{extra} "
                "GROUP BY label ORDER BY users DESC LIMIT 30", grouped_params,
            ).fetchall()
            return [dict(row) for row in rows]

        question_extra = (" AND " if where else " WHERE ") + "e.event_name IN ('question_viewed','question_answered','question_skipped','question_back','question_validation_error')"
        question_rows = conn.execute(
            """SELECT e.step_key,
             COUNT(DISTINCT CASE WHEN e.event_name='question_viewed' THEN e.chel_id END) viewed,
             COUNT(DISTINCT CASE WHEN e.event_name='question_answered' THEN e.chel_id END) answered,
             COUNT(DISTINCT CASE WHEN e.event_name='question_skipped' THEN e.chel_id END) skipped,
             COUNT(CASE WHEN e.event_name='question_back' THEN 1 END) back_count,
             COUNT(CASE WHEN e.event_name='question_validation_error' THEN 1 END) validation_errors,
             CAST(AVG(CASE WHEN e.event_name IN ('question_answered','question_skipped') THEN e.duration_ms END) AS INTEGER) avg_duration_ms
             """ + join + where + question_extra + " GROUP BY e.step_key", params,
        ).fetchall()
        questions = []
        for row in question_rows:
            item = dict(row)
            item["label"] = QUESTION_LABELS.get(item["step_key"], item["step_key"] or "Неизвестный вопрос")
            item["conversion"] = round(item["answered"] / item["viewed"] * 100, 1) if item["viewed"] else 0.0
            questions.append(item)
        questions.sort(key=lambda item: list(QUESTION_LABELS).index(item["step_key"]) if item["step_key"] in QUESTION_LABELS else 999)

        daily = [dict(row) for row in conn.execute(
            "SELECT SUBSTR(e.received_at,1,10) date, COUNT(DISTINCT e.chel_id) users, "
            "COUNT(DISTINCT e.session_id) sessions, COUNT(*) events" + join + where +
            " GROUP BY date ORDER BY date", params,
        ).fetchall()]
        error_extra = (" AND " if where else " WHERE ") + "(e.event_name LIKE '%_error' OR e.event_name='api_error')"
        errors = [dict(row) for row in conn.execute(
            "SELECT e.event_name label, COUNT(*) events, COUNT(DISTINCT e.chel_id) users" +
            join + where + error_extra + " GROUP BY e.event_name ORDER BY events DESC", params,
        ).fetchall()]
        recent_total = total_events
        recent_pages = max(1, (recent_total + recent_limit - 1) // recent_limit)
        recent_page = min(recent_page, recent_pages)
        recent_offset = (recent_page - 1) * recent_limit
        recent = []
        for row in conn.execute(
            "SELECT e.received_at, e.event_name, e.chel_id, e.session_id, e.screen, e.step_key, "
            "e.device_type, e.browser, e.properties, e.is_server" + join + where +
            " ORDER BY e.received_at DESC LIMIT ? OFFSET ?", [*params, recent_limit, recent_offset],
        ).fetchall():
            item = dict(row)
            try:
                item["properties"] = json.loads(item["properties"] or "{}")
            except json.JSONDecodeError:
                item["properties"] = {}
            recent.append(item)
        filter_options = {
            "devices": [row[0] for row in conn.execute("SELECT DISTINCT device_type FROM analytics_events WHERE IS_STATS_USER(chel_id) = 1 ORDER BY device_type") if row[0]],
            "methods": [row[0] for row in conn.execute(
                "SELECT DISTINCT COALESCE(NULLIF(json_extract(properties,'$.method'),''),registration_method) method "
                "FROM analytics_events WHERE IS_STATS_USER(chel_id) = 1 AND event_name='registration_completed' "
                "AND COALESCE(NULLIF(json_extract(properties,'$.method'),''),registration_method) "
                "IN ('anonymous','max','telegram','result') ORDER BY method"
            )],
            "sources": [row[0] for row in conn.execute("SELECT DISTINCT entry_source FROM analytics_sessions WHERE IS_STATS_USER(chel_id) = 1 AND entry_source<>'' ORDER BY entry_source LIMIT 100")],
        }
        registrations = grouped("COALESCE(NULLIF(json_extract(e.properties,'$.method'),''),e.registration_method)", "registration_completed")
        devices = grouped("e.device_type")
        operating_systems = grouped("e.operating_system")
        browsers = grouped("e.browser")
        sources = grouped("COALESCE(NULLIF(e.source,''),s.entry_source)")
        payment_eligible_users = None
        if device or method or source:
            payment_eligible_users = {
                str(row[0]) for row in conn.execute(
                    "SELECT DISTINCT e.chel_id" + join + where, params,
                ).fetchall()
            }
        at_exam_extra = (" AND " if where else " WHERE ") + (
            "e.event_name='payment_method_selected' "
            "AND json_extract(e.properties,'$.method')='at_exam'"
        )
        at_exam_users = int(conn.execute(
            "SELECT COUNT(DISTINCT e.chel_id)" + join + where + at_exam_extra, params,
        ).fetchone()[0] or 0)
        payments = _payment_statistics(
            period, date_from, date_to,
            eligible_users=payment_eligible_users, at_exam_users=at_exam_users,
        )
        selection_extra = (" AND " if where else " WHERE ") + (
            "e.event_name IN ('examinations_selection_completed','examination_selection_confirmed')"
        )
        selection_rows = conn.execute(
            "SELECT e.rowid event_order, e.event_id, e.chel_id, e.event_name, e.received_at, e.properties" +
            join + where + selection_extra + " ORDER BY e.received_at, e.rowid",
            params,
        ).fetchall()
        parsed_selection_rows = []
        latest_selections = {}
        for row in selection_rows:
            item = dict(row)
            try:
                item["properties"] = json.loads(item["properties"] or "{}")
            except json.JSONDecodeError:
                item["properties"] = {}
            parsed_selection_rows.append(item)
            if item["event_name"] != "examinations_selection_completed":
                continue
            selection_id = str(item["properties"].get("selection_id", "")).strip()
            if not selection_id:
                continue
            latest_selections[item["chel_id"]] = item

        selected_by_user = {chel_id: {} for chel_id in latest_selections}
        for item in parsed_selection_rows:
            if item["event_name"] != "examination_selection_confirmed":
                continue
            latest = latest_selections.get(item["chel_id"])
            if not latest:
                continue
            properties = item["properties"]
            if properties.get("selection_id") != latest["properties"].get("selection_id"):
                continue
            exam_id = str(properties.get("exam_id", "")).strip()
            if not exam_id:
                continue
            selected_by_user[item["chel_id"]][exam_id] = (
                str(properties.get("exam_name", "")).strip() or exam_id
            )

        completed_selection_users = len(latest_selections)
        users_with_selection = sum(bool(items) for items in selected_by_user.values())
        catalog_labels = _current_examination_labels()
        examination_counts = {}
        for items in selected_by_user.values():
            for exam_id, exam_name in items.items():
                aggregate = examination_counts.setdefault(
                    exam_id, {
                        "exam_id": exam_id,
                        "label": catalog_labels.get(exam_id)
                        or ("Архивный чекап (снят с продажи)" if exam_id == "ferritin" else exam_name),
                        "users": 0,
                    },
                )
                aggregate["users"] += 1
        examinations = []
        for item in examination_counts.values():
            item["percent_of_selectors"] = round(
                item["users"] / users_with_selection * 100, 1,
            ) if users_with_selection else 0.0
            item["percent_of_completed"] = round(
                item["users"] / completed_selection_users * 100, 1,
            ) if completed_selection_users else 0.0
            examinations.append(item)
        examinations.sort(key=lambda item: (-item["users"], item["label"].lower()))
        examination_summary = {
            "completed_users": completed_selection_users,
            "users_with_selection": users_with_selection,
            "selected_items": sum(item["users"] for item in examinations),
        }
    return {
        "generated_at": _now(), "period": period, "date_from": date_from, "date_to": date_to,
        "summary": {"users": registered_users, "visitors": total_users, "sessions": total_sessions, "events": total_events},
        "funnel": funnel, "daily": daily, "questions": questions,
        "registrations": registrations, "devices": devices,
        "operating_systems": operating_systems, "browsers": browsers,
        "sources": sources, "examinations": examinations,
        "examination_summary": examination_summary,
        "payments": payments,
        "errors": errors, "recent": recent,
        "recent_pagination": {
            "page": recent_page, "limit": recent_limit, "total": recent_total,
            "pages": recent_pages,
        },
        "filter_options": filter_options,
        "privacy": "Медицинские ответы, сообщения, телефоны и номера пробирок не сохраняются.",
    }


def admin_report(
    period: str = "30", device: str = "", method: str = "", source: str = "",
    recent_page: int = 1, recent_limit: int = 25,
    date_from: str = "", date_to: str = "",
) -> dict:
    arguments = (
        str(period), str(device), str(method), str(source), int(recent_page),
        int(recent_limit), str(date_from), str(date_to),
    )
    return _cached_report(
        "admin", arguments,
        lambda: _admin_report_uncached(
            period, device, method, source, recent_page, recent_limit,
            date_from, date_to,
        ),
    )


def _metric2_report_uncached(
    period: str = "30", device: str = "", method: str = "", source: str = "",
    date_from: str = "", date_to: str = "", flow: str = "standard",
) -> dict:
    """Return unique-user onboarding paths without storing questionnaire answers.

    Screen reach counts every user once, while the route itself is loop-erased.
    For example, ``A -> B -> C -> B -> D`` becomes ``A -> B -> D``.  This keeps
    the fact that C was visited, but prevents a return to B from creating the
    false incomplete edge ``C -> D``.
    """
    flow = str(flow or "standard").strip().lower()
    if flow not in {"standard", "result"}:
        raise ValueError("Неизвестная ветка Метрики 2.0")
    expected_context = "result" if flow == "result" else "onboarding"
    where, params = _filters(period, device, method, source, date_from, date_to)
    join = " FROM analytics_events e LEFT JOIN analytics_sessions s ON s.session_id=e.session_id "
    definitions = [
        definition for definition in _metric2_screen_definitions()
        if ("result" if definition.get("flow") == "result" else "standard") == flow
    ]
    relevant_events = {"onboarding_screen_viewed", "onboarding_screen_action"}
    for definition in definitions:
        relevant_events.update(
            spec["event"] for spec in definition.get("legacy_reach", [])
        )
        for action in definition.get("actions", []):
            relevant_events.update(spec["event"] for spec in action.get("legacy", []))
    relevant_events = sorted(relevant_events)
    event_filter = " AND e.event_name IN (" + ",".join("?" for _ in relevant_events) + ")"
    with connection() as conn:
        rows = conn.execute(
            "SELECT e.rowid event_order, e.event_name, e.chel_id, e.session_id, "
            "e.properties, e.client_at, e.received_at" + join + where + event_filter +
            " ORDER BY e.chel_id, COALESCE(NULLIF(e.client_at,''),e.received_at), "
            "e.received_at, e.rowid",
            [*params, *relevant_events],
        ).fetchall()
        parsed_rows = []
        for row in rows:
            try:
                properties = json.loads(row["properties"] or "{}")
            except (json.JSONDecodeError, TypeError):
                properties = {}
            parsed_rows.append({
                "event": str(row["event_name"] or ""),
                "chel_id": str(row["chel_id"] or ""),
                "session_id": str(row["session_id"] or ""),
                "properties": properties if isinstance(properties, dict) else {},
                "client_at": str(row["client_at"] or ""),
                "received_at": str(row["received_at"] or ""),
                "event_order": int(row["event_order"] or 0),
            })

        def matches(row: dict, spec: dict) -> bool:
            if row["event"] != spec.get("event"):
                return False
            return all(
                str(row["properties"].get(key, "")) == str(value)
                for key, value in spec.get("properties", {}).items()
            )

        rows_by_event: dict[str, list[dict]] = defaultdict(list)
        for row in parsed_rows:
            rows_by_event[row["event"]].append(row)

        def users_for(specs: list[dict]) -> set[str]:
            users: set[str] = set()
            for spec in specs:
                for row in rows_by_event.get(str(spec.get("event") or ""), []):
                    if row["chel_id"] and matches(row, spec):
                        users.add(row["chel_id"])
            return users

        definition_by_id = {definition["id"]: definition for definition in definitions}
        legacy_reach_by_event: dict[str, list[tuple[str, dict]]] = defaultdict(list)
        for definition in definitions:
            for spec in definition.get("legacy_reach", []):
                legacy_reach_by_event[str(spec.get("event") or "")].append(
                    (definition["id"], spec)
                )
        generic_paths: dict[str, list[tuple[tuple, str, str]]] = defaultdict(list)
        legacy_paths: dict[str, list[tuple[tuple, str]]] = defaultdict(list)
        for row in parsed_rows:
            chel_id = row["chel_id"]
            if not chel_id:
                continue
            order_key = (
                row["client_at"] or row["received_at"],
                row["received_at"], row["event_order"],
            )
            properties = row["properties"]
            if (
                row["event"] == "onboarding_screen_viewed"
                and properties.get("context") == expected_context
                and properties.get("screen") in definition_by_id
            ):
                previous_screen = str(properties.get("previous_screen") or "")
                if previous_screen not in definition_by_id:
                    previous_screen = ""
                generic_paths[chel_id].append(
                    (order_key, properties["screen"], previous_screen)
                )
                continue
            for screen_id, spec in legacy_reach_by_event.get(row["event"], []):
                if matches(row, spec):
                    legacy_paths[chel_id].append((order_key, screen_id))

        # Modern tracking is authoritative for a user. Legacy reach events are
        # used only for older users who have no generic screen-view events.
        reached_screens_by_user: dict[str, set[str]] = defaultdict(set)
        canonical_paths: dict[str, list[str]] = {}
        for chel_id in set(generic_paths) | set(legacy_paths):
            modern_path = generic_paths.get(chel_id, [])
            raw_path = modern_path or [
                (order_key, screen_id, "")
                for order_key, screen_id in legacy_paths.get(chel_id, [])
            ]
            path: list[str] = []
            seen_reach: set[str] = set()
            for _, screen_id, previous_screen in sorted(raw_path, key=lambda item: item[0]):
                # ``previous_screen`` is written atomically with the target
                # screen view.  It repairs the route when a visible return
                # screen was not separately emitted (for example closing a
                # modal that reveals the registration screen underneath).
                if (
                    previous_screen
                    and previous_screen != screen_id
                    and previous_screen in seen_reach
                    and (not path or path[-1] != previous_screen)
                ):
                    if previous_screen in path:
                        path = path[:path.index(previous_screen) + 1]
                    else:
                        path.append(previous_screen)
                seen_reach.add(screen_id)
                if path and path[-1] == screen_id:
                    continue
                if screen_id in path:
                    path = path[:path.index(screen_id) + 1]
                else:
                    path.append(screen_id)
            root_screen = next(
                (screen_id for screen_id in path if definition_by_id[screen_id].get("root")),
                "",
            )
            if not root_screen:
                continue
            canonical_paths[chel_id] = path[path.index(root_screen):]
            reached_screens_by_user[chel_id] = seen_reach

        start_cohort = set(canonical_paths)
        screen_users: dict[str, set[str]] = {
            definition["id"]: {
                chel_id for chel_id in canonical_paths
                if definition["id"] in reached_screens_by_user.get(chel_id, set())
            }
            for definition in definitions
        }
        edge_users: dict[tuple[str, str], set[str]] = defaultdict(set)
        for chel_id, path in canonical_paths.items():
            for source_id, target_id in zip(path, path[1:]):
                edge_users[(source_id, target_id)].add(chel_id)

        transition_explanations = {
            ("welcome", "registration"): "Новый пользователь продолжил и выбрал способ входа.",
            ("welcome", "appearance"): "Авторизация через Telegram или MAX завершилась до открытия экрана регистрации — например, пользователь перешёл по ссылке непосредственно из бота. Поэтому после приветствия выбор способа входа не показывался.",
            ("registration", "anonymous_warning"): "Пользователь выбрал анонимный вход.",
            ("registration", "appearance"): "Пользователь завершил вход через Telegram или MAX.",
            ("anonymous_warning", "appearance"): "Пользователь подтвердил анонимный вход.",
        }

        def transition_explanation(source_id: str, target_id: str) -> str:
            known = transition_explanations.get((source_id, target_id))
            if known:
                return known
            labels = [
                action["label"]
                for action in definition_by_id[source_id].get("actions", [])
                if action.get("target") == target_id
            ]
            if labels:
                return "Переход после действия: " + " / ".join(labels) + "."
            return (
                "Это не прямой переход: между экранами отсутствуют события. "
                "Пользователь мог пройти промежуточные шаги в старой версии, "
                "либо они были исключены как повторные просмотры и возвраты."
            )

        def is_direct_transition(source_id: str, target_id: str) -> bool:
            if (source_id, target_id) in transition_explanations:
                return True
            return any(
                action.get("target") == target_id
                for action in definition_by_id[source_id].get("actions", [])
            )

        root_ids = [definition["id"] for definition in definitions if definition.get("root")]
        start_cohort = set().union(*(screen_users.get(screen_id, set()) for screen_id in root_ids))
        start_users = len(start_cohort)
        result_screens, previous_main_id = [], ""
        for definition in definitions:
            screen_id = definition["id"]
            reached = screen_users[screen_id]
            parent_id = "" if definition.get("root") else (definition.get("parent_id") or previous_main_id)
            parent_users = screen_users.get(parent_id, set()) if parent_id else set()
            parent_count = len(parent_users)
            actions = []
            grouped_actions: dict[str, list[dict]] = defaultdict(list)
            for action in definition.get("actions", []):
                if action.get("target") or action.get("terminal_outcome"):
                    grouped_actions["final_transition"].append(action)
                elif action.get("exclusive_group"):
                    grouped_actions[f"choice:{action['exclusive_group']}"].append(action)
            final_group_action: dict[tuple[str, str], tuple[tuple, str]] = {}
            for group_id, group_actions in grouped_actions.items():
                for action in group_actions:
                    generic = _metric2_spec(
                        "onboarding_screen_action", screen=screen_id,
                        action=action["id"], context=definition.get("flow", "onboarding"),
                    )
                    for spec in [generic, *action.get("legacy", [])]:
                        for row in rows_by_event.get(str(spec.get("event") or ""), []):
                            chel_id = row["chel_id"]
                            if not chel_id or chel_id not in reached or not matches(row, spec):
                                continue
                            order_key = (
                                row["client_at"] or row["received_at"],
                                row["received_at"], row["event_order"],
                            )
                            current = final_group_action.get((group_id, chel_id))
                            if current is None or order_key >= current[0]:
                                # Only the last choice in an exclusive group
                                # represents the user's final outcome.
                                final_group_action[(group_id, chel_id)] = (
                                    order_key, action["id"],
                                )
            for action in definition.get("actions", []):
                generic = _metric2_spec(
                    "onboarding_screen_action", screen=screen_id,
                    action=action["id"], context=definition.get("flow", "onboarding"),
                )
                target_id = action.get("target", "")
                if target_id or action.get("terminal_outcome"):
                    action_group = "final_transition"
                elif action.get("exclusive_group"):
                    action_group = f"choice:{action['exclusive_group']}"
                else:
                    action_group = ""
                if action_group:
                    action_users = {
                        chel_id for (group_id, chel_id), (_, action_id) in final_group_action.items()
                        if group_id == action_group and action_id == action["id"]
                    }
                else:
                    action_users = users_for([generic, *action.get("legacy", [])]) & reached
                actions.append({
                    "id": action["id"], "label": action["label"],
                    "users": len(action_users),
                    "percent_of_screen": round(len(action_users) / len(reached) * 100, 1) if reached else 0.0,
                    "percent_of_start": round(len(action_users) / start_users * 100, 1) if start_users else 0.0,
                    "target": target_id,
                    "target_label": action.get("target_label", ""),
                    "counting_mode": (
                        "final_transition" if action_group == "final_transition"
                        else "final_choice" if action_group else "interaction"
                    ),
                })
            incoming = []
            outgoing = []
            outgoing_user_ids: set[str] = set()
            incomplete_transition_user_ids: set[str] = set()
            for (source_id, target_id), transition_users in edge_users.items():
                direct_transition = is_direct_transition(source_id, target_id)
                if not direct_transition and (source_id == screen_id or target_id == screen_id):
                    incomplete_transition_user_ids.update(transition_users)
                if target_id == screen_id:
                    source_count = len(screen_users.get(source_id, set()))
                    incoming.append({
                        "screen_id": source_id,
                        "title": definition_by_id[source_id]["title"],
                        "direct": direct_transition,
                        "explanation": transition_explanation(source_id, target_id),
                        "users": len(transition_users),
                        "percent_of_screen": round(len(transition_users) / len(reached) * 100, 1) if reached else 0.0,
                        "percent_of_source": round(len(transition_users) / source_count * 100, 1) if source_count else 0.0,
                        "percent_of_start": round(len(transition_users) / start_users * 100, 1) if start_users else 0.0,
                    })
                if source_id == screen_id:
                    target_count = len(screen_users.get(target_id, set()))
                    outgoing_user_ids.update(transition_users)
                    outgoing.append({
                        "screen_id": target_id,
                        "title": definition_by_id[target_id]["title"],
                        "direct": direct_transition,
                        "explanation": transition_explanation(source_id, target_id),
                        "users": len(transition_users),
                        "percent_of_screen": round(len(transition_users) / len(reached) * 100, 1) if reached else 0.0,
                        "percent_of_target": round(len(transition_users) / target_count * 100, 1) if target_count else 0.0,
                        "percent_of_start": round(len(transition_users) / start_users * 100, 1) if start_users else 0.0,
                    })
            incoming.sort(key=lambda item: (-item["users"], item["title"]))
            outgoing.sort(key=lambda item: (-item["users"], item["title"]))
            arrived_from_parent = reached & parent_users
            not_transitioned_user_ids = parent_users - reached
            alternate_path_user_ids: set[str] = set()
            actual_dropoff_user_ids: set[str] = set()
            for chel_id in not_transitioned_user_ids:
                path = canonical_paths.get(chel_id, [])
                if parent_id in path and path.index(parent_id) < len(path) - 1:
                    alternate_path_user_ids.add(chel_id)
                else:
                    actual_dropoff_user_ids.add(chel_id)
            dropoff_users = len(not_transitioned_user_ids)
            stopped_users = max(0, len(reached) - len(outgoing_user_ids))
            item = {
                key: value for key, value in definition.items()
                if key not in {"legacy_reach", "actions"}
            }
            item.update({
                "users": len(reached),
                "percent_of_start": round(len(reached) / start_users * 100, 1) if start_users else 0.0,
                "comparison_id": parent_id,
                "comparison_users": parent_count,
                "percent_of_parent": round(len(arrived_from_parent) / parent_count * 100, 1) if parent_count else (100.0 if definition.get("root") and reached else 0.0),
                "dropoff_users": dropoff_users,
                "dropoff_percent_of_parent": round(dropoff_users / parent_count * 100, 1) if parent_count else 0.0,
                "dropoff_percent_of_start": round(dropoff_users / start_users * 100, 1) if start_users else 0.0,
                "alternate_path_users": len(alternate_path_user_ids),
                "alternate_path_percent_of_parent": round(len(alternate_path_user_ids) / parent_count * 100, 1) if parent_count else 0.0,
                "actual_dropoff_users": len(actual_dropoff_user_ids),
                "actual_dropoff_percent_of_parent": round(len(actual_dropoff_user_ids) / parent_count * 100, 1) if parent_count else 0.0,
                "data_quality": "incomplete" if incomplete_transition_user_ids else "complete",
                "incomplete_transition_users": len(incomplete_transition_user_ids),
                "outgoing_users": len(outgoing_user_ids),
                "outgoing_percent_of_screen": round(len(outgoing_user_ids) / len(reached) * 100, 1) if reached else 0.0,
                "outgoing_percent_of_start": round(len(outgoing_user_ids) / start_users * 100, 1) if start_users else 0.0,
                "stopped_users": stopped_users,
                "stopped_percent_of_screen": round(stopped_users / len(reached) * 100, 1) if reached else 0.0,
                "stopped_percent_of_start": round(stopped_users / start_users * 100, 1) if start_users else 0.0,
                "terminal": screen_id in {"completion", "completion_skipped", "result_existing", "result_found", "result_notification"},
                "incoming_transitions": incoming,
                "outgoing_transitions": outgoing,
                "actions": actions,
            })
            result_screens.append(item)
            if not definition.get("branch"):
                previous_main_id = screen_id

        filter_options = {
            "devices": [row[0] for row in conn.execute(
                "SELECT DISTINCT device_type FROM analytics_events "
                "WHERE IS_STATS_USER(chel_id) = 1 ORDER BY device_type"
            ) if row[0]],
            "methods": [row[0] for row in conn.execute(
                "SELECT DISTINCT COALESCE(NULLIF(json_extract(properties,'$.method'),''),registration_method) method "
                "FROM analytics_events WHERE IS_STATS_USER(chel_id) = 1 AND event_name='registration_completed' "
                "AND COALESCE(NULLIF(json_extract(properties,'$.method'),''),registration_method) "
                "IN ('anonymous','max','telegram','result') ORDER BY method"
            ) if row[0]],
            "sources": [row[0] for row in conn.execute(
                "SELECT DISTINCT entry_source FROM analytics_sessions "
                "WHERE IS_STATS_USER(chel_id) = 1 AND entry_source<>'' ORDER BY entry_source LIMIT 100"
            ) if row[0]],
        }
    return {
        "generated_at": _now(), "period": period, "date_from": date_from, "date_to": date_to,
        "flow": flow,
        "flow_label": "Получение результатов" if flow == "result" else "Обычный путь",
        "summary": {
            "start_users": start_users,
            "screens": len(result_screens),
            "reached_completion": len(
                set().union(*(
                    screen_users.get(item["id"], set())
                    for item in result_screens if item.get("terminal")
                ))
            ),
            "unique_transitions": sum(len(users) for users in edge_users.values()),
        },
        "screens": result_screens,
        "filter_options": filter_options,
        "privacy": "Каждый пользователь и переход учитываются один раз. Повторные открытия и возвраты не увеличивают показатели; ответы анкеты и медицинские данные не передаются.",
    }


def metric2_report(
    period: str = "30", device: str = "", method: str = "", source: str = "",
    date_from: str = "", date_to: str = "", flow: str = "standard",
) -> dict:
    arguments = (
        str(period), str(device), str(method), str(source),
        str(date_from), str(date_to), str(flow),
    )
    return _cached_report(
        "metric2", arguments,
        lambda: _metric2_report_uncached(
            period, device, method, source, date_from, date_to, flow,
        ),
    )


def cleanup_old_events() -> int:
    if not settings.analytics_enabled or settings.analytics_retention_days <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=settings.analytics_retention_days)).isoformat()
    with _write_lock, connection() as conn:
        cursor = conn.execute("DELETE FROM analytics_events WHERE received_at < ?", (cutoff,))
        conn.execute(
            "DELETE FROM analytics_sessions WHERE last_seen_at < ? AND session_id NOT IN "
            "(SELECT DISTINCT session_id FROM analytics_events)", (cutoff,),
        )
        conn.commit()
        removed = max(0, int(cursor.rowcount or 0))
    if removed:
        _invalidate_report_cache()
    return removed


def delete_user_data(chel_id: str) -> dict:
    """Remove all product-analytics events and sessions for a user."""
    chel_id = str(chel_id or "").strip()
    if not settings.analytics_database_path.exists():
        return {"events": 0, "sessions": 0}
    with _write_lock, connection() as conn:
        existing_tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        events = 0
        sessions = 0
        if "analytics_events" in existing_tables:
            events = max(0, int(conn.execute(
                "DELETE FROM analytics_events WHERE chel_id = ?", (chel_id,),
            ).rowcount or 0))
        if "analytics_sessions" in existing_tables:
            sessions = max(0, int(conn.execute(
                "DELETE FROM analytics_sessions WHERE chel_id = ?", (chel_id,),
            ).rowcount or 0))
        conn.commit()
    if events or sessions:
        _invalidate_report_cache()
    return {"events": events, "sessions": sessions}

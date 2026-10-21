from copy import deepcopy
from dataclasses import asdict, dataclass


AGENT_IDS = {
    "manager", "safety", "therapist", "cardiologist", "neurologist",
    "dermatologist", "pediatrician", "psychologist", "general",
}
ROUTE_ACTIONS = {"respond", "continue", "clarify", "handoff", "human", "emergency"}
AGENT_ACTIONS = {"respond", "ask", "handoff", "human", "emergency"}
URGENCY_LEVELS = {"routine", "soon", "urgent", "emergency"}

PSYCHOLOGY_STAGES = {
    "request", "details", "duration", "impact", "resources", "expectation",
    "technique", "offer", "consent", "done",
}
PSYCHOLOGY_PRIORITIES = {"routine", "attention", "urgent"}

DEFAULT_PSYCHOLOGY_DIALOG_STATE = {
    "stage": "request",
    "category_primary": "unclear",
    "categories_secondary": [],
    "client_quote": "",
    "manifestations": {"thoughts": "", "body": "", "behavior": ""},
    "duration": "",
    "impact_score": None,
    "impact_areas": [],
    "context": "",
    "tried_before": "",
    "resources": "",
    "expectation": "",
    "techniques_used": [],
    "risk_flags": [],
    "priority": "routine",
    "communication_style": "",
    "hypothesis": "",
    "name": "",
    "age": None,
    "timezone": "",
    "preferred_time": "",
    "consent_summary": None,
    "session_offers_made": 0,
    "last_offer_message_index": None,
    "session_offer_declined": False,
    "free_session_available": True,
    "remaining_paid": 0,
    "pending_request": None,
}


def normalize_psychology_dialog_state(value: dict | None) -> dict:
    """Return the bounded, stable state used only by Vita's master-class chat."""
    result = deepcopy(DEFAULT_PSYCHOLOGY_DIALOG_STATE)
    if not isinstance(value, dict):
        return result
    stage = str(value.get("stage") or "request")
    result["stage"] = stage if stage in PSYCHOLOGY_STAGES else "request"
    for key in (
        "category_primary", "client_quote", "duration", "context", "tried_before",
        "resources", "expectation", "communication_style", "hypothesis", "name",
        "timezone", "preferred_time",
    ):
        result[key] = str(value.get(key) or "")[:1000]
    if not result["category_primary"]:
        result["category_primary"] = "unclear"
    for key in ("categories_secondary", "impact_areas"):
        items = value.get(key)
        if isinstance(items, list):
            result[key] = [str(item)[:200] for item in items[:12]]
    manifestations = value.get("manifestations")
    if isinstance(manifestations, dict):
        result["manifestations"] = {
            key: str(manifestations.get(key) or "")[:1000]
            for key in ("thoughts", "body", "behavior")
        }
    impact = value.get("impact_score")
    if impact is not None:
        try:
            result["impact_score"] = max(0, min(10, int(impact)))
        except (TypeError, ValueError):
            pass
    age = value.get("age")
    if age is not None:
        try:
            result["age"] = max(0, min(120, int(age)))
        except (TypeError, ValueError):
            pass
    techniques = value.get("techniques_used")
    if isinstance(techniques, list):
        result["techniques_used"] = [
            {
                "name": str(item.get("name") or "")[:200],
                "before": item.get("before") if isinstance(item.get("before"), int) else None,
                "after": item.get("after") if isinstance(item.get("after"), int) else None,
                "feedback": str(item.get("feedback") or "")[:500],
            }
            for item in techniques[:10] if isinstance(item, dict)
        ]
    flags = value.get("risk_flags")
    if isinstance(flags, list):
        result["risk_flags"] = [
            {
                "type": str(item.get("type") or "")[:100],
                "evidence": str(item.get("evidence") or "")[:500],
                "client_answer": str(item.get("client_answer") or "")[:300],
            }
            for item in flags[:10] if isinstance(item, dict)
        ]
    priority = str(value.get("priority") or "routine")
    result["priority"] = priority if priority in PSYCHOLOGY_PRIORITIES else "routine"
    consent = value.get("consent_summary")
    result["consent_summary"] = consent if isinstance(consent, bool) else None
    for key in ("session_offer_declined", "free_session_available"):
        if isinstance(value.get(key), bool):
            result[key] = value[key]
    for key in ("session_offers_made", "remaining_paid"):
        try:
            result[key] = max(0, int(value.get(key) or 0))
        except (TypeError, ValueError):
            pass
    last_offer = value.get("last_offer_message_index")
    if last_offer is not None:
        try:
            result["last_offer_message_index"] = max(0, int(last_offer))
        except (TypeError, ValueError):
            pass
    pending = value.get("pending_request")
    result["pending_request"] = (
        {
            "request_number": str(pending.get("request_number") or "")[:20],
            "status": str(pending.get("status") or "")[:50],
        }
        if isinstance(pending, dict) else None
    )
    return result


DEFAULT_CONTEXT = {
    "current_topic": "",
    "topic_relation": "unclear",
    "topic_history": [],
    "user_goal": "",
    "patient": {
        "age": None,
        "sex": "",
        "weight_kg": None,
        "pregnancy": "unknown",
        "conditions": [],
        "medications": [],
        "allergies": [],
    },
    "known_facts": [],
    "answered_questions": [],
    "open_questions": [],
    "red_flags_checked": [],
}


def normalize_context(value: dict | None) -> dict:
    result = deepcopy(DEFAULT_CONTEXT)
    if not isinstance(value, dict):
        return result
    for key in ("current_topic", "topic_relation", "user_goal"):
        if key in value:
            result[key] = str(value[key] or "")[:500]
    if result["topic_relation"] not in {"same", "followup", "new", "unclear"}:
        result["topic_relation"] = "unclear"
    for key in (
        "topic_history", "known_facts", "answered_questions",
        "open_questions", "red_flags_checked",
    ):
        if isinstance(value.get(key), list):
            result[key] = [str(item)[:300] for item in value[key][:20]]
    patient = value.get("patient")
    if isinstance(patient, dict):
        result["patient"].update({key: patient[key] for key in result["patient"] if key in patient})
    return result


@dataclass
class RouteDecision:
    action: str
    target_agent: str
    reason: str
    context: dict

    @classmethod
    def from_dict(cls, data: dict) -> "RouteDecision":
        required = {"action", "target_agent", "reason", "context"}
        if not required.issubset(data):
            raise ValueError("Оркестратор вернул неполное решение")
        action = data["action"]
        target = data["target_agent"]
        if action not in ROUTE_ACTIONS or target not in AGENT_IDS:
            raise ValueError("Оркестратор вернул неизвестное действие или агента")
        return cls(action=action, target_agent=target, reason=str(data["reason"])[:500], context=normalize_context(data["context"]))


@dataclass
class AgentResult:
    message: str
    next_action: str
    target_agent: str | None
    handoff_reason: str
    urgency: str
    missing_information: list[str]
    dialog_state: dict | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "AgentResult":
        required = {"message", "next_action", "target_agent", "handoff_reason", "urgency", "missing_information"}
        if not required.issubset(data):
            raise ValueError("Агент вернул неполный структурированный ответ")
        action = data["next_action"]
        target = data["target_agent"]
        urgency = data["urgency"]
        if action not in AGENT_ACTIONS or urgency not in URGENCY_LEVELS:
            raise ValueError("Агент вернул неизвестное действие или уровень срочности")
        if target is not None and target not in AGENT_IDS:
            raise ValueError("Агент запросил неизвестного специалиста")
        return cls(
            message=str(data["message"]).strip(), next_action=action, target_agent=target,
            handoff_reason=str(data["handoff_reason"])[:500], urgency=urgency,
            missing_information=[str(item)[:300] for item in data["missing_information"][:10]],
            dialog_state=(
                normalize_psychology_dialog_state(data.get("dialog_state"))
                if isinstance(data.get("dialog_state"), dict) else None
            ),
        )


@dataclass
class ChatResponse:
    conversation_id: str
    user_message: dict
    assistant_message: dict
    agent: str
    handoff_from: str | None
    handoff_reason: str
    action: str
    human_escalation: bool = False
    emergency: bool = False
    human_ticket_id: str | None = None
    human_channel: str | None = None
    human_channel_prompt: str | None = None
    context: dict | None = None
    urgency: str = "routine"
    missing_information: list[str] | None = None
    attachments: list[dict] | None = None
    council_available: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


PATIENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "age": {"type": ["integer", "null"]},
        "sex": {"type": "string"},
        "weight_kg": {"type": ["number", "null"]},
        "pregnancy": {"type": "string", "enum": ["yes", "no", "possible", "unknown", "not_applicable"]},
        "conditions": {"type": "array", "items": {"type": "string"}},
        "medications": {"type": "array", "items": {"type": "string"}},
        "allergies": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["age", "sex", "weight_kg", "pregnancy", "conditions", "medications", "allergies"],
}

CONTEXT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "current_topic": {"type": "string"},
        "topic_relation": {"type": "string", "enum": ["same", "followup", "new", "unclear"]},
        "topic_history": {"type": "array", "items": {"type": "string"}},
        "user_goal": {"type": "string"},
        "patient": PATIENT_SCHEMA,
        "known_facts": {"type": "array", "items": {"type": "string"}},
        "answered_questions": {"type": "array", "items": {"type": "string"}},
        "open_questions": {"type": "array", "items": {"type": "string"}},
        "red_flags_checked": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "current_topic", "topic_relation", "topic_history", "user_goal", "patient",
        "known_facts", "answered_questions", "open_questions", "red_flags_checked",
    ],
}

ROUTE_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": sorted(ROUTE_ACTIONS)},
        "target_agent": {"type": "string", "enum": sorted(AGENT_IDS)},
        "reason": {"type": "string"},
        "context": CONTEXT_SCHEMA,
    },
    "required": ["action", "target_agent", "reason", "context"],
}

PSYCHOLOGY_DIALOG_STATE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "stage": {"type": "string", "enum": sorted(PSYCHOLOGY_STAGES)},
        "category_primary": {"type": "string"},
        "categories_secondary": {"type": "array", "items": {"type": "string"}},
        "client_quote": {"type": "string"},
        "manifestations": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "thoughts": {"type": "string"}, "body": {"type": "string"},
                "behavior": {"type": "string"},
            },
            "required": ["thoughts", "body", "behavior"],
        },
        "duration": {"type": "string"},
        "impact_score": {"type": ["integer", "null"]},
        "impact_areas": {"type": "array", "items": {"type": "string"}},
        "context": {"type": "string"},
        "tried_before": {"type": "string"},
        "resources": {"type": "string"},
        "expectation": {"type": "string"},
        "techniques_used": {
            "type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "before": {"type": ["integer", "null"]},
                    "after": {"type": ["integer", "null"]},
                    "feedback": {"type": "string"},
                },
                "required": ["name", "before", "after", "feedback"],
            },
        },
        "risk_flags": {
            "type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "type": {"type": "string"}, "evidence": {"type": "string"},
                    "client_answer": {"type": "string"},
                },
                "required": ["type", "evidence", "client_answer"],
            },
        },
        "priority": {"type": "string", "enum": sorted(PSYCHOLOGY_PRIORITIES)},
        "communication_style": {"type": "string"},
        "hypothesis": {"type": "string"},
        "name": {"type": "string"},
        "age": {"type": ["integer", "null"]},
        "timezone": {"type": "string"},
        "preferred_time": {"type": "string"},
        "consent_summary": {"type": ["boolean", "null"]},
        "session_offers_made": {"type": "integer"},
        "last_offer_message_index": {"type": ["integer", "null"]},
        "session_offer_declined": {"type": "boolean"},
        "free_session_available": {"type": "boolean"},
        "remaining_paid": {"type": "integer"},
        "pending_request": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "request_number": {"type": "string"},
                        "status": {"type": "string"},
                    },
                    "required": ["request_number", "status"],
                },
            ],
        },
    },
    "required": list(DEFAULT_PSYCHOLOGY_DIALOG_STATE),
}


AGENT_RESULT_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "message": {"type": "string"},
        "next_action": {"type": "string", "enum": sorted(AGENT_ACTIONS)},
        "target_agent": {"type": ["string", "null"], "enum": sorted(AGENT_IDS) + [None]},
        "handoff_reason": {"type": "string"},
        "urgency": {"type": "string", "enum": sorted(URGENCY_LEVELS)},
        "missing_information": {"type": "array", "items": {"type": "string"}},
        "dialog_state": {
            "anyOf": [
                {"type": "null"},
                PSYCHOLOGY_DIALOG_STATE_SCHEMA,
            ],
        },
    },
    "required": ["message", "next_action", "target_agent", "handoff_reason", "urgency", "missing_information", "dialog_state"],
}

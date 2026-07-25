from copy import deepcopy
from dataclasses import asdict, dataclass


AGENT_IDS = {
    "manager", "safety", "therapist", "cardiologist", "neurologist",
    "dermatologist", "pediatrician", "psychologist", "general",
}
ROUTE_ACTIONS = {"respond", "continue", "clarify", "handoff", "human", "emergency"}
AGENT_ACTIONS = {"respond", "ask", "handoff", "human", "emergency"}
URGENCY_LEVELS = {"routine", "soon", "urgent", "emergency"}


DEFAULT_CONTEXT = {
    "current_topic": "",
    "topic_relation": "unclear",
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
    for key in ("known_facts", "answered_questions", "open_questions", "red_flags_checked"):
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
        "user_goal": {"type": "string"},
        "patient": PATIENT_SCHEMA,
        "known_facts": {"type": "array", "items": {"type": "string"}},
        "answered_questions": {"type": "array", "items": {"type": "string"}},
        "open_questions": {"type": "array", "items": {"type": "string"}},
        "red_flags_checked": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["current_topic", "topic_relation", "user_goal", "patient", "known_facts", "answered_questions", "open_questions", "red_flags_checked"],
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
    },
    "required": ["message", "next_action", "target_agent", "handoff_reason", "urgency", "missing_information"],
}

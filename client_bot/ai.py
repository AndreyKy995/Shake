"""ИИ-менеджер на Claude: отвечает клиенту и решает, кому из клиентов нужен владелец."""

import json
import logging
from dataclasses import dataclass

import anthropic

log = logging.getLogger(__name__)

STATUSES = ("dialog", "hot", "human", "not_target", "spam")

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {
            "type": "string",
            "description": "Ответ клиенту. Пустая строка — не отвечать (только для спама).",
        },
        "status": {"type": "string", "enum": list(STATUSES)},
        "summary": {
            "type": "string",
            "description": "2–4 предложения для владельца: кто клиент, что хочет, сроки, бюджет, договорённости.",
        },
        "client_name": {"type": "string"},
        "contact": {"type": "string", "description": "Телефон/почта, если клиент их дал, иначе пустая строка."},
    },
    "required": ["reply", "status", "summary", "client_name", "contact"],
    "additionalProperties": False,
}

SYSTEM_TEMPLATE = """Ты — менеджер по работе с клиентами. Ты переписываешься с клиентами от имени владельца бизнеса.
Твоя задача — вежливо и коротко отвечать, выяснять, что нужно клиенту, и передавать владельцу только тех, кому он действительно нужен.

<business>
{business}
</business>

Как вести диалог:
- Пиши на языке клиента, коротко (1–3 предложения), по-человечески, без канцелярита и без markdown.
- Отвечай на вопросы только по информации из <business>. Не выдумывай цены, сроки, гарантии и условия. Если ответа там нет — скажи, что уточнишь у специалиста, и поставь статус human.
- Задавай не больше одного-двух уточняющих вопросов за раз. Выясни то, что в <business> указано как важное для заявки.
- Не обещай того, что может только владелец (скидки, особые условия, точная дата).

Как выбирать status (это решение видит только владелец, клиенту о нём не говори):
- dialog — разговор продолжается, клиент ещё не готов или данных не хватает.
- hot — целевой клиент: его запрос подходит бизнесу, понятно что нужно, и он готов к следующему шагу (заказ, запись, созвон) или оставил контакт. В reply скажи, что передаёшь заявку и с ним скоро свяжутся.
- human — клиент просит живого человека, жалуется, задаёт вопрос, на который нет ответа в <business>, или ситуация нестандартная.
- not_target — запрос не подходит бизнесу (не та услуга, не тот регион, явно не тот бюджет по критериям из <business>). Вежливо объясни и попрощайся.
- spam — реклама, рассылки, боты, бессмыслица, оскорбления. reply можно оставить пустым.

summary пиши для владельца, по-русски, с конкретикой из переписки. Если что-то не выяснено — так и напиши."""


@dataclass
class Decision:
    reply: str
    status: str
    summary: str
    client_name: str
    contact: str


class AIManager:
    def __init__(self, business: str, model: str, effort: str):
        self.client = anthropic.AsyncAnthropic()
        self.system = SYSTEM_TEMPLATE.format(business=business.strip())
        self.model = model
        self.effort = effort

    async def decide(self, history: list[tuple[str, str]]) -> Decision:
        """history — [(role, text)], role: user | assistant | owner."""
        response = await self.client.beta.messages.create(
            model=self.model,
            max_tokens=4000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            cache_control={"type": "ephemeral"},
            system=self.system,
            messages=_to_messages(history),
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": DECISION_SCHEMA},
            },
        )
        if response.stop_reason == "refusal":
            log.warning("Claude declined: %s", response.stop_details)
            return Decision("", "human", "ИИ не смог ответить на это сообщение — посмотрите переписку.", "", "")
        text = next(b.text for b in response.content if b.type == "text")
        data = json.loads(text)
        return Decision(**data)


def _to_messages(history: list[tuple[str, str]]) -> list[dict]:
    """Склеивает переписку в чередование user/assistant, как требует API.

    Ответы владельца идут как реплики assistant: для клиента это тот же собеседник.
    """
    messages: list[dict] = []
    for role, text in history:
        api_role = "user" if role == "user" else "assistant"
        if messages and messages[-1]["role"] == api_role:
            messages[-1]["content"] += "\n" + text
        else:
            messages.append({"role": api_role, "content": text})
    # API требует, чтобы диалог начинался с user и заканчивался им.
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    return messages

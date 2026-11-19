"""Быстрая проверка модели на вопросе станции — до того, как выбрать её основной.

Модель получает вопрос по-русски и инструмент ``kks_card``, затем готовый
вымышленный результат инструмента. Проверяется то, без чего ИИ в LOCALAI
бесполезен:

- **tool_call** — вызвала инструмент с правильным кодом, а не выдумала ответ;
- **uses_result** — взяла число из результата инструмента;
- **cites** — поставила ссылку ``[n]`` на источник;
- **russian** — ответила по-русски.

Это не замена набору проверочных вопросов (AI-13), а отсев: модель, которая
не проходит эти четыре проверки, на вопросах станции не справится.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from ai.model import Model, ModelError

CODE = "10LAC10AP001"
QUESTION = f"Что за объект {CODE} и какое у него давление?"
SYSTEM = (
    "Ты — помощник инженеров станции. Отвечай по-русски, кратко. Сведения бери только из"
    " инструментов. После каждого утверждения ставь ссылку [n], где n — поле ref из результата."
)
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "kks_card",
            "description": "Объект по коду ККС: название и связанные записи",
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
            },
        },
    }
]
RESULT = {
    "ref": 1,
    "code": CODE,
    "name": "Питательный электронасос 1",
    "records": [{"ref": 2, "label": "ПЭН-1", "values": {"Давление, кгс/см²": 16.4}}],
}


def run(model: Model) -> dict[str, Any]:
    shown: dict[str, Any] = {"model": model.name, "available": model.available()}
    if not shown["available"]:
        return {**shown, "score": 0, "checks": {}, "error": "Модель недоступна"}
    started = time.perf_counter()
    first: float | None = None
    checks = {"tool_call": False, "uses_result": False, "cites": False, "russian": False}
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": QUESTION},
    ]
    answer = ""
    try:
        calls = []
        for chunk in model.chat(messages, TOOLS):
            first = first or time.perf_counter()
            calls.extend(chunk.calls)
            answer += chunk.text
        call = next((item for item in calls if item.name == "kks_card"), None)
        if call is not None:
            checks["tool_call"] = CODE.lower() in str(call.args.get("code", "")).lower()
            messages.append(
                {"role": "assistant", "content": answer, "tool_calls": [
                    {"id": call.id or "call_0", "name": call.name, "args": call.args}]}
            )  # fmt: skip
            messages.append({"role": "tool", "id": call.id or "call_0", "name": call.name,
                             "content": json.dumps(RESULT, ensure_ascii=False)})  # fmt: skip
            answer = "".join(chunk.text for chunk in model.chat(messages, TOOLS))
    except ModelError as error:
        return {**shown, "score": 0, "checks": checks, "error": str(error)}
    checks["uses_result"] = "16,4" in answer or "16.4" in answer
    checks["cites"] = re.search(r"\[[12]\]", answer) is not None
    letters = re.findall(r"[A-Za-zА-Яа-яЁё]", answer)
    cyrillic = [item for item in letters if re.match(r"[А-Яа-яЁё]", item)]
    checks["russian"] = bool(letters) and len(cyrillic) / len(letters) > 0.6
    return {
        **shown,
        "score": sum(checks.values()),
        "checks": checks,
        "first_token_ms": round(((first or time.perf_counter()) - started) * 1000),
        "took_ms": round((time.perf_counter() - started) * 1000),
        "answer": answer.strip()[:600],
        "error": None,
    }

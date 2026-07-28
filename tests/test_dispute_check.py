"""
Тесты разбора переписок на «забыл подтвердить» и «спор».

Ключевое свойство, которое здесь проверяется: **любая неопределённость
превращается в спор**. Ошибочный «спор» стоит одной ручной проверки,
ошибочное «подтвердить» — всей заявки в поддержку. Поэтому падение модели,
битый ответ, пропущенный заказ и незнакомый вердикт обязаны давать `dispute`.
"""

from __future__ import annotations

import pytest

from Utils.dispute_check import (ChatLine, Decision, OrderCase, Verdict, build_prompt,
                                 classify, parse_response, split)
from Utils.llm import LLMError, extract_json


def case(order_id: str = "A1", lines: list[ChatLine] | None = None) -> OrderCase:
    return OrderCase(order_id=order_id, buyer="buyer", lot="Ключ Steam",
                     age_hours=30, lines=lines or [])


class FakeLLM:
    """Двойник клиента модели: отдаёт заготовленный ответ либо падает."""

    def __init__(self, response: str = "[]", error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls = 0
        self.prompts: list[str] = []

    def complete(self, system_prompt: str, user_prompt: str, **kwargs) -> str:
        self.calls += 1
        self.prompts.append(user_prompt)
        if self.error:
            raise self.error
        return self.response


# --- Разбор ответа модели --------------------------------------------------

def test_confirm_verdict_is_parsed() -> None:
    raw = '[{"order_id": "A1", "verdict": "confirm", "reason": "товар выдан, вопросов нет"}]'
    decisions = parse_response(raw, ["A1"])

    assert len(decisions) == 1
    assert decisions[0].verdict is Verdict.CONFIRM
    assert decisions[0].reason == "товар выдан, вопросов нет"


def test_dispute_verdict_is_parsed() -> None:
    raw = '[{"order_id": "A1", "verdict": "dispute", "reason": "просит возврат"}]'
    assert parse_response(raw, ["A1"])[0].verdict is Verdict.DISPUTE


def test_hash_prefix_in_order_id_is_tolerated() -> None:
    raw = '[{"order_id": "#A1", "verdict": "confirm", "reason": "ок"}]'
    assert parse_response(raw, ["A1"])[0].is_confirm


def test_json_wrapped_in_markdown_is_parsed() -> None:
    """Модели часто оборачивают ответ в ```json-блок."""
    raw = '```json\n[{"order_id": "A1", "verdict": "confirm", "reason": "ок"}]\n```'
    assert parse_response(raw, ["A1"])[0].is_confirm


def test_json_with_surrounding_text_is_parsed() -> None:
    raw = 'Вот результат:\n[{"order_id": "A1", "verdict": "confirm", "reason": "ок"}]\nГотово.'
    assert parse_response(raw, ["A1"])[0].is_confirm


def test_single_object_instead_of_array_is_parsed() -> None:
    raw = '{"order_id": "A1", "verdict": "confirm", "reason": "ок"}'
    assert parse_response(raw, ["A1"])[0].is_confirm


# --- Неопределённость всегда даёт спор -------------------------------------

def test_unknown_verdict_becomes_dispute() -> None:
    raw = '[{"order_id": "A1", "verdict": "maybe", "reason": "не уверен"}]'
    assert parse_response(raw, ["A1"])[0].verdict is Verdict.DISPUTE


def test_missing_verdict_becomes_dispute() -> None:
    raw = '[{"order_id": "A1", "reason": "нет вердикта"}]'
    assert parse_response(raw, ["A1"])[0].verdict is Verdict.DISPUTE


def test_order_absent_from_response_becomes_dispute() -> None:
    """Модель промолчала о заказе — значит спорный, а не подтверждённый."""
    raw = '[{"order_id": "A1", "verdict": "confirm", "reason": "ок"}]'
    decisions = parse_response(raw, ["A1", "A2"])

    assert len(decisions) == 2
    by_id = {d.order_id: d for d in decisions}
    assert by_id["A1"].is_confirm
    assert by_id["A2"].verdict is Verdict.DISPUTE


def test_unparsable_response_makes_everything_dispute() -> None:
    decisions = parse_response("модель написала прозу без json", ["A1", "A2"])
    assert all(d.verdict is Verdict.DISPUTE for d in decisions)


def test_empty_response_makes_everything_dispute() -> None:
    assert all(d.verdict is Verdict.DISPUTE for d in parse_response("", ["A1"]))


def test_case_insensitive_confirm() -> None:
    raw = '[{"order_id": "A1", "verdict": "CONFIRM", "reason": "ок"}]'
    assert parse_response(raw, ["A1"])[0].is_confirm


def test_result_order_matches_request_order() -> None:
    raw = ('[{"order_id": "C", "verdict": "confirm", "reason": "1"},'
           ' {"order_id": "A", "verdict": "confirm", "reason": "2"}]')
    decisions = parse_response(raw, ["A", "B", "C"])
    assert [d.order_id for d in decisions] == ["A", "B", "C"]


# --- Поведение при сбое модели --------------------------------------------

def test_llm_failure_marks_batch_disputed() -> None:
    """Модель недоступна — заказы не должны попасть в первый список."""
    client = FakeLLM(error=LLMError("соединение отклонено"))
    decisions = classify([case("A1"), case("A2")], client=client)

    assert len(decisions) == 2
    assert all(d.verdict is Verdict.DISPUTE for d in decisions)
    assert "недоступна" in decisions[0].reason


def test_empty_input_needs_no_request() -> None:
    client = FakeLLM()
    assert classify([], client=client) == []
    assert client.calls == 0


def test_batching_splits_requests() -> None:
    client = FakeLLM(response="[]")
    classify([case(f"A{i}") for i in range(10)], client=client, batch_size=4)
    assert client.calls == 3


def test_all_orders_get_a_decision() -> None:
    client = FakeLLM(response="[]")
    decisions = classify([case(f"A{i}") for i in range(7)], client=client, batch_size=3)
    assert len(decisions) == 7


# --- Формирование запроса --------------------------------------------------

def test_prompt_contains_order_data() -> None:
    prompt = build_prompt([case("XYZ789")])
    assert "XYZ789" in prompt
    assert "Ключ Steam" in prompt


def test_prompt_marks_who_said_what() -> None:
    lines = [ChatLine("shop", "выдал ключ", is_seller=True),
             ChatLine("vasya", "спасибо", is_seller=False)]
    prompt = build_prompt([case("A1", lines)])

    assert "[ПРОДАВЕЦ] выдал ключ" in prompt
    assert "[ПОКУПАТЕЛЬ] спасибо" in prompt


def test_empty_chat_is_marked_explicitly() -> None:
    """Пустая переписка — повод для спора, модель должна её увидеть."""
    assert "(переписки нет)" in build_prompt([case("A1", [])])


def test_long_message_is_truncated() -> None:
    lines = [ChatLine("vasya", "я" * 5000, is_seller=False)]
    prompt = build_prompt([case("A1", lines)])
    assert len(prompt) < 3000
    assert "..." in prompt


def test_only_recent_messages_are_sent() -> None:
    lines = [ChatLine("vasya", f"сообщение {i}", is_seller=False) for i in range(200)]
    prompt = build_prompt([case("A1", lines)])

    assert "сообщение 199" in prompt
    assert "сообщение 0\n" not in prompt


# --- Разделение на два списка ---------------------------------------------

def test_split_separates_lists() -> None:
    decisions = [
        Decision("A", Verdict.CONFIRM, "ок"),
        Decision("B", Verdict.DISPUTE, "претензия"),
        Decision("C", Verdict.CONFIRM, "ок"),
    ]
    confirm, dispute = split(decisions)

    assert [d.order_id for d in confirm] == ["A", "C"]
    assert [d.order_id for d in dispute] == ["B"]


def test_split_of_empty_input() -> None:
    assert split([]) == ([], [])


# --- extract_json ----------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ('{"a": 1}', {"a": 1}),
    ('[1, 2]', [1, 2]),
    ('```json\n{"a": 1}\n```', {"a": 1}),
    ('текст до {"a": 1} текст после', {"a": 1}),
])
def test_extract_json_variants(raw: str, expected) -> None:
    assert extract_json(raw) == expected


def test_extract_json_raises_on_garbage() -> None:
    with pytest.raises(ValueError):
        extract_json("здесь нет никакого json")

"""
Разбор переписки по заказу: «покупатель забыл подтвердить» или «есть спор».

Форма поддержки FunPay требует разложить заказы на два списка. В первый
попадают только те, где услуга оказана и переписка **безоговорочно** читается
как «покупатель просто забыл нажать кнопку». Во второй - всё остальное:
претензии, вопросы, любая неоднозначность.

Ошибка в первом списке отклоняет заявку целиком. Отсюда главное свойство
модуля: **любая неуверенность трактуется как спор**. Ошибочно отправить заказ
во второй список стоит одной ручной проверки; ошибочно отправить в первый -
стоит всей заявки. Поэтому:

* модель получает инструкцию при сомнениях выбирать ``dispute``;
* неизвестный, отсутствующий или неразобранный вердикт становится ``dispute``;
* сбой запроса к модели переводит в ``dispute`` весь пакет, а не пропускает его.

Модуль занимается только классификацией: он не ходит в FunPay и не отправляет
ничего в поддержку.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from logging import getLogger
from typing import Any, Iterable

from Utils.llm import LLMClient, LLMError, extract_json

logger = getLogger("4FP.dispute_check")

MAX_MESSAGES_PER_CHAT = 40
"""Сколько последних сообщений чата отдавать модели."""

MAX_MESSAGE_CHARS = 400
"""Предел длины одного сообщения. Длинные обрезаются."""

BATCH_SIZE = 8
"""Сколько заказов разбирать за один запрос к модели."""


class Verdict(StrEnum):
    """Решение по заказу."""

    CONFIRM = "confirm"
    """Покупатель забыл подтвердить, спора нет. Первый список формы."""

    DISPUTE = "dispute"
    """Претензия, вопрос или неоднозначность. Второй список формы."""


@dataclass(frozen=True)
class ChatLine:
    """Одна реплика переписки в виде, пригодном для модели."""

    author: str
    text: str
    is_seller: bool


@dataclass(frozen=True)
class OrderCase:
    """Заказ вместе с перепиской, поданный на разбор."""

    order_id: str
    buyer: str
    lot: str
    age_hours: int
    lines: list[ChatLine]


@dataclass(frozen=True)
class Decision:
    """Результат разбора одного заказа."""

    order_id: str
    verdict: Verdict
    reason: str

    @property
    def is_confirm(self) -> bool:
        """Можно ли включать заказ в первый список формы."""
        return self.verdict is Verdict.CONFIRM


SYSTEM_PROMPT = """\
Ты помогаешь продавцу на площадке FunPay разобрать переписки по заказам,
которые покупатели не подтвердили.

По каждому заказу реши, к какой группе он относится:

"confirm" - услуга явно оказана, и переписка безоговорочно читается так, что
покупатель просто забыл нажать кнопку подтверждения. Никаких претензий,
вопросов без ответа, споров о качестве, просьб о возврате, жалоб на задержку.

"dispute" - всё остальное. Сюда относится любая из ситуаций:
- покупатель жалуется, спорит, недоволен;
- покупатель просит возврат или отмену;
- покупатель задал вопрос и не получил ответа;
- покупатель сообщает о проблеме с товаром;
- из переписки нельзя однозначно понять, что услуга оказана;
- переписка пустая или слишком короткая для вывода;
- есть хоть малейшая неоднозначность.

ГЛАВНОЕ ПРАВИЛО: при любом сомнении выбирай "dispute".
Ошибочный "dispute" стоит продавцу одной ручной проверки.
Ошибочный "confirm" приводит к отклонению всей заявки в поддержку.
Сомневаешься - значит "dispute".

Отвечай ТОЛЬКО валидным JSON-массивом, без пояснений и без markdown:
[{"order_id": "ABC123", "verdict": "confirm", "reason": "краткая причина"}]

Причину пиши по-русски, одной короткой фразой."""


def _format_case(case: OrderCase) -> str:
    """
    Готовит описание заказа для модели.

    :param case: заказ с перепиской.

    :return: текстовый блок.
    """
    header = (f"ЗАКАЗ {case.order_id}\n"
              f"Покупатель: {case.buyer}\n"
              f"Лот: {case.lot}\n"
              f"Ожидает подтверждения: {case.age_hours} ч\n"
              f"Переписка:")

    if not case.lines:
        return f"{header}\n(переписки нет)"

    rendered = []
    for line in case.lines[-MAX_MESSAGES_PER_CHAT:]:
        text = line.text.strip().replace("\n", " ")
        if len(text) > MAX_MESSAGE_CHARS:
            text = text[:MAX_MESSAGE_CHARS] + "..."
        role = "ПРОДАВЕЦ" if line.is_seller else "ПОКУПАТЕЛЬ"
        rendered.append(f"  [{role}] {text}")
    return f"{header}\n" + "\n".join(rendered)


def build_prompt(cases: Iterable[OrderCase]) -> str:
    """
    Собирает пользовательский запрос из пакета заказов.

    :param cases: заказы с перепиской.

    :return: текст запроса.
    """
    blocks = [_format_case(case) for case in cases]
    return ("Разбери заказы ниже и верни JSON-массив с решением по каждому.\n\n"
            + "\n\n---\n\n".join(blocks))


def parse_response(raw: str, expected_ids: Iterable[str]) -> list[Decision]:
    """
    Разбирает ответ модели в решения.

    Заказы, по которым модель не дала внятного вердикта, получают
    ``DISPUTE`` - пропустить заказ в первый список по недосмотру нельзя.

    :param raw: ответ модели.
    :param expected_ids: заказы, решение по которым ожидается.

    :return: решения по всем ожидаемым заказам.
    """
    expected = list(expected_ids)
    by_id: dict[str, Decision] = {}

    try:
        payload = extract_json(raw)
    except ValueError:
        logger.warning("Ответ модели не разобран, весь пакет уходит в спорные")
        logger.debug(f"Сырой ответ: {raw[:500]}")
        payload = []

    if isinstance(payload, dict):
        payload = [payload]

    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            order_id = str(item.get("order_id", "")).strip().lstrip("#")
            if not order_id:
                continue
            raw_verdict = str(item.get("verdict", "")).strip().lower()
            verdict = Verdict.CONFIRM if raw_verdict == Verdict.CONFIRM.value else Verdict.DISPUTE
            reason = str(item.get("reason", "")).strip() or "без пояснения"
            by_id[order_id] = Decision(order_id, verdict, reason)

    decisions = []
    for order_id in expected:
        decision = by_id.get(order_id)
        if decision is None:
            decisions.append(Decision(order_id, Verdict.DISPUTE,
                                      "модель не дала решения по заказу"))
        else:
            decisions.append(decision)
    return decisions


def classify(cases: list[OrderCase], client: LLMClient | None = None,
             batch_size: int = BATCH_SIZE) -> list[Decision]:
    """
    Разбирает заказы на «подтвердить» и «спорные».

    Заказы обрабатываются пакетами: один запрос на пакет вместо запроса
    на заказ. Сбой запроса переводит весь пакет в спорные, а не прерывает
    разбор остальных.

    :param cases: заказы с перепиской.
    :param client: клиент модели; при отсутствии создаётся из настроек.
    :param batch_size: сколько заказов в одном запросе.

    :return: решения по всем заказам, в порядке подачи.
    """
    if not cases:
        return []

    llm = client or LLMClient()
    decisions: list[Decision] = []

    for start in range(0, len(cases), batch_size):
        batch = cases[start:start + batch_size]
        batch_ids = [case.order_id for case in batch]
        try:
            raw = llm.complete(SYSTEM_PROMPT, build_prompt(batch))
        except LLMError as exc:
            logger.warning(f"Модель недоступна, пакет из {len(batch)} заказов "
                           f"помечен спорным: {exc}")
            decisions.extend(
                Decision(order_id, Verdict.DISPUTE, f"модель недоступна: {exc}")
                for order_id in batch_ids)
            continue

        decisions.extend(parse_response(raw, batch_ids))

    return decisions


def split(decisions: list[Decision]) -> tuple[list[Decision], list[Decision]]:
    """
    Делит решения на два списка формы поддержки.

    :param decisions: решения по заказам.

    :return: кортеж ``(в первый список, во второй список)``.
    """
    confirm = [decision for decision in decisions if decision.is_confirm]
    dispute = [decision for decision in decisions if not decision.is_confirm]
    return confirm, dispute

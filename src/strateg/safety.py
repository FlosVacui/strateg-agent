"""Проверки текста снимка до записи (ADR-0011): контакты, закрытые данные, обращения к ИИ.

- Почта и телефоны заменяются метками. Номера 8-800 — телефоны организаций, их не трогаем.
- Признаки закрытых данных (гриф, «для служебного пользования», «коммерческая тайна») — только пометка.
- Текст, похожий на обращение к ИИ, — пометка «подозрительный». Это сигнал человеку, а не защита.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

EMAIL_LABEL = "[почта]"
PHONE_LABEL = "[телефон]"

_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONES = (
    # +7 / 8 / «7 (» и далее 10 цифр с пробелами, дефисами, скобками
    re.compile(r"(?<![\w+])(?:\+7|8|7(?=\s*\())[\s\-]*\(?\d{3}\)?[\s\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)"),
    # международный номер: плюс, код страны, не менее 9 цифр
    re.compile(r"(?<![\w+])\+(?!7)\d(?:[\s\-()]*\d){8,13}(?!\d)"),
    # (495) 123-45-67 без кода страны
    re.compile(r"(?<![\w+])\(\d{3}\)[\s\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)"),
)

_CLOSED = {
    # «гриф секретности», «гриф ограниченного доступа», «гриф «Секретно»», «гриф: секретно», «с грифом»;
    # «грифель» и гриф штанги сюда не попадают
    "гриф": re.compile(
        r"\bгриф\s+(?:секретност\w*|ограничен\w*|[«\"“„])|\bс\s+грифом\b|\bгриф:\s*\w+",
        re.IGNORECASE,
    ),
    "для служебного пользования": re.compile(r"для\s+служебного\s+пользования|\bДСП\b"),
    "коммерческая тайна": re.compile(r"коммерческ\w+\s+тайн\w+", re.IGNORECASE),
}

# (признак для сообщения, шаблон). В сообщения попадает только название признака, не сам текст.
_INSTRUCTIONS = (
    (
        "игнорировать прежние указания",
        re.compile(
            r"игнорируй\w*\s+(?:все\s+|любые\s+)?(?:предыдущ\w+|прежн\w+|прошл\w+|выше)\s+(?:инструкци\w+|указани\w+|команд\w+)"
            r"|забудь\w*\s+(?:все\s+)?(?:предыдущ\w+\s+|прежн\w+\s+)?(?:инструкци\w+|указани\w+)"
            r"|ignore\s+(?:all\s+|any\s+)?(?:the\s+)?(?:previous|prior|above|earlier)\s+(?:instructions|prompts?|rules)"
            r"|disregard\s+(?:all\s+|any\s+)?(?:the\s+)?(?:previous|prior|above)\s+(?:instructions|rules)",
            re.IGNORECASE,
        ),
    ),
    (
        "обращение к ИИ или ассистенту",
        re.compile(
            r"если\s+ты\s+(?:—\s+|-\s+)?(?:ии|ai|нейросет\w+|языков\w+\s+модел\w+|ассистент\w*|бот)\b"
            r"|(?:ии|ai|llm)[\s-]*(?:агент\w*|ассистент\w*|модел\w+)[,:]\s"
            r"|(?:уважаем\w+|dear)\s+(?:ии|ai|llm|ассистент|assistant|нейросет\w+)"
            r"|if\s+you\s+are\s+(?:an?\s+)?(?:ai|llm|language\s+model|assistant|chatbot)\b"
            r"|(?:note|message|instructions?)\s+(?:to|for)\s+(?:ai|llm|assistants?|language\s+models?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "смена роли или системная инструкция",
        re.compile(
            r"системн\w+\s+(?:промпт|инструкци\w+|сообщени\w+)"
            r"|system\s+(?:prompt|message|instructions?)"
            r"|\byou\s+are\s+now\b|\bnew\s+instructions?\b|новые\s+инструкции"
            r"|<\s*/?\s*system\s*>",
            re.IGNORECASE,
        ),
    ),
    (
        "требование скрыть от пользователя или выполнить действие",
        re.compile(
            r"не\s+(?:сообщай|говори|показывай)\s+(?:об\s+этом\s+)?пользовател\w+"
            r"|do\s+not\s+(?:tell|inform|reveal\s+to)\s+the\s+user"
            r"|(?:выполни|запусти)\s+(?:следующ\w+\s+)?(?:команду|код|скрипт)"
            r"|(?:execute|run)\s+the\s+following\s+(?:command|code|script)"
            r"|(?:отправь|передай|перешли)\s+(?:все\s+)?(?:данные|файлы|материалы|ключи|пароли)\s+(?:проекта\s+)?на\b"
            r"|(?:send|forward|upload)\s+(?:all\s+)?(?:the\s+)?(?:data|files|keys|passwords)\s+to\b",
            re.IGNORECASE,
        ),
    ),
)

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass
class SafetyReport:
    text: str
    anonymized: list[str] = field(default_factory=list)  # значения Anonymized: «телефоны», «адреса почты»
    closed_markers: list[str] = field(default_factory=list)
    instruction_signs: list[str] = field(default_factory=list)


def clean_text(text: str) -> str:
    """Окончания LF, без управляющих символов и пробелов в конце строк, пустые строки подряд сжаты."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL.sub("", text)
    lines = [line.rstrip() for line in text.split("\n")]
    out: list[str] = []
    for line in lines:
        if line == "" and out and out[-1] == "":
            continue
        out.append(line)
    return "\n".join(out).strip("\n") + "\n"


def _is_org_phone(match: str) -> bool:
    digits = re.sub(r"\D", "", match)
    return digits.startswith(("7800", "8800"))


def mask_contacts(text: str) -> tuple[str, list[str]]:
    """Заменяет адреса почты и телефоны метками; номера 8-800 не трогает."""
    found: list[str] = []
    text, n = _EMAIL.subn(EMAIL_LABEL, text)
    if n:
        found.append("адреса почты")
    hit = False

    def repl(m: re.Match[str]) -> str:
        nonlocal hit
        if _is_org_phone(m.group()):
            return m.group()
        hit = True
        return PHONE_LABEL

    for pattern in _PHONES:
        text = pattern.sub(repl, text)
    if hit:
        found.append("телефоны")
    return text, found


def inspect_text(text: str) -> SafetyReport:
    """Все проверки до записи снимка: текст после замены контактов и найденные признаки."""
    text = clean_text(text)
    masked, anonymized = mask_contacts(text)
    return SafetyReport(
        text=masked,
        anonymized=anonymized,
        closed_markers=[name for name, pattern in _CLOSED.items() if pattern.search(masked)],
        instruction_signs=[name for name, pattern in _INSTRUCTIONS if pattern.search(masked)],
    )

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
    # «Для служебного пользования» в начале предложения тоже признак; ДСП — только заглавными
    "для служебного пользования": re.compile(r"(?i:для\s+служебного\s+пользования)|\bДСП\b"),
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


# ---------------------------------------------------------------------------
# Материалы человека (`strateg add`): персональные данные с контрольными суммами, имена
# ---------------------------------------------------------------------------

INN_LABEL = "[ИНН]"
SNILS_LABEL = "[СНИЛС]"
CARD_LABEL = "[карта]"
_NUMBERS = "номера документов и карт"

_CARD_GROUPED = re.compile(r"(?<![\w-])\d{4}(?:[ -]\d{4}){2,3}(?:[ -]\d{1,3})?(?![\w-])")
_CARD_PLAIN = re.compile(r"(?<![\w-])\d{13,19}(?![\w-])")
_SNILS = re.compile(r"(?<![\w-])(?:\d{3}[ -]\d{3}[ -]\d{3}[ -]\d{2}|\d{11})(?![\w-])")
_INN = re.compile(r"(?<![\w-])\d{10}(?:\d{2})?(?![\w-])")
_NAME_WORD = re.compile(r"[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?")
_NAME_GRAMMEMES = frozenset({"Name", "Surn", "Patr"})


@dataclass(frozen=True)
class Replacement:
    """Одна замена в обезличенной копии: что заменено и в какой строке. Само значение не хранится."""

    label: str  # «[телефон]», «[почта]», «[ИНН]», «[СНИЛС]», «[карта]»
    line: int


def luhn_ok(digits: str) -> bool:
    """Контрольная сумма Луна для номера карты."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def snils_ok(digits: str) -> bool:
    """СНИЛС: 11 цифр, последние две — контрольное число по первым девяти."""
    if len(digits) != 11:
        return False
    total = sum(int(d) * w for d, w in zip(digits[:9], range(9, 0, -1), strict=True))
    control = total if total < 100 else total % 101
    if control in (100, 101):
        control = 0
    return control == int(digits[9:])


def inn_ok(digits: str) -> bool:
    """ИНН из 10 (организация) или 12 (человек) цифр с контрольными суммами."""

    def check(body: str, weights: tuple[int, ...]) -> int:
        return sum(int(d) * w for d, w in zip(body, weights, strict=True)) % 11 % 10

    w10 = (2, 4, 10, 3, 5, 9, 4, 6, 8)
    w11 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
    w12 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
    if len(digits) == 10:
        return check(digits[:9], w10) == int(digits[9])
    if len(digits) == 12:
        return check(digits[:10], w11) == int(digits[10]) and check(digits[:11], w12) == int(digits[11])
    return False


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def mask_personal(text: str) -> tuple[str, list[Replacement], list[str]]:
    """Обезличивание материала: почта, телефоны, ИНН человека, СНИЛС, номера карт.

    Возвращает (текст с метками, замены с номером строки, категории для реестра). Номера 8-800 и ИНН из
    10 цифр (организация) не трогаются, но и не принимаются за другой номер. Строки не склеиваются:
    номер строки в списке замен — номер строки текста.
    """
    taken: list[tuple[int, int]] = []
    found: list[tuple[int, int, str, str]] = []  # начало, конец, метка, категория

    def free(a: int, b: int) -> bool:
        return all(b <= x or a >= y for x, y in taken)

    def take(a: int, b: int, label: str | None, category: str | None) -> None:
        taken.append((a, b))
        if label and category:
            found.append((a, b, label, category))

    for m in _EMAIL.finditer(text):
        take(m.start(), m.end(), EMAIL_LABEL, "адреса почты")
    for pattern in _PHONES:
        for m in pattern.finditer(text):
            if free(m.start(), m.end()):
                if _is_org_phone(m.group()):
                    take(m.start(), m.end(), None, None)
                else:
                    take(m.start(), m.end(), PHONE_LABEL, "телефоны")
    for pattern in (_CARD_GROUPED, _CARD_PLAIN):
        for m in pattern.finditer(text):
            digits = _digits(m.group())
            if 13 <= len(digits) <= 19 and luhn_ok(digits) and free(m.start(), m.end()):
                take(m.start(), m.end(), CARD_LABEL, _NUMBERS)
    for m in _SNILS.finditer(text):
        if snils_ok(_digits(m.group())) and free(m.start(), m.end()):
            take(m.start(), m.end(), SNILS_LABEL, _NUMBERS)
    for m in _INN.finditer(text):
        digits = m.group()
        if free(m.start(), m.end()) and inn_ok(digits):
            if len(digits) == 12:
                take(m.start(), m.end(), INN_LABEL, _NUMBERS)
            else:
                take(m.start(), m.end(), None, None)  # ИНН организации: не персональные данные

    found.sort()
    out: list[str] = []
    replacements: list[Replacement] = []
    categories: list[str] = []
    pos = 0
    for a, b, label, category in found:
        out.append(text[pos:a])
        out.append(label)
        replacements.append(Replacement(label, text.count("\n", 0, a) + 1))
        if category not in categories:
            categories.append(category)
        pos = b
    out.append(text[pos:])
    return "".join(out), replacements, categories


_morph = None


def _analyzer():
    global _morph
    if _morph is None:
        import pymorphy3

        _morph = pymorphy3.MorphAnalyzer()
    return _morph


def find_names(text: str, limit: int = 50) -> list[tuple[str, int]]:
    """Слова, которые словарь морфологии признаёт именем, фамилией или отчеством: (слово, строка).

    Это предупреждение человеку, а не замена: словарь даёт ложные имена («Роза» в «Роза ветров»,
    «Вера», «Надежда»), а имена публичных лиц в новостях и отчётах маскировать не нужно.
    """
    morph = _analyzer()
    seen: set[str] = set()
    out: list[tuple[str, int]] = []
    for m in _NAME_WORD.finditer(text):
        word = m.group()
        if word.lower() in seen:
            continue
        if any(
            p.score >= 0.3 and _NAME_GRAMMEMES & set(p.tag.grammemes)
            for part in word.split("-")
            for p in morph.parse(part)[:3]
        ):
            seen.add(word.lower())
            out.append((word, text.count("\n", 0, m.start()) + 1))
            if len(out) >= limit:
                break
    return out


@dataclass
class MaterialReport:
    """Итог проверки текста материала до модели."""

    text: str  # текст с заменами: его видит модель
    replacements: list[Replacement] = field(default_factory=list)
    anonymized: list[str] = field(default_factory=list)
    closed_markers: list[str] = field(default_factory=list)
    instruction_signs: list[str] = field(default_factory=list)
    names: list[tuple[str, int]] = field(default_factory=list)


def inspect_material(text: str) -> MaterialReport:
    """Проверки текста материала: персональные данные, закрытые данные, обращения к ИИ, имена.

    Закрытые данные и обращения к ИИ — те же признаки, что у `fetch`; ищутся уже в обезличенном тексте.
    """
    cleaned = clean_text(text)
    masked, replacements, categories = mask_personal(cleaned)
    return MaterialReport(
        text=masked,
        replacements=replacements,
        anonymized=categories,
        closed_markers=[name for name, pattern in _CLOSED.items() if pattern.search(masked)],
        instruction_signs=[name for name, pattern in _INSTRUCTIONS if pattern.search(masked)],
        names=find_names(masked),
    )

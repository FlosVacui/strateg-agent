"""Проверки текста снимка до записи (ADR-0011): контакты, закрытые данные, обращения к ИИ.

- Почта и телефоны заменяются метками. Номера 8-800 — телефоны организаций, их не трогаем.
- Признаки закрытых данных (гриф, «для служебного пользования», «коммерческая тайна») — только пометка.
- Строки, похожие на ключи (общие шаблоны с упаковкой, `keys`), заменяются меткой `[ключ]`.
- Текст, похожий на обращение к ИИ (`instructions`), — пометка «подозрительный». Это сигнал человеку, а не
  защита: обращение, записанное иначе, пройдёт.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field

from strateg import keys
from strateg.instructions import find_signs

EMAIL_LABEL = "[почта]"
PHONE_LABEL = "[телефон]"
KEY_LABEL = keys.KEY_LABEL
KEYS_CATEGORY = "ключи"
AUTHORS_CATEGORY = "авторы отзывов"

_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# разделитель в номере: пробел, дефис, длинное и неразрывное тире, минус
_SEP = r"[\s\-\u2010-\u2015\u2212]"
_PHONES = (
    # +7 / 8 / голая «7» перед кодом 3xx, 4xx, 8xx, 9xx и далее 10 цифр с пробелами, тире, скобками
    re.compile(
        rf"(?<![\w+])(?:\+7|8|7(?={_SEP}*\(?[3489]\d{{2}})){_SEP}*\(?\d{{3}}\)?{_SEP}*\d{{3}}{_SEP}*\d{{2}}{_SEP}*\d{{2}}(?!\d)"
    ),
    # международный номер: плюс, код страны, не менее 9 цифр
    re.compile(r"(?<![\w+])\+(?!7)\d(?:[\s\-\u2010-\u2015\u2212()]*\d){8,13}(?!\d)"),
    # (495) 123-45-67 без кода страны
    re.compile(rf"(?<![\w+])\(\d{{3}}\){_SEP}*\d{{3}}{_SEP}*\d{{2}}{_SEP}*\d{{2}}(?!\d)"),
)

# --- «Коммерческая тайна» --------------------------------------------------------------------------------
_TRADE_SECRET_WORDS = re.compile(r"коммерческ\w*\s+тайн\w*", re.IGNORECASE)
# Признаки пересказа: предложение с ними — упоминание в новости или статье, а не пометка документа.
# Только слова, которых в пометке не бывает: ссылка на чужие слова, глаголы сообщения в прошедшем времени,
# «не раскрывает» в личной форме (пометка пишет «не раскрывать»), заголовки-вопросы, определения.
# Слова пометок («содержит», «составляет», «охраняется», «защищено», «режим», «закон о …», «не раскрывать»)
# сюда не входят: неизвестная форма — метка.
_NEWS_SIGNS = re.compile(
    "|".join(
        (
            r"ссыла\w*с[ья]\s+на\b|\bсосла\w+",
            r"не\s+позволя(?:ет|ют)\b|\bне\s+мо(?:жем|гу)\b|по\s+данным",
            r"не\s+раскрыва(?:ет|ют|л[аио]?)\b|не\s+раскрыл[аи]?\b|не\s+публику(?:ет|ют)\b",
            r"отказал\w*\s+(?:\w+\s+){0,3}(?:назва\w+|раскры\w+|сообщ\w+|комментир\w+)",
            r"получил\w*\s+отказ|отказал\w*\s+от\s+комментари\w+",
            r"что\s+такое|как\s+защит\w+|что\s+нужно\s+знать|\bпочему\b",
            r"коммерческ\w+\s+тайн\w+\s*[—–-]\s*это\b",
            r"по\s+мнению|по\s+словам|журналист\w*|эксперт\w*|пресс-служб\w*",
            r"\b(?:ответил|сообщил|заявил|пояснил|сказал|признал)[аи]?\b",
        )
    ),
    re.IGNORECASE,
)
# Предложение — отрезок между «.», «!», «?» и переводом строки; перенос перед строчной буквой — продолжение.
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+|\n+(?![ \t]*[а-яёa-z])")


class _TradeSecret:
    """Признак «коммерческая тайна»: слова в предложении без признака новости (`.search`, как у шаблона)."""

    def search(self, text: str) -> bool:
        breaks = list(_SENTENCE_BREAK.finditer(text))
        starts = [0] + [m.end() for m in breaks]
        ends = [m.start() for m in breaks] + [len(text)]
        for hit in _TRADE_SECRET_WORDS.finditer(text):
            first = bisect.bisect_right(starts, hit.start()) - 1
            last = bisect.bisect_left(ends, hit.end())
            if not _NEWS_SIGNS.search(text[starts[first] : ends[last]]):
                return True
        return False


_CLOSED = {
    # «гриф секретности», «гриф ограниченного доступа», «гриф «Секретно»», «гриф: секретно», «с грифом»;
    # «грифель» и гриф штанги сюда не попадают
    "гриф": re.compile(
        r"\bгриф\s+(?:секретност\w*|ограничен\w*|[«\"“„])|\bс\s+грифом\b|\bгриф:\s*\w+",
        re.IGNORECASE,
    ),
    # «Для служебного пользования» в начале предложения тоже признак; ДСП — только заглавными
    "для служебного пользования": re.compile(r"(?i:для\s+служебного\s+пользования)|\bДСП\b"),
    # «Коммерческая тайна» метится всегда, кроме предложения с явным признаком новости или рассуждения о
    # законе (`_NEWS_SIGNS`): формы пометки не перечислить, неизвестная форма — метка (лишний вопрос человеку
    # дешевле утечки).
    "коммерческая тайна": _TradeSecret(),
}

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass
class SafetyReport:
    text: str
    anonymized: list[str] = field(default_factory=list)  # значения Anonymized: «телефоны», «адреса почты»
    closed_markers: list[str] = field(default_factory=list)
    instruction_signs: list[str] = field(default_factory=list)
    key_kinds: dict[str, int] = field(default_factory=dict)  # вид ключа -> число замен (значений нет)


def count_kinds(hits: list[keys.KeyHit]) -> dict[str, int]:
    out: dict[str, int] = {}
    for h in hits:
        out[h.kind] = out.get(h.kind, 0) + 1
    return out


def snapshots_before_masking(sources) -> dict[str, str]:
    """Прежние снимки, снятые до замены авторов отзывов: {прежний И-n: новый И-n, который его перепроверил}.

    Признак: у нового снимка (`refreshes` — на прежний) в `anonymized` есть «авторы отзывов», а у прежнего
    нет. Имена и ники в таком прежнем снимке остались; он лежит в `sources/` и уходит в архив.
    """
    by_id = {s.id: s for s in sources}
    out: dict[str, str] = {}
    for s in sources:
        old = by_id.get(s.refreshes) if getattr(s, "refreshes", None) else None
        if (
            old is not None
            and old.text_path
            and AUTHORS_CATEGORY in s.anonymized
            and AUTHORS_CATEGORY not in old.anonymized
        ):
            out.setdefault(old.id, s.id)
    return out


def stale_snapshot_note(old: str, new: str) -> str:
    """Фраза для `fetch --refresh`, `status` и `pack`: прежний снимок остался, в нём могут быть имена."""
    return (
        f"прежний снимок {old} снят до замены авторов отзывов и остался в sources/: в нём могут быть имена "
        f"и ники, "
        f"он попадёт в архив `pack`; новый снимок {new} с метками. Если утверждений на {old} нет, он не нужен"
    )


def found_what(categories: list[str]) -> str:
    """Что найдено, по-русски: персональные данные, строки, похожие на ключи, или и то и другое."""
    pii = [c for c in categories if c != KEYS_CATEGORY]
    if pii and KEYS_CATEGORY in categories:
        return "персональные данные и строки, похожие на ключи"
    return "строки, похожие на ключи" if KEYS_CATEGORY in categories else "персональные данные"


def keys_message(kinds: dict[str, int]) -> str:
    """Сообщение о заменённых ключах: вид и число замен, без значений."""
    total = sum(kinds.values())
    listing = "; ".join(f"{kind} — {n}" for kind, n in kinds.items())
    return (
        f"строки, похожие на ключи, заменены меткой {KEY_LABEL} (замен: {total}): {listing}. "
        "Значения не показываются; если ключ настоящий, перевыпустите его: он лежал в открытом тексте"
    )


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
    text, key_hits = keys.mask_keys(text)  # ключи первыми: цифры в ключе не должны сойти за телефон
    text, numbers = mask_page_numbers(text)  # до телефонов: группы цифр карты не должны сойти за номер
    masked, anonymized = mask_contacts(text)
    if numbers:
        anonymized.append(_NUMBERS)
    if key_hits:
        anonymized.append(KEYS_CATEGORY)
    return SafetyReport(
        text=masked,
        anonymized=anonymized,
        closed_markers=[name for name, pattern in _CLOSED.items() if pattern.search(masked)],
        instruction_signs=find_signs(masked),
        key_kinds=count_kinds(key_hits),
    )


# ---------------------------------------------------------------------------
# Материалы человека (`strateg add`): персональные данные с контрольными суммами, имена
# ---------------------------------------------------------------------------

INN_LABEL = "[ИНН]"
SNILS_LABEL = "[СНИЛС]"
CARD_LABEL = "[карта]"
_NUMBERS = "номера документов и карт"

_CARD_GROUPED = re.compile(r"(?<![\w-])\d{4}(?:[ -]\d{4}){2,3}(?:[ -]\d{1,3})?(?![\w-])")
# без разделителей — только номера платёжных систем: первая цифра 2–6 и от 14 цифр; тринадцать цифр — штрихкод
# EAN-13 (сумма Луна подходит у каждого десятого), карты в 13 цифр практически не выпускаются
_CARD_PLAIN = re.compile(r"(?<![\w-])[2-6]\d{13,18}(?![\w-])")
_SNILS = re.compile(r"(?<![\w-])(?:\d{3}[ -]\d{3}[ -]\d{3}[ -]\d{2}|\d{11})(?![\w-])")
_INN = re.compile(r"(?<![\w-])\d{10}(?:\d{2})?(?![\w-])")
_NAME_WORD = re.compile(r"[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?")
_NAME_GRAMMEMES = frozenset({"Name", "Surn", "Patr"})


@dataclass(frozen=True)
class Replacement:
    """Одна замена в обезличенной копии: что заменено и в какой строке. Само значение не хранится."""

    label: str  # «[телефон]», «[почта]», «[ИНН]», «[СНИЛС]», «[карта]», «[ключ]»
    line: int
    kind: str = ""  # для ключей — вид («токен GitHub …»); самого значения нет


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


class _Taken:
    """Занятые отрезки текста: упорядоченные непересекающиеся, проверка свободы — бинарным поиском.

    Новые отрезки копятся отдельно и вливаются в основной список один раз после прохода шаблона (`commit`):
    вставка в середину списка на каждый отрезок растила бы время квадратом от числа замен.
    Отрезки одного прохода не пересекаются; свободу проверяют по тому, что занято до прохода.
    """

    def __init__(self) -> None:
        self.starts: list[int] = []
        self.ends: list[int] = []
        self._new: list[tuple[int, int]] = []

    def free(self, a: int, b: int) -> bool:
        i = bisect.bisect_right(self.starts, a)
        if i > 0 and self.ends[i - 1] > a:
            return False
        return not (i < len(self.starts) and self.starts[i] < b)

    def add(self, a: int, b: int) -> None:
        self._new.append((a, b))

    def commit(self) -> None:
        if not self._new:
            return
        merged = sorted([*zip(self.starts, self.ends, strict=True), *self._new])  # две упорядоченные части
        self.starts = [x for x, _ in merged]
        self.ends = [y for _, y in merged]
        self._new = []


def _line_of(newlines: list[int], pos: int) -> int:
    """Номер строки (с единицы) для позиции в тексте; список переводов строк считается один раз."""
    return bisect.bisect_left(newlines, pos) + 1


def mask_personal(text: str) -> tuple[str, list[Replacement], list[str]]:
    """Обезличивание материала: ключи, почта, телефоны, ИНН человека, СНИЛС, номера карт.

    Возвращает (текст с метками, замены с номером строки, категории для реестра). Номера 8-800 и ИНН из
    10 цифр (организация) не трогаются, но и не принимаются за другой номер. Строки не склеиваются:
    номер строки в списке замен — номер строки текста. Время растёт почти линейно от числа замен.
    """
    taken = _Taken()
    found: list[tuple[int, int, str, str, str]] = []  # начало, конец, метка, категория, вид ключа

    def take(a: int, b: int, label: str | None, category: str | None, kind: str = "") -> None:
        taken.add(a, b)
        if label and category:
            found.append((a, b, label, category, kind))

    for hit in keys.find_keys(text):  # ключи первыми: их цифры не должны сойти за телефон или номер
        take(hit.start, hit.end, KEY_LABEL, KEYS_CATEGORY, hit.kind)
    taken.commit()
    for m in _EMAIL.finditer(text):
        if taken.free(m.start(), m.end()):
            take(m.start(), m.end(), EMAIL_LABEL, "адреса почты")
    taken.commit()
    for pattern in _PHONES:
        for m in pattern.finditer(text):
            if taken.free(m.start(), m.end()):
                if _is_org_phone(m.group()):
                    take(m.start(), m.end(), None, None)
                else:
                    take(m.start(), m.end(), PHONE_LABEL, "телефоны")
        taken.commit()
    for pattern in (_CARD_GROUPED, _CARD_PLAIN):
        for m in pattern.finditer(text):
            digits = _digits(m.group())
            if 13 <= len(digits) <= 19 and luhn_ok(digits) and taken.free(m.start(), m.end()):
                take(m.start(), m.end(), CARD_LABEL, _NUMBERS)
        taken.commit()
    for m in _SNILS.finditer(text):
        if snils_ok(_digits(m.group())) and taken.free(m.start(), m.end()):
            take(m.start(), m.end(), SNILS_LABEL, _NUMBERS)
    taken.commit()
    for m in _INN.finditer(text):
        digits = m.group()
        if taken.free(m.start(), m.end()) and inn_ok(digits):
            if len(digits) == 12:
                take(m.start(), m.end(), INN_LABEL, _NUMBERS)
            else:
                take(m.start(), m.end(), None, None)  # ИНН организации: не персональные данные

    found.sort()
    newlines = [i for i, ch in enumerate(text) if ch == "\n"] if found else []
    out: list[str] = []
    replacements: list[Replacement] = []
    categories: list[str] = []
    pos = 0
    for a, b, label, category, kind in found:
        out.append(text[pos:a])
        out.append(label)
        replacements.append(Replacement(label, _line_of(newlines, a), kind))
        if category not in categories:
            categories.append(category)
        pos = b
    out.append(text[pos:])
    return "".join(out), replacements, categories


# На странице заменяются только узкие формы с разделителями: СНИЛС «ddd-ddd-ddd dd» (с контрольным числом)
# и номер карты группами (с суммой Луна). ИНН и цифры подряд — публичные сведения реестров, номера заказов и
# артикулы: не трогаются.
_SNILS_SEPARATED = re.compile(r"(?<![\w-])\d{3}[ -]\d{3}[ -]\d{3}[ -]\d{2}(?![\w-])")


def mask_page_numbers(text: str) -> tuple[str, int]:
    """СНИЛС с разделителями и номер карты группами в тексте страницы; возвращает текст и число замен."""
    spans: list[tuple[int, int, str]] = []
    for m in _CARD_GROUPED.finditer(text):
        digits = _digits(m.group())
        if 13 <= len(digits) <= 19 and luhn_ok(digits):
            spans.append((m.start(), m.end(), CARD_LABEL))
    for m in _SNILS_SEPARATED.finditer(text):
        if snils_ok(_digits(m.group())):
            spans.append((m.start(), m.end(), SNILS_LABEL))
    if not spans:
        return text, 0
    spans.sort()
    out: list[str] = []
    pos = 0
    count = 0
    for a, b, label in spans:
        if a < pos:
            continue
        out.append(text[pos:a])
        out.append(label)
        pos = b
        count += 1
    out.append(text[pos:])
    return "".join(out), count


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

    @property
    def key_kinds(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.replacements:
            if r.label == KEY_LABEL:
                out[r.kind] = out.get(r.kind, 0) + 1
        return out


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
        instruction_signs=find_signs(masked),
        names=find_names(masked),
    )

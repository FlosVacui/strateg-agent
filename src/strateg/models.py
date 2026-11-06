"""Модели файлов проекта `projects/<id>/` — источник правды для форматов.

JSON Schema генерирует `scripts/gen_schemas.py`. Схема описывает форму файла; правила, которые
схемой не выразить (связи между записями и файлами, зависимости полей), держит код: проверки
внутри одной записи — валидаторы моделей, проверки между записями и файлами —
`strateg.project_check.check_project`.

Ключи — английские, snake_case; значения закрытых списков — русские, как в описании модели данных.
Все модели отвергают неизвестные поля. Строки, которые YAML 1.1 читает как другой тип (`no`, `1.10`,
даты без кавычек, шаг `3`), в файле берутся в кавычки: модели принимают то, что даёт `yaml.safe_load`,
и строгий тип поля сообщает об этом по-русски.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from functools import lru_cache
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    Strict,
    StrictBool,
    model_validator,
)

SCHEMA_VERSION = 1
MIN_FUZZY_QUOTE_LEN = 40  # цитата короче проходит проверку только точным вхождением
FUZZY_THRESHOLD = 93  # стартовый порог нечёткого сходства, процентов
WEB_DIR = "sources"  # снимки веба
INPUTS_DIR = "inputs"  # материалы человека и извлечённый из них текст

# ---------------------------------------------------------------------------
# Общие типы
# ---------------------------------------------------------------------------

# Элемент пути: не пустой, не «.» и «..», без пробелов по краям, не кончается точкой.
_SEGMENT = r"(?:[^/\\\s\x00.]|[^/\\\s\x00][^/\\\x00]*[^/\\\s\x00.])"
_PATH_PATTERN = rf"^(?![A-Za-z]:){_SEGMENT}(?:/{_SEGMENT})*$"


def _check_rel_path(value: str) -> str:
    """Путь внутри проекта: только относительный, через `/`, в одном каноническом виде."""
    if not value.strip():
        raise ValueError("путь пустой")
    if value.startswith("\\\\"):
        raise ValueError("сетевой путь (\\\\сервер\\...) недопустим: путь должен быть относительным")
    if re.match(r"^[A-Za-z]:", value):
        raise ValueError("абсолютный путь с буквой диска недопустим: путь должен быть относительным")
    if value.startswith("/"):
        raise ValueError("абсолютный путь (начинается с /) недопустим: путь должен быть относительным")
    if "\\" in value:
        raise ValueError("в пути обратная косая черта: пишите пути через /")
    if "\x00" in value:
        raise ValueError("в пути недопустимый символ")
    parts = value.split("/")
    if ".." in parts:
        raise ValueError("путь выходит за папку проекта через ..")
    if "" in parts:
        raise ValueError("в пути пустой элемент: двойной / или / в конце; один файл — одна запись пути")
    if "." in parts:
        raise ValueError("в пути элемент «.»: уберите его, один файл — одна запись пути")
    for part in parts:
        if part != part.strip() or part.endswith("."):
            raise ValueError(
                f"элемент пути «{part}» начинается или кончается пробелом либо кончается точкой: "
                "один файл — одна запись пути"
            )
    return value


def _check_not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("строка пустая")
    return value


RelPath = Annotated[
    str,
    AfterValidator(_check_rel_path),
    Field(min_length=1, json_schema_extra={"pattern": _PATH_PATTERN}),
]
Text = Annotated[str, AfterValidator(_check_not_blank), Field(min_length=1)]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
SourceId = Annotated[str, Field(pattern=r"^И-[1-9][0-9]*$")]
ClaimId = Annotated[str, Field(pattern=r"^У-[1-9][0-9]*$")]
ProjectId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")]
Url = Annotated[str, Field(pattern=r"^https?://[^\s]+$")]
Extractor = Annotated[str, Field(pattern=r"^[^\s]+ [0-9][^\s]*$")]


def show(value: Any) -> str:
    """Значение для сообщения об ошибке: без имён типов Python."""
    if value is None:
        return "пусто"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "список"
    if isinstance(value, dict):
        return "набор полей"
    return str(value)


def kind_of(value: Any) -> str:
    """Название вида значения по-русски, для «ожидается …, а получено …»."""
    if value is None:
        return "пусто"
    if isinstance(value, bool):
        return "логическое значение"
    if isinstance(value, int):
        return "целое число"
    if isinstance(value, float):
        return "дробное число"
    if isinstance(value, str):
        return "строка"
    if isinstance(value, list):
        return "список"
    if isinstance(value, dict):
        return "набор полей"
    if isinstance(value, dt.date):
        return "дата"
    return "значение другого вида"


_TIME_START = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


def _check_time_input(value: Any) -> Any:
    """Время — строка ISO 8601 (с буквой T); число и строка из цифр не считаются временем.

    Время с поясом без кавычек YAML 1.1 читает сам как дату и время — его принимаем.
    """
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, str) and _TIME_START.match(value):
        return value
    raise ValueError(
        "время должно быть строкой ISO 8601 с поясом, например 2026-11-06T10:00:00+03:00; "
        f"получено: {show(value)} ({kind_of(value)})"
    )


def _check_schema_version(value: Any) -> Any:
    """Версия схемы — целое число; `true`, `1.0` и строка «1» не годятся."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise ValueError(
        f"версия схемы «{show(value)}» не поддерживается; ожидается целое число {SCHEMA_VERSION}"
    )


Time = Annotated[AwareDatetime, BeforeValidator(_check_time_input)]
SchemaVersion = Annotated[Literal[1], BeforeValidator(_check_schema_version)]
Number = Annotated[float, Strict(), Field(allow_inf_nan=False)]  # конечное число
Percent = Annotated[float, Strict(), Field(ge=0, le=100, allow_inf_nan=False)]
Count = Annotated[int, Strict(), Field(ge=0)]
Flag = StrictBool

Mode = Literal["пилот", "автопилот"]
Step = Literal["1", "2а", "2б", "3", "4", "5", "6", "7", "8"]
Gate = Literal["В1", "В2", "В3", "В4", "В5"]
GateStatus = Literal["впереди", "ждут ответа", "отвечено", "пройдены автоматически"]
Actor = Literal["агент", "пользователь", "CLI", "хук"]

SourceKind = Literal[
    "страница",
    "PDF",
    "документ",
    "таблица",
    "презентация",
    "изображение",
    "интервью",
    "заметка пользователя",
    "синтетика",
]
Access = Literal[
    "получен",
    "источник недоступен: защита",
    "источник недоступен: ошибка",
    "запрещено robots.txt",
    "нужен браузер",
]
Anonymized = Literal["телефоны", "адреса почты", "номера документов и карт", "имена", "ники"]

ClaimType = Literal["факт", "гипотеза", "прогноз", "допущение", "синтетика"]
Origin = Literal["источник", "пользователь", "агент", "синтетика"]
ClaimStatus = Literal["действует", "заменено", "отозвано"]
PublisherKind = Literal[
    "официальный первоисточник",
    "отраслевое исследование",
    "СМИ",
    "вторичный пересказ",
    "отзыв пользователя",
    "материал пользователя",
]
CheckMethod = Literal["точное вхождение", "без пробелов и дефисов", "нечёткое"]

EventKind = Literal[
    "действие",
    "ворота",
    "ошибка",
    "исправление",
    "смена режима",
    "выбор модели",
    "изменено вне агента",
    "найдена инструкция в источнике",
    "бюджет",
    "учёт недоступен",
]
ActionStatus = Literal["начато", "выполнено", "ошибка", "пропущено", "прервано"]

CostEntry = Literal["по сообщению", "итог сессии"]
CostSource = Literal["измерено", "оценка", "нет данных"]

# Виды источника, цитату из которых нужно искать по месту (ADR-0004: `locator`).
LOCATOR_KINDS = frozenset({"PDF", "документ", "таблица", "презентация", "интервью"})


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def norm(text: str) -> str:
    """Сравнение названий: NFKC, регистр, пробелы."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


_QUOTES = dict.fromkeys("«»„“”‟‹›‘’‚‛'`", '"')
_DASHES = dict.fromkeys("‐‑‒–—―−﹘﹣－", "-")
_TRANSLATE = str.maketrans({**_QUOTES, **_DASHES, "­": None, "ё": "е"})


def normalize_quote(text: str) -> str:
    """Нормализация для сверки цитаты со снимком (ADR-0005).

    NFKC, удаление мягких переносов, ё -> е, кавычки одного вида, все тире и дефисы одним знаком,
    регистр, пробелы схлопнуты.
    """
    text = unicodedata.normalize("NFKC", text).casefold().translate(_TRANSLATE)
    return " ".join(text.split())


def strip_spaces_and_hyphens(text: str) -> str:
    """Вид «без пробелов и дефисов» от уже нормализованного текста."""
    return "".join(ch for ch in text if not ch.isspace() and ch != "-")


def has_meaning(text: str) -> bool:
    """После нормализации в цитате есть хотя бы одна буква или цифра."""
    return any(ch.isalnum() for ch in normalize_quote(text))


@lru_cache(maxsize=32)
def _normalized_snapshot(snapshot: str) -> tuple[str, str]:
    """Снимок нормализуется один раз: обычный вид и вид без пробелов и дефисов."""
    s = normalize_quote(snapshot)
    return s, strip_spaces_and_hyphens(s)


def quote_in_snapshot(method: str, quote: str, snapshot: str) -> bool:
    """Есть ли цитата в тексте снимка способом «точное вхождение» или «без пробелов и дефисов».

    Цитата без букв и цифр не находится нигде. Нечёткое сходство здесь не считается:
    пересчёт идёт в `quote check` (пакет 3.2).
    """
    if method not in ("точное вхождение", "без пробелов и дефисов"):
        raise ValueError(f"способ «{method}» не проверяется по тексту снимка")
    if not has_meaning(quote):
        return False
    q = normalize_quote(quote)
    s, s_compact = _normalized_snapshot(snapshot)
    if method == "точное вхождение":
        return q in s
    return strip_spaces_and_hyphens(q) in s_compact


def _raise(problems: list[str]) -> None:
    if problems:
        raise ValueError("\n".join(problems))


# ---------------------------------------------------------------------------
# sources.yaml — реестр источников и метаданные снимка
# ---------------------------------------------------------------------------


class FetchInfo(_Model):
    """Метаданные снимка веб-страницы: что и когда скачано."""

    accessed_at: Time = Field(description="Дата и время обращения, ISO 8601 с поясом")
    final_url: Url | None = Field(None, description="Итоговый адрес после перенаправлений")
    region: Text | None = Field(None, description="Код региона, например RU-MOW; пусто, если не зависит")
    encoding: Text | None = Field(None, description="Кодировка ответа, например utf-8")
    extractor: Extractor | None = Field(None, description="Извлекатель текста с версией")
    raw_sha256: Sha256 | None = Field(None, description="sha256 сырого ответа после снятия сжатия")


class Source(_Model):
    """Источник `И-n`: страница, материал человека или синтетика."""

    id: SourceId
    kind: SourceKind
    url: Url | None = Field(None, description="Адрес веб-источника")
    path: RelPath | None = Field(None, description="Файл материала в inputs/ (или снимок экрана)")
    access: Access
    access_note: Text | None = Field(None, description="Ошибка или причина недоступности")
    fetch: FetchInfo | None = None
    text_path: RelPath | None = Field(
        None, description="Файл текста: sources/ для веба, inputs/ для материалов"
    )
    text_sha256: Sha256 | None = Field(None, description="sha256 файла text_path — это хеш снимка")
    suspicious: Flag = False
    untrusted: Flag = False
    anonymized: list[Anonymized] = Field(default_factory=list, description="Что заменено при обезличивании")
    anonymization_confirmed: Flag = Field(False, description="Человек подтвердил обезличенную копию")
    closed_markers: list[Text] = Field(default_factory=list, description="Найденные признаки закрытых данных")
    closed_confirmed: Flag = Field(False, description="Человек подтвердил, что материал общедоступный")
    no_personal_data_confirmed: Flag = Field(
        False, description="Для изображения: человек подтвердил, что ПДн нет"
    )

    @model_validator(mode="after")
    def _rules(self) -> Self:
        p: list[str] = []
        web = self.url is not None
        local = self.path is not None
        if web == local:
            p.append("нужен ровно один адрес: url (веб) или path (материал в inputs/)")
        if web:
            if self.fetch is None:
                p.append("у веб-источника нет блока fetch (дата обращения и метаданные снимка)")
            elif self.access == "получен":
                lacking = [
                    n
                    for n in ("final_url", "encoding", "extractor", "raw_sha256")
                    if getattr(self.fetch, n) is None
                ]
                if lacking:
                    p.append("снимок получен, но в fetch нет: " + ", ".join(lacking))
        elif local:
            if self.fetch is not None:
                p.append("блок fetch бывает только у веб-источника")
            if self.access != "получен":
                p.append(
                    "материал из inputs/ всегда «получен»; недоступные веб-страницы получают другой статус"
                )
            allowed = (INPUTS_DIR + "/", WEB_DIR + "/") if self.kind == "изображение" else (INPUTS_DIR + "/",)
            if not (self.path or "").startswith(allowed):
                p.append(
                    "файл материала должен лежать в " + " или ".join(a.rstrip("/") + "/" for a in allowed)
                )

        if (self.text_path is None) != (self.text_sha256 is None):
            p.append("text_path и text_sha256 задаются вместе")
        needs_text = self.access == "получен" and self.kind != "изображение"
        if needs_text and self.text_path is None:
            p.append("нет снимка текста: нужны text_path и text_sha256")
        if not needs_text and self.text_path is not None:
            p.append("снимок текста есть только у полученного источника, кроме изображения")
        if self.text_path is not None:
            folder = WEB_DIR if web else INPUTS_DIR
            if not self.text_path.startswith(folder + "/"):
                p.append(f"текст {'снимка веба' if web else 'материала'} должен лежать в {folder}/")

        if local:
            if self.anonymized and not self.anonymization_confirmed:
                p.append("материал обезличен, но человек не подтвердил обезличенную копию")
            if self.kind == "изображение" and not self.no_personal_data_confirmed:
                p.append("изображение: человек не подтвердил, что персональных данных на нём нет")
        if self.closed_markers and not self.closed_confirmed:
            p.append("найдены признаки закрытых данных, но человек не подтвердил, что материал общедоступный")
        _raise(p)
        return self


class SourcesFile(_Model):
    """Файл `sources.yaml`: реестр источников. Пишет только CLI."""

    schema_version: SchemaVersion
    sources: list[Source] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# claims.yaml — реестр утверждений
# ---------------------------------------------------------------------------


class QuoteCheck(_Model):
    """Запись проверки цитаты по снимку."""

    method: CheckMethod
    similarity: Percent = Field(
        description="Сходство, 0–100; для точных способов — 100, для нечёткого — не ниже порога"
    )
    snapshot_sha256: Sha256 = Field(description="sha256 снимка, по которому проверяли")
    checked_at: Time

    @model_validator(mode="after")
    def _rules(self) -> Self:
        p: list[str] = []
        if self.method != "нечёткое" and self.similarity != 100:
            p.append(f"способ «{self.method}» требует сходства 100, а в записи проверки {self.similarity:g}")
        if self.method == "нечёткое" and self.similarity < FUZZY_THRESHOLD:
            p.append(
                f"сходство {self.similarity:g} ниже порога {FUZZY_THRESHOLD}: "
                "цитата нечёткой проверкой не проходит"
            )
        _raise(p)
        return self


class Quote(_Model):
    """Цитата из источника и поля источника по ADR-0005."""

    source: SourceId
    text: Text
    locator: Text | None = Field(None, description="Место: страница PDF, лист и ячейка, слайд, строка")
    publisher: Text | None = Field(None, description="Издатель первоисточника")
    publisher_kind: PublisherKind | None = None
    self_reported: Flag | None = Field(None, description="Источник пишет о себе или своём продукте")
    check: QuoteCheck | None = Field(None, description="Нет записи — цитата не проверена")

    @model_validator(mode="after")
    def _rules(self) -> Self:
        p: list[str] = []
        if not has_meaning(self.text):
            p.append(
                "в цитате нет ни буквы, ни цифры после нормализации — так цитата нашлась бы в любом снимке"
            )
        if self.check is not None and self.check.method != "точное вхождение":
            n = len(" ".join(self.text.split()))
            if n < MIN_FUZZY_QUOTE_LEN:
                p.append(
                    f"цитата короче {MIN_FUZZY_QUOTE_LEN} знаков (в цитате {n}) проверяется только "
                    f"точным вхождением, а записан способ «{self.check.method}»"
                )
        _raise(p)
        return self


class StatusChange(_Model):
    """Запись истории статуса: старые записи не стираются."""

    status: ClaimStatus
    at: Time
    by: Actor
    reason: Text | None = None


class Dispute(_Model):
    """Спор: утверждение с расхождением и причина."""

    claim: ClaimId
    reason: Text


class ForecastRange(_Model):
    low: Number
    high: Number
    unit: Text


class Claim(_Model):
    """Утверждение `У-n`."""

    id: ClaimId
    text: Text
    type: ClaimType
    origin: Origin
    subject: Text = Field(
        description="Предмет утверждения; вместе с периодом и регионом задаёт, что оно заменяет"
    )
    period: Text | None = None
    region: Text | None = None
    basis: list[ClaimId] = Field(default_factory=list, description="Утверждения-основания")
    check_method: Text | None = Field(None, description="Гипотеза: чем и как проверить")
    forecast_range: ForecastRange | None = Field(None, description="Прогноз: диапазон")
    taken_from: Text | None = Field(None, description="Допущение: откуда взято")
    quotes: list[Quote] = Field(default_factory=list)
    suspicious: Flag = Field(False, description="Хотя бы один источник помечен «подозрительный»")
    dispute: Dispute | None = None
    status: ClaimStatus
    status_history: list[StatusChange]
    replaces: ClaimId | None = Field(None, description="«Заменяет»")
    replaced_by: ClaimId | None = Field(None, description="«Заменено на»")
    plan_question: Text | None = Field(None, description="Вопрос плана строкой")
    step: Step = Field(description="Шаг, на котором записано утверждение (ADR-0002)")
    attempt: Annotated[int, Strict(), Field(ge=1)] = Field(
        description="Номер попытки шага: при повторе прерванного шага растёт, записанное не дублируется"
    )

    @model_validator(mode="after")
    def _rules(self) -> Self:
        p: list[str] = []
        t = self.type
        if (t == "синтетика") != (self.origin == "синтетика"):
            p.append("тип и происхождение «синтетика» идут вместе: синтетика не становится другим типом")
        if t == "факт":
            if self.origin != "источник":
                p.append(f"факт не может иметь происхождение «{self.origin}»: нужен источник")
            if not any(q.check is not None for q in self.quotes):
                p.append("факт без проверенной цитаты: нужна цитата из источника с записью проверки")
        if t in ("гипотеза", "прогноз") and not (self.basis or self.quotes):
            p.append(f"у типа «{t}» нет оснований: укажите утверждения-основания (basis) или цитаты")
        if t == "гипотеза" and self.check_method is None:
            p.append("у гипотезы нет способа проверки (check_method)")
        if t == "прогноз":
            if self.forecast_range is None:
                p.append("у прогноза нет диапазона (forecast_range)")
            elif self.forecast_range.low > self.forecast_range.high:
                p.append("диапазон прогноза задан наоборот: low больше high")
        if t == "допущение" and self.taken_from is None:
            p.append("у допущения не сказано, откуда оно взято (taken_from)")
        if self.origin == "пользователь" and t != "допущение":
            p.append("правка пользователя без источника — допущение с происхождением «пользователь»")
        if self.origin == "источник":
            if not self.quotes:
                p.append("происхождение «источник», но цитат нет")
            if any(
                q.publisher is None or q.publisher_kind is None or q.self_reported is None
                for q in self.quotes
            ):
                p.append("у цитаты из источника нужны publisher, publisher_kind и self_reported")

        if self.id in self.basis:
            p.append("утверждение указано в собственных основаниях")
        if len(set(self.basis)) != len(self.basis):
            p.append("в основаниях есть повторы")
        if self.dispute is not None and self.dispute.claim == self.id:
            p.append("спор ссылается на само утверждение")

        hist = self.status_history
        if not hist:
            p.append("пустая история статусов")
        else:
            if hist[0].status != "действует":
                p.append("история статусов начинается не с «действует»")
            if hist[-1].status != self.status:
                p.append(
                    f"статус «{self.status}» не совпадает с последней записью истории «{hist[-1].status}»"
                )
            for i, (a, b) in enumerate(zip(hist, hist[1:], strict=False), start=2):
                if a.status != "действует":
                    p.append(f"в истории статусов запись {i} идёт после «{a.status}»: этот статус конечный")
                elif b.status == a.status:
                    p.append(f"в истории статусов запись {i} повторяет статус «{b.status}»")
                if b.at < a.at:
                    p.append(f"в истории статусов запись {i} раньше предыдущей")
        if self.status == "заменено":
            if self.replaced_by is None:
                p.append("статус «заменено» без ссылки на замену (replaced_by)")
            elif self.replaced_by == self.id:
                p.append("утверждение заменено само собой")
        elif self.replaced_by is not None:
            p.append(
                f"replaced_by задано при статусе «{self.status}»: ссылка на замену бывает только у «заменено»"
            )
        if self.replaces == self.id:
            p.append("утверждение заменяет само себя")
        _raise(p)
        return self


class ClaimsFile(_Model):
    """Файл `claims.yaml`: реестр утверждений. Пишут скиллы через CLI."""

    schema_version: SchemaVersion
    claims: list[Claim] = Field(default_factory=list)


def independent_primary_sources(claim: Claim) -> int:
    """Число независимых первоисточников утверждения — вычисляется, не хранится.

    Считаются различные издатели первоисточника среди проверенных цитат: перепечатка и пересказ
    одного первоисточника — один первоисточник.
    """
    return len({norm(q.publisher) for q in claim.quotes if q.publisher and q.check is not None})


# ---------------------------------------------------------------------------
# journal.jsonl — журнал событий (одна запись — одна строка)
# ---------------------------------------------------------------------------


class GateEvent(_Model):
    """Событие ворот. Ожидание ответа не входит во время работы."""

    gate: Gate
    mode: Mode
    questions: Count = Field(description="Сколько вопросов задано человеку")
    opened_at: Time
    answered_at: Time | None = Field(None, description="Нет ответа — ворота прошли сообщением или ждут")
    answer: Text | None = None

    @model_validator(mode="after")
    def _rules(self) -> Self:
        p: list[str] = []
        if self.answered_at is None and self.answer is not None:
            p.append("есть ответ, но нет времени ответа (answered_at)")
        if self.answered_at is not None and self.answer is None:
            p.append("есть время ответа, но нет самого ответа (answer)")
        if self.answered_at is not None and self.answered_at < self.opened_at:
            p.append("ответ на воротах раньше их открытия")
        _raise(p)
        return self


class JournalEvent(_Model):
    """Строка `journal.jsonl`. Только добавление. Пишет только CLI."""

    schema_version: SchemaVersion
    time: Time
    actor: Actor
    kind: EventKind
    step: Step
    name: Text | None = Field(None, description="Что за действие, например «запрос получен»")
    message: Text | None = Field(None, description="Пояснение или причина")
    key: Sha256 | None = Field(None, description="Ключ идемпотентности: хеш шага и входов")
    status: ActionStatus | None = None
    duration_s: Number | None = Field(None, ge=0, description="Длительность действия, секунды")
    gate: GateEvent | None = None
    mode: Mode | None = Field(None, description="Для смены режима")
    role: Text | None = Field(None, description="Для выбора модели")
    model: Text | None = Field(None, description="Для выбора модели")
    source: SourceId | None = Field(None, description="Для найденной инструкции в источнике")
    path: RelPath | None = Field(None, description="Для «изменено вне агента»")

    @model_validator(mode="after")
    def _rules(self) -> Self:
        p: list[str] = []
        k = self.kind
        if k == "действие":
            if self.name is None:
                p.append("у действия нет названия (name)")
            if self.key is None:
                p.append("у действия нет ключа идемпотентности (key)")
            if self.status is None:
                p.append("у действия нет статуса (status)")
            if self.status == "выполнено" and self.duration_s is None:
                p.append(
                    "у выполненного действия нет длительности (duration_s): по ней считается время работы"
                )
        else:
            if self.status is not None or self.duration_s is not None:
                p.append(f"status и duration_s бывают только у действия, а не у «{k}»")
            if self.key is not None and k != "ошибка":
                p.append(f"key бывает только у действия и ошибки, а не у «{k}»")
        if (k == "ворота") != (self.gate is not None):
            p.append("блок gate нужен событию «ворота» и только ему")
        if k == "смена режима" and self.mode is None:
            p.append("у смены режима нет нового режима (mode)")
        if k == "выбор модели" and (self.role is None or self.model is None):
            p.append("у выбора модели нужны role и model")
        if k == "найдена инструкция в источнике" and self.source is None:
            p.append("у найденной инструкции нет источника (source)")
        if k == "изменено вне агента" and self.path is None:
            p.append("у события «изменено вне агента» нет файла (path)")
        if k in ("ошибка", "исправление", "бюджет", "учёт недоступен") and self.message is None:
            p.append(f"у события «{k}» нет пояснения (message)")
        _raise(p)
        return self


# ---------------------------------------------------------------------------
# cost.jsonl — учёт стоимости (ADR-0010)
# ---------------------------------------------------------------------------


class CostRecord(_Model):
    """Строка `cost.jsonl`. Только добавление. Стоимость может быть оценкой или отсутствовать."""

    schema_version: SchemaVersion
    time: Time
    session_id: Text
    role: Text
    model: Text
    environment: Text = Field(description="Среда: claude-code, claude-p, cursor и т. п.")
    step: Step | None = None
    entry: CostEntry
    message_id: Text | None = Field(None, description="id сообщения транскрипта — ключ против удвоения")
    input_tokens: Count = 0
    output_tokens: Count = 0
    cache_write_5m_tokens: Count = 0
    cache_write_1h_tokens: Count = 0
    cache_read_tokens: Count = 0
    web_searches: Count = 0
    cost_usd: Number | None = Field(None, ge=0)
    cost_source: CostSource

    @model_validator(mode="after")
    def _rules(self) -> Self:
        p: list[str] = []
        if self.entry == "по сообщению" and self.message_id is None:
            p.append("у записи «по сообщению» нет message_id: без него не отличить повторный разбор")
        if self.entry == "итог сессии" and self.message_id is not None:
            p.append("у записи «итог сессии» не бывает message_id")
        if (self.cost_usd is None) != (self.cost_source == "нет данных"):
            p.append("cost_usd пусто тогда и только тогда, когда cost_source = «нет данных»")
        _raise(p)
        return self


# ---------------------------------------------------------------------------
# state.yaml — состояние проекта
# ---------------------------------------------------------------------------


class Budget(_Model):
    """Бюджет и расход. Денег может не быть: тогда лимит действует по времени."""

    limit_usd: Number | None = Field(None, gt=0)
    spent_usd: Number | None = Field(None, ge=0, description="Нет данных о стоимости — пусто")
    limit_minutes: Number = Field(gt=0)
    spent_minutes: Number = Field(ge=0)


class ProjectState(_Model):
    """Файл `state.yaml`. Пишет только CLI."""

    schema_version: SchemaVersion
    id: ProjectId
    mode: Mode
    step: Step
    gates: dict[Gate, GateStatus]
    budget: Budget
    next_action: Text | None = Field(None, description="Что делать дальше")
    files: dict[RelPath, Sha256] = Field(
        default_factory=dict,
        description="sha256 файлов, которые пишет CLI: по ним замечают правки вне агента",
    )

    @model_validator(mode="after")
    def _rules(self) -> Self:
        p: list[str] = []
        lacking = [g for g in ("В1", "В2", "В3", "В4", "В5") if g not in self.gates]
        if lacking:
            p.append("в gates нет ворот: " + ", ".join(lacking))
        _raise(p)
        return self


# Схемы, которые генерирует scripts/gen_schemas.py: имя файла без `.schema.json` -> модель.
SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "state": ProjectState,
    "sources": SourcesFile,
    "claims": ClaimsFile,
    "journal-event": JournalEvent,
    "cost-record": CostRecord,
}

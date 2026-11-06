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
PlanId = Annotated[str, Field(pattern=r"^ВП-[1-9][0-9]*$")]
GapId = Annotated[str, Field(pattern=r"^П-[1-9][0-9]*$")]
OptionId = Annotated[str, Field(pattern=r"^Вар-[1-9][0-9]*$")]
PromiseId = Annotated[str, Field(pattern=r"^О-[1-9][0-9]*$")]
RowId = Annotated[str, Field(pattern=r"^ПП-[1-9][0-9]*$")]
DecisionId = Annotated[str, Field(pattern=r"^Реш-[1-9][0-9]*$")]
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


def latin_hint(value: Any) -> str:
    """Подсказка, если в номере латинская буква: B, P, O, a, e, p глазом не отличить от русских."""
    if not isinstance(value, str):
        return ""
    letters = sorted({ch for ch in value if ch.isascii() and ch.isalpha()})
    if not letters:
        return ""
    shown = ", ".join(f"«{ch}»" for ch in letters)
    return f"; в записи латинские буквы ({shown}), а в номерах нужны русские"


_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CLAIM_REF = re.compile(r"^У-[1-9][0-9]*$")
HYPOTHESIS_WORD = "гипотеза"
_NOT_ROLES = frozenset({"все", "команда", "tbd"})


def _check_day(value: str) -> str:
    """Дата — строка ГГГГ-ММ-ДД в кавычках: без кавычек YAML 1.1 читает её как другой тип."""
    try:
        if not _DAY.match(value):
            raise ValueError
        dt.date(int(value[:4]), int(value[5:7]), int(value[8:]))
    except ValueError:
        raise ValueError(
            f"дата «{value}» не подходит: нужна дата вида 2026-11-06 (год-месяц-день) в кавычках"
        ) from None
    return value


def _check_basis(value: str) -> str:
    if value == HYPOTHESIS_WORD or _CLAIM_REF.match(value):
        return value
    raise ValueError(
        f"основание «{value}» не подходит: нужен номер утверждения вида У-1 (буква русская) "
        f"или слово «{HYPOTHESIS_WORD}»{latin_hint(value)}"
    )


def _check_owner(value: str) -> str:
    """Владелец — одна роль; «все», «команда» и «TBD» ответственности не задают."""
    if norm(value).strip(" .,;:!") in _NOT_ROLES:
        raise ValueError(
            f"владелец «{value}» не роль: «все», «команда» и «TBD» отвергаются, "
            "назовите одну роль, например «руководитель направления»"
        )
    return value


Day = Annotated[
    str,
    AfterValidator(_check_day),
    Field(json_schema_extra={"pattern": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"}),
]
BasisRef = Annotated[
    str,
    AfterValidator(_check_basis),
    Field(json_schema_extra={"pattern": r"^(У-[1-9][0-9]*|гипотеза)$"}),
]
Owner = Annotated[Text, AfterValidator(_check_owner)]

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
    пересчёт идёт при записи утверждения командой `claim add`.
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
# frame.yaml — рамка задачи; те же поля хранит событие ворот В1
# ---------------------------------------------------------------------------

FrameOrigin = Literal["из запроса", "из материалов", "ответ человека", "допущение"]


class FrameField(_Model):
    """Поле рамки: значение и откуда оно."""

    value: Text
    origin: FrameOrigin
    source: SourceId | None = Field(None, description="Источник, если значение взято из материала")

    @model_validator(mode="after")
    def _rules(self) -> Self:
        if self.source is not None and self.origin != "из материалов":
            raise ValueError(
                f"источник (source) указан при происхождении «{self.origin}»: "
                "он бывает только у «из материалов»"
            )
        return self


class FrameBody(_Model):
    """Поля рамки без служебных: их хранят `frame.yaml` и событие В1."""

    decision: FrameField
    decision_space: list[Text] = Field(min_length=2, description="Варианты решения: не меньше двух")
    deadline: FrameField
    research_question: FrameField
    recipient: FrameField
    constraints: FrameField
    data: FrameField
    region: FrameField
    success_criteria: FrameField
    out_of_scope: FrameField

    @model_validator(mode="after")
    def _rules(self) -> Self:
        seen = [norm(x) for x in self.decision_space]
        if len(set(seen)) != len(seen):
            raise ValueError("в decision_space есть повторы: варианты решения должны различаться")
        return self


class _FrameHeader(_Model):
    schema_version: SchemaVersion
    version: Annotated[int, Strict(), Field(ge=1)] = Field(
        description="Версия рамки: растёт при каждой перезаписи, прежние лежат в history/"
    )


class FrameFile(FrameBody, _FrameHeader):
    """Файл `frame.yaml`: рамка задачи. Пишет только CLI."""


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
    frame: FrameBody | None = Field(None, description="Только у В1: все поля рамки и откуда каждое")

    @model_validator(mode="after")
    def _rules(self) -> Self:
        p: list[str] = []
        if self.frame is not None and self.gate != "В1":
            p.append(f"поле frame бывает только у ворот В1, а не у {self.gate}")
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
    idempotency_hash: Sha256 | None = Field(None, description="Ключ идемпотентности: хеш шага и входов")
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
            if self.idempotency_hash is None:
                p.append("у действия нет ключа идемпотентности (idempotency_hash)")
            if self.status is None:
                p.append("у действия нет статуса (status)")
            if self.status == "выполнено" and self.duration_s is None:
                p.append(
                    "у выполненного действия нет длительности (duration_s): по ней считается время работы"
                )
        else:
            if self.status is not None or self.duration_s is not None:
                p.append(f"status и duration_s бывают только у действия, а не у «{k}»")
            if self.idempotency_hash is not None and k != "ошибка":
                p.append(f"idempotency_hash бывает только у действия и ошибки, а не у «{k}»")
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
    region: Text | None = Field(None, description="Регион по умолчанию для fetch, например RU-MOW")
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


# ---------------------------------------------------------------------------
# plan.yaml — план исследования
# ---------------------------------------------------------------------------

Impact = Literal["высокое", "среднее", "низкое"]
QuestionStatus = Literal["открыт", "закрыт", "пробел"]


class _AliasModel(_Model):
    """Модель с ключами `if` и `from`: это слова Python, поле называется иначе, файл пишет ключ."""

    model_config = ConfigDict(extra="forbid", serialize_by_alias=True)


class Estimate(_Model):
    """Оценка стоимости и времени: деньги или минуты могут быть неизвестны."""

    usd: Number | None = Field(None, ge=0)
    minutes: Number | None = Field(None, ge=0)


class Branch(_AliasModel):
    """Ветка вопроса плана: ответ и действие, к которому он ведёт."""

    if_: Text = Field(alias="if", description="Ответ или находка")
    then: Text = Field(description="Что делаем при таком ответе")


def _action_key(text: str) -> str:
    return norm(text).strip(" .,;:!")


class PlanQuestion(_Model):
    """Вопрос плана `ВП-n`: попадает в план, только если разные ответы ведут к разным действиям."""

    id: PlanId
    text: Text
    impact: Impact
    branches: list[Branch] = Field(min_length=2)
    close_cost: Estimate
    where: Text = Field(description="Где искать")
    status: QuestionStatus
    gap: GapId | None = Field(None, description="Пробел, если вопрос остался без ответа")

    @model_validator(mode="after")
    def _rules(self) -> Self:
        p: list[str] = []
        seen: set[str] = set()
        for b in self.branches:
            key = _action_key(b.then)
            if key in seen:
                p.append(
                    f"в ветках одинаковое действие «{b.then}»: вопрос, ответы на который ведут к одному "
                    "действию, в план не попадает — перенесите его в excluded"
                )
                break
            seen.add(key)
        if self.status == "пробел" and self.gap is None:
            p.append("статус «пробел» без ссылки на пробел (gap)")
        if self.status != "пробел" and self.gap is not None:
            p.append(f"gap задано при статусе «{self.status}»: ссылка на пробел бывает только у «пробел»")
        _raise(p)
        return self


class Excluded(_Model):
    """Вопрос, который не меняет решение, и почему."""

    text: Text
    reason: Text


class PlanFile(_Model):
    """Файл `plan.yaml`: план исследования. Пишет только CLI."""

    schema_version: SchemaVersion
    questions: list[PlanQuestion] = Field(min_length=1)
    excluded: list[Excluded] = Field(default_factory=list)
    estimate: Estimate = Field(description="Оценка всего пути до выпуска")

    @model_validator(mode="after")
    def _rules(self) -> Self:
        if self.estimate.usd is None and self.estimate.minutes is None:
            raise ValueError("оценка пути (estimate) пуста: укажите минуты, доллары или то и другое")
        return self


# ---------------------------------------------------------------------------
# gaps.yaml — пробелы и перечень показателей
# ---------------------------------------------------------------------------

GapReason = Literal["нет данных", "данные закрыты", "источники спорят", "источник недоступен", "другая"]
GapStatus = Literal["открыт", "закрыт"]
MetricKind = Literal["охват", "конверсия", "удержание и отток", "выручка и затраты", "качество продукта"]
CloseVia = Literal["открытый источник", "материал руководителя", "закрытые данные"]


class Search(_Model):
    """Что искали и когда."""

    query: Text
    date: Day


class Gap(_Model):
    """Пробел `П-n`: чего не знаем, почему и что искали."""

    id: GapId
    text: Text
    reason: GapReason
    reason_note: Text | None = Field(None, description="Пояснение к причине «другая»")
    searched: list[Search] = Field(min_length=1)
    affects_choice: Flag
    affects_options: list[OptionId] = Field(default_factory=list, description="Может быть пустым до шага 4")
    how_to_close: Text
    close_cost: Estimate
    close_via: CloseVia | None = Field(None, description="Чем пробел предлагается закрыть")
    assumption: ClaimId | None = Field(None, description="Допущение, принятое вместо значения")
    hypothesis: ClaimId | None = Field(None, description="Гипотеза, проверить которую мешает пробел")
    metric_kind: MetricKind | None = None
    status: GapStatus
    closed_by: list[ClaimId] = Field(default_factory=list, description="Чем закрыт")

    @model_validator(mode="after")
    def _rules(self) -> Self:
        p: list[str] = []
        if self.reason == "другая" and self.reason_note is None:
            p.append("причина «другая» без пояснения (reason_note)")
        if self.reason != "другая" and self.reason_note is not None:
            p.append(f"reason_note задано при причине «{self.reason}»: пояснение бывает только у «другая»")
        if self.status == "закрыт" and not self.closed_by:
            p.append("статус «закрыт» без ссылок на утверждения (closed_by)")
        if self.status == "открыт" and self.closed_by:
            p.append("closed_by задано при статусе «открыт»: ссылки на утверждения бывают только у «закрыт»")
        _raise(p)
        return self


class MetricCoverage(_Model):
    """Показатель, без которого решение не проверить, и ровно один ответ по нему."""

    kind: MetricKind
    claims: list[ClaimId] | None = Field(None, min_length=1, description="Что известно")
    gap: GapId | None = Field(None, description="Пробел, если значение не найдено")
    not_relevant: Text | None = Field(None, description="Причина, почему показатель не относится к задаче")

    @model_validator(mode="after")
    def _rules(self) -> Self:
        given = [
            name
            for name, value in (
                ("claims", self.claims),
                ("gap", self.gap),
                ("not_relevant", self.not_relevant),
            )
            if value is not None
        ]
        if len(given) != 1:
            if given:
                raise ValueError(
                    f"по показателю «{self.kind}» несколько ответов ({', '.join(given)}): нужен ровно один"
                )
            raise ValueError(
                f"по показателю «{self.kind}» нет ответа: укажите ровно одно из claims, gap, not_relevant"
            )
        return self


class GapsFile(_Model):
    """Файл `gaps.yaml`: пробелы и перечень показателей. Пишет только CLI."""

    schema_version: SchemaVersion
    gaps: list[Gap] = Field(default_factory=list)
    metric_coverage: list[MetricCoverage] = Field(
        default_factory=list, description="Полнота перечня — признак рубрики, до шага 3 перечня может не быть"
    )

    @model_validator(mode="after")
    def _rules(self) -> Self:
        first: dict[str, int] = {}
        p: list[str] = []
        for n, m in enumerate(self.metric_coverage, start=1):
            if m.kind in first:
                p.append(f"показатель «{m.kind}» повторён в metric_coverage (записи №{first[m.kind]} и №{n})")
            first.setdefault(m.kind, n)
        _raise(p)
        return self


# ---------------------------------------------------------------------------
# options.yaml — варианты, возражения, сравнение, рекомендация, выбор
# ---------------------------------------------------------------------------

OptionKind = Literal["оставить как есть", "закрыть направление", "не запускать", "активный"]
Axis = Literal["аудитория", "ценность", "модель дохода"]
Criterion = Literal["эффект", "затраты", "риск", "первый сигнал", "обратимость"]


class Differs(_AliasModel):
    """Чем вариант отличается от ближайшего: по какой оси, от какого и как."""

    axis: Axis
    from_: OptionId = Field(alias="from")
    how: Text


class Objection(_Model):
    """Возражение: какое допущение бьёт, что должно оказаться правдой и на чём стоит."""

    hits_assumption: Text
    drops_if: Text
    basis: BasisRef


class Cell(_Model):
    """Ячейка сравнения: текст или причина, почему пусто."""

    text: Text | None = None
    why_empty: Text | None = None

    @model_validator(mode="after")
    def _cell(self) -> Self:
        if (self.text is None) == (self.why_empty is None):
            raise ValueError("в ячейке нужно ровно одно из text и why_empty")
        return self


class ForecastCell(Cell):
    """Эффект и первый сигнал — прогнозы: с текстом идёт утверждение типа «прогноз»."""

    forecast: ClaimId | None = None

    @model_validator(mode="after")
    def _forecast(self) -> Self:
        if self.text is not None and self.forecast is None:
            raise ValueError(
                "у ячейки с текстом нет ссылки на прогноз (forecast): эффект и первый сигнал — прогнозы"
            )
        if self.text is None and self.forecast is not None:
            raise ValueError("forecast задан у пустой ячейки: прогноз бывает только у ячейки с текстом")
        return self


class ReversibilityCell(Cell):
    """Обратимость: с текстом нужны механизм, срок и цена отката."""

    what: Text | None = Field(None, description="Механизм отката")
    time: Text | None = Field(None, description="Срок отката")
    price: Text | None = Field(None, description="Цена отката")

    @model_validator(mode="after")
    def _reversal(self) -> Self:
        given = {"what": self.what, "time": self.time, "price": self.price}
        if self.text is not None:
            lacking = [k for k, v in given.items() if v is None]
            if lacking:
                raise ValueError("у обратимости с текстом нет: " + ", ".join(lacking))
        elif any(v is not None for v in given.values()):
            raise ValueError(
                "what, time и price заданы у пустой ячейки: они бывают только у ячейки с текстом"
            )
        return self


class Comparison(_Model):
    """Сравнение по пяти критериям, без суммы баллов."""

    effect: ForecastCell
    cost: Cell
    risk: Cell
    first_signal: ForecastCell
    reversibility: ReversibilityCell


class Option(_Model):
    """Вариант `Вар-n`."""

    id: OptionId
    title: Text
    kind: OptionKind
    differs: Differs
    main_assumption: Text
    inaction_cost: Text | None = Field(None, description="Цена бездействия, у «оставить как есть»")
    return_condition: Text | None = Field(
        None, description="Условие возврата, у «закрыть направление» и «не запускать»"
    )
    objections: list[Objection] = Field(min_length=2)
    comparison: Comparison

    @model_validator(mode="after")
    def _rules(self) -> Self:
        p: list[str] = []
        if self.kind == "оставить как есть" and self.inaction_cost is None:
            p.append("у «оставить как есть» нет цены бездействия (inaction_cost)")
        if self.kind in ("закрыть направление", "не запускать") and self.return_condition is None:
            p.append(f"у «{self.kind}» нет условия возврата (return_condition)")
        if self.differs.from_ == self.id:
            p.append("вариант отличается сам от себя: в differs.from нужен другой вариант")
        _raise(p)
        return self


class ChangeMind(_Model):
    """«Передумаю, если»: показатель, порог и действие."""

    metric: Text
    threshold: Text
    action: Text


class Recommendation(_Model):
    """Рекомендация: вариант, основания, сильнейшее возражение против неё и что заставит передумать."""

    option: OptionId
    why: Text
    claims: list[ClaimId] = Field(min_length=1)
    strongest_objection: Text
    answer: Text
    change_mind: list[ChangeMind] = Field(min_length=1)
    loses_on: Criterion = Field(description="Критерий, по которому рекомендованный вариант проигрывает")


class OptionsFile(_Model):
    """Файл `options.yaml`: варианты и рекомендация. Пишет только CLI."""

    schema_version: SchemaVersion
    options: list[Option]
    recommendation: Recommendation
    chosen: OptionId | None = Field(None, description="Выбор человека на В4; до ответа пусто")

    @model_validator(mode="after")
    def _rules(self) -> Self:
        p: list[str] = []
        kinds = [o.kind for o in self.options]
        if len(kinds) < 4:
            p.append(f"вариантов {len(kinds)}, нужно не меньше четырёх")
        if "оставить как есть" not in kinds:
            p.append("нет варианта «оставить как есть»")
        if "закрыть направление" not in kinds and "не запускать" not in kinds:
            p.append("нет варианта «закрыть направление» (для нового продукта — «не запускать»)")
        if kinds.count("активный") < 2:
            p.append(f"активных вариантов {kinds.count('активный')}, нужно не меньше двух")
        _raise(p)
        return self


# ---------------------------------------------------------------------------
# promises.yaml — обещание -> возможность -> источник
# ---------------------------------------------------------------------------

PromiseStatus = Literal["подтверждено", "подтверждено частично", "не подтверждено"]


class Promise(_Model):
    """Обещание `О-n`: что обещаем, чем это обеспечено и на каком факте стоит."""

    id: PromiseId
    text: Text
    capability: Text = Field(description="Возможность продукта")
    claims: list[ClaimId] = Field(default_factory=list, description="Факты, на которых стоит обещание")
    conditions: Text = Field(description="Условия и исключения, регион, период")
    status: PromiseStatus

    @model_validator(mode="after")
    def _rules(self) -> Self:
        if self.status != "не подтверждено" and not self.claims:
            raise ValueError(
                f"статус «{self.status}» без фактов (claims): подтверждение стоит на утверждениях"
            )
        return self


class PromisesFile(_Model):
    """Файл `promises.yaml`: таблица обещаний. Пишет только CLI."""

    schema_version: SchemaVersion
    promises: list[Promise]


# ---------------------------------------------------------------------------
# verification.yaml — план проверки
# ---------------------------------------------------------------------------

RowKind = Literal["основной", "защитный"]
Strength = Literal["сильная", "средняя", "слабая"]


class Magnitude(_Model):
    """Величина изменения диапазоном."""

    low: Number
    high: Number
    unit: Text

    @model_validator(mode="after")
    def _rules(self) -> Self:
        if self.low > self.high:
            raise ValueError("диапазон задан наоборот: low больше high")
        return self


class Threshold(_Model):
    """Порог и действие при его пересечении."""

    threshold: Text
    action: Text


class VerificationRow(_Model):
    """Строка плана проверки `ПП-n`: показатель, величина, база, срок, владелец и порог отказа."""

    id: RowId
    hypothesis: ClaimId | None = Field(
        None, description="Проверяемая гипотеза; у защитного показателя может быть пусто"
    )
    kind: RowKind
    metric: Text
    direction: Text
    magnitude: Magnitude
    baseline: Text = Field(description="База сравнения; нет базы — номер пробела П-n")
    deadline: Text
    data_source: Text
    owner: Owner
    change_mind: Threshold
    from_change_mind: Annotated[int, Strict(), Field(ge=1)] | None = Field(
        None, description="Номер записи в options.yaml: recommendation.change_mind, откуда взят порог"
    )
    needed_n: Annotated[int, Strict(), Field(ge=1)] | None = Field(None, description="Нужный размер выборки")
    method: Text | None = None
    strength: Strength | None = None
    not_prove: Text | None = Field(None, description="Что проверка не докажет")

    @model_validator(mode="after")
    def _rules(self) -> Self:
        p: list[str] = []
        if self.kind == "основной" and self.hypothesis is None:
            p.append("у основного показателя нет гипотезы (hypothesis)")
        if self.method is not None:
            if self.strength is None:
                p.append("у метода нет оценки силы (strength)")
            if self.not_prove is None:
                p.append("у метода нет границы вывода (not_prove): что проверка не докажет")
        _raise(p)
        return self


class PremortemCause(_Model):
    """Причина провала выбранного варианта: гипотеза, сигнал, порог и строка плана."""

    claim: ClaimId
    signal: Text
    threshold: Text
    row: RowId
    hits_choice: Flag = Field(description="Причина бьёт по главному допущению выбранного варианта")


class UndesiredEffect(_Model):
    text: Text
    row: RowId


class VerificationFile(_Model):
    """Файл `verification.yaml`: план проверки. Пишет только CLI."""

    schema_version: SchemaVersion
    rows: list[VerificationRow] = Field(min_length=1)
    premortem: list[PremortemCause] = Field(min_length=3, max_length=3, description="Ровно три причины")
    undesired_effects: list[UndesiredEffect] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# decisions.yaml — решения (ADR-0016)
# ---------------------------------------------------------------------------

ConditionOrigin = Literal["передумаю, если", "пре-мортем"]
DecisionStatus = Literal["действует", "заменено", "отменено"]


class DecisionCondition(_Model):
    """Условие отказа: без порога и даты его не проверить."""

    metric: Text
    threshold: Text
    date: Day
    action: Text
    origin: ConditionOrigin


class Commitment(_Model):
    what: Text
    due: Day
    owner: Owner


class Decision(_Model):
    """Решение `Реш-n`."""

    id: DecisionId
    option: OptionId
    basis: list[ClaimId] = Field(min_length=1, description="Утверждения-основания")
    rejected: list[OptionId] = Field(default_factory=list, description="Отвергнутые варианты")
    assumptions: list[ClaimId] = Field(default_factory=list, description="Принятые допущения")
    expected: Text = Field(description="Ожидаемое изменение")
    owner: Owner
    conditions: list[DecisionCondition] = Field(min_length=1)
    review_date: Day
    commitments: list[Commitment] = Field(default_factory=list)
    status: DecisionStatus
    replaces: DecisionId | None = None
    replaced_by: DecisionId | None = None

    @model_validator(mode="after")
    def _rules(self) -> Self:
        p: list[str] = []
        if self.option in self.rejected:
            p.append("выбранный вариант указан и среди отвергнутых")
        if self.status == "заменено":
            if self.replaced_by is None:
                p.append("статус «заменено» без ссылки на замену (replaced_by)")
        elif self.replaced_by is not None:
            p.append(
                f"replaced_by задано при статусе «{self.status}»: ссылка на замену бывает только у «заменено»"
            )
        if self.replaces == self.id or self.replaced_by == self.id:
            p.append("решение заменяет само себя")
        _raise(p)
        return self


class DecisionsFile(_Model):
    """Файл `decisions.yaml`: решения. Пишет только CLI."""

    schema_version: SchemaVersion
    decisions: list[Decision]


# Схемы, которые генерирует scripts/gen_schemas.py: имя файла без `.schema.json` -> модель.
SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "state": ProjectState,
    "sources": SourcesFile,
    "claims": ClaimsFile,
    "journal-event": JournalEvent,
    "cost-record": CostRecord,
    "frame": FrameFile,
    "plan": PlanFile,
    "gaps": GapsFile,
    "options": OptionsFile,
    "promises": PromisesFile,
    "verification": VerificationFile,
    "decisions": DecisionsFile,
}

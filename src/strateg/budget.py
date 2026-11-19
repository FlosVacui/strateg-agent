"""Бюджет проекта и таблицы настроек `config/` (ADR-0010).

Бюджет — деньги и время работы агента; остановка наступает по тому, что кончится раньше. Время — сумма
длительностей шагов без ожидания ответов человека, плюс идущий шаг. Если стоимость неизвестна, лимит
действует только по времени. На выпуск с пометкой о неполноте оставлен резерв: в нём `budget check`
не отказывает.

`strateg budget check --next` оценивает цену следующего шага: медиана по завершённым шагам проекта (деньги —
из `cost.jsonl`, минуты — из журнала); истории нет — таблица `step_estimate` в `config/defaults.yaml`, и вывод
называет её оценкой. Остаток меньше оценки — код 1.

`config/defaults.yaml` — бюджет по умолчанию; `config/prices.yaml` — цены Anthropic с датой обращения
к странице цен (по ним оценивается стоимость, когда среда не отдаёт доллары).
"""

from __future__ import annotations

import os
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from strateg import project as proj
from strateg.models import JournalEvent
from strateg.project import CliError, Project

CONFIG_ENV = "STRATEG_CONFIG_DIR"  # где лежат defaults.yaml и prices.yaml (по умолчанию config/ репозитория)
DEFAULTS_FILE = "defaults.yaml"
PRICES_FILE = "prices.yaml"
RELEASE_ACTIONS = frozenset({"выпуск", "release"})  # действия, которым отдан резерв бюджета


def config_dir() -> Path:
    """Папка настроек: переменная окружения, затем `config/` рядом с исходниками, затем `config/` корня
    репозитория, найденного от текущей папки."""
    env = os.environ.get(CONFIG_ENV)
    if env:
        return Path(env)
    here = Path(__file__).resolve().parents[2] / "config"
    if (here / DEFAULTS_FILE).is_file():
        return here
    from strateg.cli import find_repo_root  # импорт здесь: cli импортирует модули команд

    repo = find_repo_root()
    if repo is None:
        raise CliError(
            f"не найден корень репозитория (pyproject.toml); задайте папку настроек в {CONFIG_ENV}"
        )
    return repo / "config"


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid")


Positive = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class Reserve(_Config):
    usd: Positive
    minutes: Positive


class BudgetDefaults(_Config):
    usd: Positive = Field(description="Лимит денег, USD")
    minutes: Positive = Field(description="Лимит времени работы агента, минуты")
    warn_share: float = Field(gt=0, lt=1, description="С какой доли лимита строка статуса предупреждает")
    release_reserve: Reserve = Field(description="Резерв на выпуск с пометкой о неполноте")


class FollowUpDefaults(_Config):
    source_max_age_days: Annotated[int, Field(gt=0)] = Field(
        default=180,
        description="Источник факта старше стольких дней (по дате обращения): основание решения устарело",
    )
    commitments_window_days: Annotated[int, Field(ge=0)] = Field(
        default=7,
        description="Обязательство со сроком в пределах стольких дней попадает в `strateg due`",
    )


STEPS = ("1", "2а", "2б", "3", "4", "5", "6", "7", "8")
# Оценка шага без истории (доллары и минуты); те же числа — в `config/defaults.yaml`, раздел `step_estimate`.
DEFAULT_STEP_ESTIMATE = {
    "1": (0.5, 2.0),
    "2а": (0.4, 2.0),
    "2б": (1.6, 8.0),
    "3": (0.4, 3.0),
    "4": (0.9, 4.0),
    "5": (0.7, 3.0),
    "6": (0.3, 2.0),
    "7": (0.6, 3.0),
    "8": (0.8, 3.0),
}


class StepEstimate(_Config):
    usd: Positive
    minutes: Positive


def _default_step_estimate() -> dict[str, StepEstimate]:
    return {k: StepEstimate(usd=u, minutes=m) for k, (u, m) in DEFAULT_STEP_ESTIMATE.items()}


class Defaults(_Config):
    budget: BudgetDefaults
    step_estimate: dict[str, StepEstimate] = Field(
        default_factory=_default_step_estimate,
        description="Цена одного шага без истории проекта (`budget check --next`); раздела нет — числа кода",
    )
    follow_up: FollowUpDefaults = Field(
        default_factory=FollowUpDefaults,
        description="Сопровождение решений; раздела нет — значения по умолчанию",
    )


class TokenPrices(_Config):
    input: Positive
    output: Positive
    cache_write_5m: Positive
    cache_write_1h: Positive
    cache_read: Positive


class LongPrompt(TokenPrices):
    """Цены Haiku 5.5 для запроса, где ввод длиннее порога (в ввод входят и токены кэша)."""

    threshold_tokens: Annotated[int, Field(gt=0)]


class ModelPrices(TokenPrices):
    long_prompt: LongPrompt | None = None


class FastPrice(_Config):
    input: Positive
    output: Positive


class WebSearchQuery(_Config):
    """Запрос к модели поиска: среднее по замеру, не цена со страницы цен."""

    model: StrictStr = Field(description="Модель, которая выполняет поиск; её цены — в `models`")
    input_tokens: Annotated[int, Field(gt=0)]
    output_tokens: Annotated[int, Field(gt=0)]
    measured: StrictStr = Field(description="Дата замера, ГГГГ-ММ-ДД")

    @field_validator("measured")
    @classmethod
    def _date(cls, value: str) -> str:
        import datetime as dt

        try:
            dt.date.fromisoformat(value)
        except ValueError:
            raise ValueError("дата замера должна быть вида 2026-11-08") from None
        return value


class Prices(_Config):
    accessed: StrictStr = Field(description="Дата обращения к странице цен, ГГГГ-ММ-ДД")
    source: StrictStr = Field(description="Страница цен")
    models: dict[str, ModelPrices]
    web_search_usd_per_1000: Positive
    web_search_query: WebSearchQuery | None = Field(
        None, description="Модель поиска и средний запрос; без неё поиск стоит одну плату за поиск"
    )
    batch_multiplier: Positive
    us_data_residency_multiplier: Positive
    fast_mode: dict[str, FastPrice] = Field(default_factory=dict)

    @field_validator("accessed")
    @classmethod
    def _date(cls, value: str) -> str:
        import datetime as dt

        try:
            dt.date.fromisoformat(value)
        except ValueError:
            raise ValueError("дата обращения должна быть вида 2026-11-07") from None
        return value

    @model_validator(mode="after")
    def _search_model_is_priced(self) -> Prices:
        if self.web_search_query is not None and self.web_search_query.model not in self.models:
            raise ValueError(f"модели поиска {self.web_search_query.model} нет в таблице цен models")
        return self


def _load(name: str, model: type[BaseModel]):
    path = config_dir() / name
    if not path.is_file():
        raise CliError(f"нет файла настроек {name}: ожидается в {path.parent.name}/")
    try:
        data = yaml.safe_load(path.read_bytes().decode("utf-8-sig"))
        return model.model_validate(data)
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValidationError) as exc:
        raise CliError(f"{name} не читается или не по схеме: {_short(exc)}") from exc


def _short(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()[:3])
    return type(exc).__name__


def load_defaults() -> Defaults:
    return _load(DEFAULTS_FILE, Defaults)


def load_prices() -> Prices:
    return _load(PRICES_FILE, Prices)


# ---------------------------------------------------------------------------
# Расход и остаток
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Measure:
    """Лимиты проекта и расход на сейчас. `spent_usd` пусто, если стоимость неизвестна."""

    limit_usd: float | None
    limit_minutes: float
    spent_usd: float | None
    spent_minutes: float

    @property
    def share_usd(self) -> float | None:
        if self.limit_usd is None or self.spent_usd is None:
            return None
        return self.spent_usd / self.limit_usd

    @property
    def share_minutes(self) -> float:
        return self.spent_minutes / self.limit_minutes

    @property
    def share(self) -> float:
        """Доля бюджета, которая кончится раньше: большая из денег и времени."""
        return max(self.share_usd or 0.0, self.share_minutes)

    @property
    def left_usd(self) -> float | None:
        if self.limit_usd is None or self.spent_usd is None:
            return None
        return max(self.limit_usd - self.spent_usd, 0.0)

    @property
    def left_minutes(self) -> float:
        return max(self.limit_minutes - self.spent_minutes, 0.0)


def measure(p: Project) -> Measure:
    b = p.state.budget
    return Measure(
        limit_usd=b.limit_usd,
        limit_minutes=b.limit_minutes,
        spent_usd=proj.cost_total(p.costs)[0],
        spent_minutes=proj.work_seconds(p.journal, at=proj.now()) / 60,
    )


def is_release(action: str | None) -> bool:
    return bool(action) and action.strip().lower() in RELEASE_ACTIONS  # type: ignore[union-attr]


def exhausted_reasons(m: Measure, *, reserve: Reserve | None = None) -> list[str]:
    """Что исчерпано: «деньги», «время». С `reserve` лимиты больше на резерв выпуска."""
    reasons = []
    limit_usd = None if m.limit_usd is None else m.limit_usd + (reserve.usd if reserve else 0.0)
    if limit_usd is not None and m.spent_usd is not None and m.spent_usd >= limit_usd:
        reasons.append("деньги")
    if m.spent_minutes >= m.limit_minutes + (reserve.minutes if reserve else 0.0):
        reasons.append("время")
    return reasons


def warning_text(m: Measure, warn_share: float) -> str | None:
    """Строка статуса: предупреждение с доли `warn_share` и «исчерпан» со 100 %."""
    reasons = exhausted_reasons(m)
    if reasons:
        return "бюджет исчерпан: " + " и ".join(reasons)
    if m.share >= warn_share:
        what = "деньги" if (m.share_usd or 0.0) >= m.share_minutes else "время"
        return f"израсходовано {round(m.share * 100)} % бюджета ({what})"
    return None


TABLE_SOURCE = "оценка по таблице config/defaults.yaml, истории проекта нет"


@dataclass(frozen=True)
class NextStep:
    """Оценка шага: деньги и минуты и откуда они взяты."""

    step: str
    usd: float
    minutes: float
    usd_source: str
    minutes_source: str

    @property
    def source(self) -> str:
        if self.usd_source == self.minutes_source:
            return self.usd_source
        return f"деньги: {self.usd_source}; время: {self.minutes_source}"

    @property
    def estimated(self) -> bool:
        """Хоть одно число — по таблице, а не по истории проекта."""
        return TABLE_SOURCE in (self.usd_source, self.minutes_source)


def next_step_of(p: Project) -> str:
    """Следующий шаг по `status`: после выполненного — следующий; начатый или прерванный — он сам."""
    step = p.state.step
    name = proj.step_action_name(step)
    last = next((e.status for e in reversed(p.journal) if proj.is_step_action(e) and e.name == name), None)
    if last != "выполнено":
        return step
    i = STEPS.index(step) if step in STEPS else len(STEPS) - 1
    return STEPS[min(i + 1, len(STEPS) - 1)]  # после шага 8 пересмотр повторяется


def _step_of(e: JournalEvent) -> str:
    return (e.name or "").removeprefix("шаг ")


def _history(p: Project) -> tuple[dict[str, float], dict[str, float]]:
    """Деньги и минуты каждого завершённого шага проекта: сообщения из `cost.jsonl` и записи журнала."""
    from strateg.costreport import breakdown_records  # импорт здесь: costreport импортирует этот модуль

    steps = [e for e in proj.own_events(p.journal) if proj.is_step_action(e)]
    done = {_step_of(e) for e in steps if e.status == "выполнено"}
    usd: dict[str, float] = {}
    for r in breakdown_records(p.costs):
        if r.entry == "по сообщению" and r.step in done and r.cost_usd is not None:
            usd[r.step] = usd.get(r.step, 0.0) + r.cost_usd
    minutes: dict[str, float] = {}
    for e in steps:
        if e.status in ("выполнено", "прервано") and _step_of(e) in done:
            minutes[_step_of(e)] = minutes.get(_step_of(e), 0.0) + (e.duration_s or 0.0) / 60
    return usd, minutes


def _median(values: dict[str, float]) -> tuple[float, str]:
    return statistics.median(
        values.values()
    ), f"медиана {len(values)} завершённых шагов проекта: {', '.join(values)}"


def estimate_step(p: Project, step: str, table: dict[str, StepEstimate]) -> NextStep:
    """Цена шага: медиана по завершённым шагам проекта; истории нет — таблица `step_estimate`."""
    usd_hist, min_hist = _history(p)
    row = table.get(step) or _default_step_estimate()[step]
    usd, usd_source = _median(usd_hist) if usd_hist else (row.usd, TABLE_SOURCE)
    minutes, minutes_source = _median(min_hist) if min_hist else (row.minutes, TABLE_SOURCE)
    return NextStep(step, usd, minutes, usd_source, minutes_source)


def _usd(x: float) -> str:
    return f"${x:.2f}".replace(".", ",")


def _min(x: float) -> str:
    return f"{x:.1f}".replace(".", ",")


def summary_text(m: Measure) -> str:
    """«деньги: потрачено $5,12 из $5,00; время: 61,0 из 60 мин» — для отказа и события журнала."""
    spent = "неизвестно" if m.spent_usd is None else _usd(m.spent_usd)
    limit = "без лимита" if m.limit_usd is None else _usd(m.limit_usd)
    return f"деньги: потрачено {spent} из {limit}; время: {_min(m.spent_minutes)} из {m.limit_minutes:g} мин"


def remaining_text(m: Measure) -> str:
    """«осталось $3,30 из $5 и 45,0 из 60 мин»; без стоимости — только время."""
    time_part = f"{_min(m.left_minutes)} из {m.limit_minutes:g} мин"
    if m.limit_usd is None:
        return f"осталось {time_part}; лимита денег нет"
    if m.left_usd is None:
        return f"осталось {time_part}; деньги не считаются: стоимость неизвестна"
    return f"осталось {_usd(m.left_usd)} из ${m.limit_usd:g} и {time_part}".replace(".", ",")


def as_dict(m: Measure, warn_share: float | None) -> dict[str, object]:
    return {
        "limit_usd": m.limit_usd,
        "limit_minutes": m.limit_minutes,
        "spent_usd": m.spent_usd,
        "spent_minutes": round(m.spent_minutes, 2),
        "remaining_usd": None if m.left_usd is None else round(m.left_usd, 4),
        "remaining_minutes": round(m.left_minutes, 2),
        "share": round(m.share, 4),
        "exhausted": exhausted_reasons(m),
        "warning": warning_text(m, warn_share) if warn_share is not None else None,
        "text": remaining_text(m),
    }

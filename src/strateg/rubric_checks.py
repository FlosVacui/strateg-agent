"""Признаки части А рубрики полноты: каждый проверяет код, без модели (ADR-0006, шаг 6).

Реестр `CHECKS` ставит имя `cli_check` из рубрики в функцию. Шаг и текст признака берутся из
рубрики (`knowledge/rubrics/completeness.yaml`) и здесь не дублируются; здесь только то, что делает
проверку: какие поля и связи смотреть. Проверка не падает на неполном проекте: нет файла — «нет» с
подсказкой, какой файл и каким шагом пишется.

Документы читаются по заголовкам `## <название>` (названия — как в `knowledge/templates/`) и по
спискам; разбор Markdown — регулярными выражениями. Слова предметной области (А-22) сравниваются по
леммам pymorphy3.
"""

from __future__ import annotations

import re
import typing
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from strateg import project as proj
from strateg.models import (
    Claim,
    ClaimsFile,
    FrameFile,
    GapsFile,
    JournalEvent,
    MetricKind,
    OptionsFile,
    PlanFile,
    ProjectState,
    PromisesFile,
    SourcesFile,
    VerificationFile,
    norm,
)
from strateg.project import CliError
from strateg.project_check import (
    CLAIMS_FILE,
    FRAME_FILE,
    GAPS_FILE,
    JOURNAL_FILE,
    OPTIONS_FILE,
    PLAN_FILE,
    PROMISES_FILE,
    SOURCES_FILE,
    STATE_FILE,
    VERIFICATION_FILE,
    _format_errors,
    _same_threshold,
    check_project,
)

# Порядок шагов: признак рубрики выполняется к своему шагу и ко всем следующим.
STEP_ORDER = ("1", "2а", "2б", "3", "4", "5", "6", "7", "8")
STEP_INDEX = {s: i for i, s in enumerate(STEP_ORDER)}

SITUATION = "outputs/situation.md"
PRODUCT_STRATEGY = "outputs/product-strategy.md"
COMMS_STRATEGY = "outputs/comms-strategy.md"
ONE_PAGER = "outputs/one-pager.md"

# Как и каким шагом пишется файл: подсказка в сообщении «нет», если файла нет.
HOW_WRITTEN: dict[str, tuple[str, str]] = {
    FRAME_FILE: ("запишите командой `strateg write` (вид frame)", "1"),
    PLAN_FILE: ("запишите командой `strateg write` (вид plan)", "2а"),
    CLAIMS_FILE: ("утверждения пишет `strateg claim add`", "2б"),
    GAPS_FILE: ("запишите командой `strateg write` (вид gaps)", "3"),
    OPTIONS_FILE: ("запишите командой `strateg write` (вид options)", "4"),
    PROMISES_FILE: ("запишите командой `strateg write` (вид promises)", "5"),
    VERIFICATION_FILE: ("запишите командой `strateg write` (вид verification)", "5"),
    SITUATION: ("документ по шаблону situation.md", "3"),
    PRODUCT_STRATEGY: ("документ по шаблону product-strategy.md", "5"),
    COMMS_STRATEGY: ("документ по шаблону comms-strategy.md", "5"),
    ONE_PAGER: ("документ по шаблону one-pager.md", "7"),
}

SEC_KNOWN = "Известно"
SEC_DISPUTES = "Где источники спорят"
SEC_MESSAGES = "Ключевые сообщения"
SEC_CHANNELS = "Роли каналов"
PRODUCT_SECTIONS = (
    "Аудитория и её задача",
    "Альтернативы",
    "Направление",
    "Осознанные отказы",
    "Польза, затраты и доходы",
)
COMMS_SECTIONS = ("Аудитория и ситуация", "Барьер", SEC_MESSAGES, SEC_CHANNELS)

# Ворота и шаг, к которому они уже пройдены (С-1, С-13).
GATE_DUE = {"В1": "2а", "В2": "2б", "В3": "4", "В4": "5", "В5": "8"}
STOP_ACTION = "внеплановая остановка"
MAX_LISTED = 12  # сколько замечаний одного признака показывать


# ---------------------------------------------------------------------------
# Реестр
# ---------------------------------------------------------------------------

Check = Callable[["Ctx"], list[str]]
CHECKS: dict[str, Check] = {}


def check(name: str) -> Callable[[Check], Check]:
    def register(fn: Check) -> Check:
        CHECKS[name] = fn
        return fn

    return register


@dataclass
class Result:
    """Итог одного признака: «да» (замечаний нет) или «нет» и что дополнить."""

    id: str
    check: str
    step: str
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def message(self) -> str | None:
        return "; ".join(self.problems) if self.problems else None


# ---------------------------------------------------------------------------
# Разбор Markdown
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"^\s*(```|~~~)")
_BULLETS = "-*+•–—"
_LIST_START = re.compile(rf"^(?:[{_BULLETS}]|\d+[.)])\s+\S")
_TABLE_ROW = re.compile(r"^\s*\|")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_PLACEHOLDER_SPAN = re.compile(r"<[^<>]*>")
_MD_MARKS = re.compile(r"[*_`]+")
_WORDS = re.compile(r"[^\W_]+")
# Слова-заглушки: раздел из одного такого слова остался незаполненным.
_STUB_WORDS = frozenset({"todo", "tbd", "fixme", "xxx", "нет", "заполнить"})


def sections(text: str) -> dict[str, str]:
    """Разделы `## Название` -> тело раздела (до следующего `## `). Заголовки внутри блоков кода не в счёт."""
    out: dict[str, list[str]] = {}
    current: str | None = None
    fenced = False
    for line in text.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
        m = None if fenced else re.match(r"^##[ \t]+(.+?)[ \t]*$", line)
        if m:
            current = m.group(1)
            out.setdefault(current, [])
        elif current is not None:
            out[current].append(line)
    return {k: "\n".join(v).strip("\n") for k, v in out.items()}


def find_section(text: str, name: str) -> str | None:
    """Тело раздела с таким названием; к названию допустим хвост после двоеточия, тире или скобки."""
    want = norm(name).casefold()
    for title, body in sections(text).items():
        t = norm(title).casefold()
        if t == want or (t.startswith(want) and not t[len(want)].isalnum()):
            return body
    return None


def is_empty_body(body: str | None) -> bool:
    """Пусто: нет ни одной буквы или цифры вне мест для текста `<...>`, либо остались только слова-заглушки
    («TODO», «нет»). Так пуст и раздел из нескольких строк-заглушек шаблона."""
    words = [w.casefold() for w in _WORDS.findall(_PLACEHOLDER_SPAN.sub(" ", body or ""))]
    return not words or all(w in _STUB_WORDS for w in words)


def list_items(body: str) -> list[str]:
    """Пункты раздела: строки списка (с вложенными и продолжениями) и строки таблицы без заголовка."""
    items: list[str] = []
    table: list[str] = []
    in_list = False  # последний пункт — строка списка: ему можно дописывать продолжение
    blank_after_list = False

    def flush_table() -> None:
        if table:
            rows = [r for r in table if not _TABLE_SEP.match(r)]
            has_header = len(table) > 1 and _TABLE_SEP.match(table[1]) is not None
            items.extend(rows[1:] if has_header else rows)
            table.clear()

    for line in body.splitlines():
        if _TABLE_ROW.match(line):
            table.append(line)
            in_list = False
            blank_after_list = True
            continue
        flush_table()
        if not line.strip():
            blank_after_list = True
            continue
        indented = line.startswith(("\t", "  "))
        if _LIST_START.match(line):
            items.append(line.strip())
            in_list = True
            blank_after_list = False
        elif in_list and indented:
            items[-1] += " " + line.strip()  # вложенный пункт, продолжение (в том числе после пустой строки)
        elif in_list and not blank_after_list:
            items[-1] += " " + line.strip()  # продолжение пункта без отступа
        else:
            in_list = False
            blank_after_list = True
    flush_table()
    return items


def short(text: str, n: int = 60) -> str:
    plain = " ".join(_MD_MARKS.sub("", text).split())
    return plain if len(plain) <= n else plain[: n - 1].rstrip() + "…"


_TOKEN = re.compile(r"(?<![\w-])([A-Za-zА-Яа-яЁё]{1,3})-([0-9]+)(?!\d)")
# Номер не по форме: без дефиса («У99», только русские буквы) или с пробелом у дефиса («У- 99», «У -99»).
_SLOPPY = re.compile(r"(?<![\w-])([A-Za-zА-Яа-яЁё]{1,3})(?:-[ \t]+|[ \t]+-|)([0-9]+)(?!\d)")
_DASH_BEFORE_DIGIT = re.compile(r"(?<=[A-Za-zА-Яа-яЁё])[‐-―−](?=\d)")
_TO_CYR = str.maketrans("abcehkmoptxyABCEHKMOPTXY", "авсенкмортхуАВСЕНКМОРТХУ")
PREFIXES = {
    "И": "sources",
    "У": "claims",
    "П": "gaps",
    "ВП": "plan",
    "Вар": "options",
    "ПП": "rows",
    "О": "promises",
}
PROMISE_DOCS = frozenset({COMMS_STRATEGY, ONE_PAGER})


def dashes(text: str) -> str:
    """Неразрывный дефис, короткое и длинное тире и минус между буквами и цифрой номера читаются как «-»."""
    return _DASH_BEFORE_DIGIT.sub("-", text)


def _sloppy(text: str) -> list[tuple[str, str, str]]:
    """Номера не по форме: (как написано, префикс по-русски, число). Строгая запись `У-99` не в счёт."""
    out: list[tuple[str, str, str]] = []
    for m in _SLOPPY.finditer(text):
        letters, n = m.groups()
        cyr = letters.translate(_TO_CYR)
        if cyr in PREFIXES and (m.group(0).count("-") or letters == cyr):
            out.append((m.group(0), cyr, n))
    return out


def refs(text: str, prefix: str) -> list[str]:
    """Номера вида `У-3` (буквы русские) в порядке появления, без повторов."""
    text = dashes(text)
    found = re.findall(rf"(?<![\w-]){re.escape(prefix)}-([0-9]+)(?!\d)", text)
    return list(dict.fromkeys(f"{prefix}-{n}" for n in found))


# ---------------------------------------------------------------------------
# Контекст проверки
# ---------------------------------------------------------------------------


class Ctx:
    """Папка проекта и разобранные файлы; каждый файл читается один раз."""

    def __init__(self, root: Path, step: str) -> None:
        self.root = root
        self.step = step
        self._models: dict[str, tuple[Any, list[str]]] = {}
        self._texts: dict[str, str | None] = {}
        self._journal: list[JournalEvent] | None = None
        self._state: ProjectState | None = None

    # --- файлы ---
    def missing(self, rel: str) -> str:
        how, step = HOW_WRITTEN.get(rel, ("", ""))
        tail = f": {how}, шаг {step}" if how else ""
        return f"нет файла {rel}{tail}"

    def model(self, rel: str, cls: type[BaseModel], sections_: tuple[str, ...] = ()) -> tuple[Any, list[str]]:
        """(модель, замечания): файла нет или он не читается — модели нет, замечание говорит что делать."""
        if rel not in self._models:
            self._models[rel] = self._load(rel, cls, sections_)
        return self._models[rel]

    def _load(self, rel: str, cls: type[BaseModel], sections_: tuple[str, ...]) -> tuple[Any, list[str]]:
        path = self.root / rel
        if not path.is_file():
            return None, [self.missing(rel)]
        try:
            raw = proj.read_yaml(path)
        except CliError as err:
            return None, [err.message]
        try:
            return cls.model_validate(raw, by_name=False), []
        except ValidationError as exc:
            errors = _format_errors(rel, sections_, raw, exc)
            more = f" (и ещё {len(errors) - 2})" if len(errors) > 2 else ""
            return None, [f"{rel} не по схеме, исправьте: " + "; ".join(errors[:2]) + more]

    def frame(self) -> tuple[FrameFile | None, list[str]]:
        return self.model(FRAME_FILE, FrameFile)

    def plan(self) -> tuple[PlanFile | None, list[str]]:
        return self.model(PLAN_FILE, PlanFile, ("questions",))

    def claims(self) -> tuple[ClaimsFile | None, list[str]]:
        return self.model(CLAIMS_FILE, ClaimsFile, ("claims",))

    def sources(self) -> tuple[SourcesFile | None, list[str]]:
        return self.model(SOURCES_FILE, SourcesFile, ("sources",))

    def gaps(self) -> tuple[GapsFile | None, list[str]]:
        return self.model(GAPS_FILE, GapsFile, ("gaps", "metric_coverage"))

    def options(self) -> tuple[OptionsFile | None, list[str]]:
        return self.model(OPTIONS_FILE, OptionsFile, ("options",))

    def promises(self) -> tuple[PromisesFile | None, list[str]]:
        return self.model(PROMISES_FILE, PromisesFile, ("promises",))

    def verification(self) -> tuple[VerificationFile | None, list[str]]:
        return self.model(VERIFICATION_FILE, VerificationFile, ("rows", "premortem", "undesired_effects"))

    def text(self, rel: str) -> str | None:
        if rel not in self._texts:
            path = self.root / rel
            try:
                self._texts[rel] = path.read_bytes().decode("utf-8-sig") if path.is_file() else None
            except (OSError, UnicodeDecodeError):
                self._texts[rel] = None
        return self._texts[rel]

    def doc(self, rel: str) -> tuple[str | None, list[str]]:
        text = self.text(rel)
        return text, ([] if text is not None else [self.missing(rel)])

    def outputs(self) -> list[str]:
        folder = self.root / "outputs"
        if not folder.is_dir():
            return []
        return [f"outputs/{p.name}" for p in sorted(folder.glob("*.md")) if p.is_file()]

    @property
    def journal(self) -> list[JournalEvent]:
        if self._journal is None:
            try:
                self._journal = proj.read_jsonl(self.root / JOURNAL_FILE, JournalEvent)
            except (ValueError, OSError):
                self._journal = []
        return self._journal

    @property
    def state(self) -> ProjectState | None:
        if self._state is None:
            try:
                self._state = ProjectState.model_validate(proj.read_yaml(self.root / STATE_FILE))
            except (CliError, ValueError, OSError):
                return None
        return self._state


def _first(problems: list[str]) -> list[str]:
    return problems[:MAX_LISTED] + (
        [f"и ещё замечаний: {len(problems) - MAX_LISTED}"] if len(problems) > MAX_LISTED else []
    )


def _latin_note(text: str) -> str:
    """Подсказка, если в тексте есть номер с латинскими буквами."""
    for letters, n in _TOKEN.findall(dashes(text)):
        cyr = letters.translate(_TO_CYR)
        if letters != cyr and cyr in PREFIXES:
            return f" (номер {letters}-{n} написан латинскими буквами, нужны русские)"
    return ""


# ---------------------------------------------------------------------------
# А-1 … А-6: файлы, рамка, ворота, план
# ---------------------------------------------------------------------------


@check("required_files_present")
def required_files_present(c: Ctx) -> list[str]:
    out: list[str] = []
    for load in (c.frame, c.plan, c.gaps, c.options, c.promises, c.verification):
        out += load()[1]
    return out


@check("frame_complete")
def frame_complete(c: Ctx) -> list[str]:
    _, out = c.frame()
    answered = [e for e in c.journal if e.gate and e.gate.gate == "В1" and e.gate.answered_at]
    if not answered:
        out.append("в журнале нет отвеченного события ворот В1: ответьте `strateg gate answer В1`")
    elif answered[-1].gate and answered[-1].gate.frame is None:
        out.append(
            "событие ответа на В1 не хранит рамку (gate.frame): ответьте на В1 заново после записи рамки"
        )
    return out


def _gate_statuses(gate: str, mode: str) -> tuple[str, ...]:
    if gate in ("В1", "В4") or mode == "пилот":
        return ("отвечено",)
    return ("пройдены автоматически", "отвечено")


@check("gates_marked")
def gates_marked(c: Ctx) -> list[str]:
    state = c.state
    if state is None:
        return [f"{STATE_FILE} не читается: запустите `strateg check`"]
    out: list[str] = []
    here = STEP_INDEX.get(c.step, 0)
    for gate, due in GATE_DUE.items():
        if STEP_INDEX[due] > here:
            continue
        events = [e.gate for e in c.journal if e.gate and e.gate.gate == gate]
        have = state.gates.get(gate)  # type: ignore[call-overload]
        if not events:
            out.append(f"в журнале нет события ворот {gate}: откройте и пройдите ворота {gate}")
            continue
        want = _gate_statuses(gate, events[-1].mode)
        if have not in want:
            wanted = "» или «".join(want)
            out.append(
                f"{STATE_FILE}, поле gates.{gate}: «{have}», а ворота открыты в режиме "
                f"«{events[-1].mode}»: нужно «{wanted}»"
            )
    return out


@check("gate_events_b1_b4")
def gate_events_b1_b4(c: Ctx) -> list[str]:
    out: list[str] = []
    b1 = [e.gate for e in c.journal if e.gate and e.gate.gate == "В1"]
    if not any(g.answered_at and g.questions <= 5 for g in b1):
        why = "нет события ворот В1" if not b1 else "нет отвеченного В1 с числом вопросов не больше 5"
        out.append(f"{JOURNAL_FILE}: {why}: на В1 задают не больше 5 вопросов и записывают ответ")
    if not any(e.gate and e.gate.gate == "В4" and e.gate.answered_at for e in c.journal):
        out.append(
            f"{JOURNAL_FILE}: нет отвеченного события ворот В4: выбор варианта записывают ответом на В4"
        )
    return out


@check("plan_complete")
def plan_complete(c: Ctx) -> list[str]:
    plan, out = c.plan()
    if plan is None:
        return out
    if plan.estimate.usd is None and plan.estimate.minutes is None:
        out.append(f"{PLAN_FILE}, поле estimate: пусто, укажите минуты или доллары")
    for q in plan.questions:
        if q.status == "открыт":
            out.append(
                f"{PLAN_FILE}, запись {q.id}: статус «открыт»; закройте вопрос утверждениями "
                "или переведите в «пробел» с пробелом П-n"
            )
    return out


@check("plan_questions_have_claims")
def plan_questions_have_claims(c: Ctx) -> list[str]:
    plan, out = c.plan()
    claims, errs = c.claims()
    if plan is None or claims is None:
        return out + errs
    linked = {(cl.plan_question or "").strip() for cl in claims.claims if cl.status == "действует"}
    stale = {(cl.plan_question or "").strip(): cl for cl in claims.claims if cl.status != "действует"}
    for q in plan.questions:
        if q.status == "закрыт" and q.id not in linked:
            hint = ""
            if q.id in stale:
                old = stale[q.id]
                hint = f"; утверждение {old.id} {_status_note(old)}: запишите действующее"
            out.append(
                f"{PLAN_FILE}, запись {q.id}: статус «закрыт», а в {CLAIMS_FILE} нет действующего "
                f"утверждения с plan_question: {q.id}{hint}"
            )
    return out


# ---------------------------------------------------------------------------
# А-7 … А-9: пробелы и картина
# ---------------------------------------------------------------------------


@check("metric_coverage_complete")
def metric_coverage_complete(c: Ctx) -> list[str]:
    gaps, out = c.gaps()
    claims, errs = c.claims()
    if gaps is None:
        return out
    by_id = {cl.id: cl for cl in claims.claims} if claims else {}
    have = {m.kind for m in gaps.metric_coverage}
    for kind in typing.get_args(MetricKind):
        if kind not in have:
            out.append(f"{GAPS_FILE}, metric_coverage: нет показателя «{kind}»")
    if claims is None:
        return out + errs
    for m in gaps.metric_coverage:
        for cid in m.claims or []:
            cl = by_id.get(cid)
            if cl is None:
                out.append(f"{GAPS_FILE}, показатель «{m.kind}»: утверждения {cid} нет в {CLAIMS_FILE}")
            elif cl.type not in ("факт", "прогноз"):
                out.append(
                    f"{GAPS_FILE}, показатель «{m.kind}»: {cid} имеет тип «{cl.type}», "
                    "нужен «факт» или «прогноз»"
                )
    return out


def _status_note(cl: Claim) -> str:
    """«отозвано» или «заменено на У-5»: почему ссылка на утверждение больше не годится."""
    return f"заменено на {cl.replaced_by}" if cl.status == "заменено" and cl.replaced_by else cl.status


def _claim_ids(c: Ctx) -> tuple[dict[str, Claim] | None, list[str]]:
    claims, errs = c.claims()
    return ({cl.id: cl for cl in claims.claims} if claims else None), errs


def _section(c: Ctx, rel: str, name: str) -> tuple[str | None, list[str]]:
    text, errs = c.doc(rel)
    if text is None:
        return None, errs
    body = find_section(text, name)
    if body is None:
        return None, [f"{rel}: нет раздела «{name}» (заголовок `## {name}`)"]
    return body, []


@check("known_items_have_claims")
def known_items_have_claims(c: Ctx) -> list[str]:
    body, out = _section(c, SITUATION, SEC_KNOWN)
    ids, errs = _claim_ids(c)
    if body is None:
        return out
    if ids is None:
        return errs
    items = list_items(body)
    if not items:
        out.append(f"{SITUATION}, раздел «{SEC_KNOWN}»: нет пунктов списка; каждый пункт ссылается на У-n")
    for n, item in enumerate(items, start=1):
        named = refs(item, "У")
        label = f"{SITUATION}, раздел «{SEC_KNOWN}», пункт {n} («{short(item)}»)"
        if not named:
            out.append(f"{label}: нет номера утверждения У-n{_latin_note(item)}")
        else:
            for r in named:
                if r not in ids:
                    out.append(f"{label}: {r} нет в {CLAIMS_FILE}")
                elif ids[r].status != "действует":
                    out.append(
                        f"{label}: {r} {_status_note(ids[r])}, замените ссылку на действующее утверждение"
                    )
    return _first(out)


def _item_pair(item: str) -> list[str]:
    return refs(item, "У")[:2]


@check("disputes_recorded")
def disputes_recorded(c: Ctx) -> list[str]:
    body, out = _section(c, SITUATION, SEC_DISPUTES)
    claims, errs = c.claims()
    gaps, gerrs = c.gaps()
    if body is None:
        return out
    if claims is None:
        return errs
    by_id = {cl.id: cl for cl in claims.claims}
    recorded = {frozenset((cl.id, cl.dispute.claim)) for cl in claims.claims if cl.dispute is not None}
    items = [] if norm(body).strip(" .").casefold() in ("", "нет") else list_items(body)
    items = [i for i in items if norm(i).strip(" .-*").casefold() != "нет"]
    named_gaps: set[str] = set()
    named_pairs: list[set[str]] = []
    for item in items:
        pair = _item_pair(item)
        label = f"{SITUATION}, раздел «{SEC_DISPUTES}», «{short(item)}»"
        if len(pair) < 2:
            out.append(f"{label}: не названа пара утверждений У-n и У-m")
            continue
        a, b = pair
        named_pairs.append({a, b})
        missing = [p for p in pair if p not in by_id]
        if missing:
            out.append(f"{label}: {', '.join(missing)} нет в {CLAIMS_FILE}")
        elif frozenset(pair) not in recorded:
            out.append(
                f"{label}: пара {a} и {b} не записана в {CLAIMS_FILE}; допишите полем dispute у {a} или {b}"
            )
        named_gaps.update(refs(item, "П"))
    if gaps is None:
        return _first(out + gerrs)
    for g in gaps.gaps:
        if g.reason != "источники спорят" or g.id in named_gaps:
            continue
        in_text = set(refs(g.text, "У"))  # пара названа в самом пробеле: П-n в пункте раздела не нужен
        if any(pair <= in_text for pair in named_pairs):
            continue
        out.append(
            f"{GAPS_FILE}, запись {g.id}: причина «источники спорят», а в разделе «{SEC_DISPUTES}» "
            f"{SITUATION} нет пункта с парой утверждений, названной в тексте пробела, или с {g.id}"
        )
    return _first(out)


# ---------------------------------------------------------------------------
# А-10 … А-19: стратегии, гипотезы, план проверки
# ---------------------------------------------------------------------------


def _role_of(item: str) -> str:
    """Роль канала: текст после названия канала (после тире, двоеточия или во второй ячейке таблицы)."""
    if _TABLE_ROW.match(item):
        cells = [x.strip() for x in item.strip().strip("|").split("|")]
        return cells[1] if len(cells) > 1 else ""
    plain = " ".join(_MD_MARKS.sub("", item).split())
    plain = re.sub(rf"^(?:[{_BULLETS}]|\d+[.)])\s+", "", plain)
    m = re.search(r"\s[—–-]\s|:\s", plain)
    return plain[m.end() :].strip() if m else ""


@check("messages_confirmed_channels_roled")
def messages_confirmed_channels_roled(c: Ctx) -> list[str]:
    out: list[str] = []
    claims, errs = c.claims()
    msgs, merrs = _section(c, COMMS_STRATEGY, SEC_MESSAGES)
    chans, cerrs = _section(c, COMMS_STRATEGY, SEC_CHANNELS)
    if claims is None:
        return errs
    by_id = {cl.id: cl for cl in claims.claims}
    if msgs is None:
        out += merrs
    else:
        items = list_items(msgs)
        if not items:
            out.append(f"{COMMS_STRATEGY}, раздел «{SEC_MESSAGES}»: нет ключевых сообщений (пунктов списка)")
        for n, item in enumerate(items, start=1):
            named = refs(item, "У")
            facts = [
                r for r in named if r in by_id and by_id[r].type == "факт" and by_id[r].status == "действует"
            ]
            if not facts:
                stale = [
                    f"{r} {_status_note(by_id[r])}"
                    for r in named
                    if r in by_id and by_id[r].status != "действует"
                ]
                tail = f"; {', '.join(stale)}: замените ссылку на действующий факт" if stale else ""
                out.append(
                    f"{COMMS_STRATEGY}, сообщение {n} («{short(item)}»): нет номера действующего "
                    f"утверждения У-n типа «факт», которое его подтверждает{tail}{_latin_note(item)}"
                )
    if chans is None:
        out += cerrs
    else:
        items = list_items(chans)
        if not items:
            out.append(f"{COMMS_STRATEGY}, раздел «{SEC_CHANNELS}»: нет каналов (пунктов списка)")
        for n, item in enumerate(items, start=1):
            if is_empty_body(_role_of(item)):
                out.append(f"{COMMS_STRATEGY}, канал {n} («{short(item)}»): не названа роль канала")
    return _first(out)


def _search_key(query: str) -> str:
    return " ".join(norm(query).casefold().split())


@check("gaps_searched_twice")
def gaps_searched_twice(c: Ctx) -> list[str]:
    gaps, out = c.gaps()
    if gaps is None:
        return out
    for g in gaps.gaps:
        if not (g.affects_choice or g.close_via == "материал руководителя"):
            continue
        queries = {_search_key(s.query) for s in g.searched if s.date}
        if len(queries) < 2:
            out.append(
                f"{GAPS_FILE}, запись {g.id}: в searched {len(queries)} запрос(а) с датой, нужно не меньше "
                "двух с разными query"
            )
    return out


@check("gaps_link_options")
def gaps_link_options(c: Ctx) -> list[str]:
    gaps, out = c.gaps()
    opts, errs = c.options()
    if gaps is None:
        return out
    if opts is None:
        return errs
    known = {o.id for o in opts.options}
    for g in gaps.gaps:
        if g.affects_choice and g.status == "открыт":
            if not g.affects_options:
                out.append(
                    f"{GAPS_FILE}, запись {g.id}: влияет на выбор, а affects_options пуст: назовите Вар-n"
                )
            out += [
                f"{GAPS_FILE}, запись {g.id}, поле affects_options: {o} нет в {OPTIONS_FILE}"
                for o in g.affects_options
                if o not in known
            ]
    return out


@check("chosen_set")
def chosen_set(c: Ctx) -> list[str]:
    opts, out = c.options()
    if opts is None:
        return out
    if opts.chosen is None:
        return [f"{OPTIONS_FILE}, поле chosen: пусто; запишите выбор человека на В4"]
    if opts.chosen not in {o.id for o in opts.options}:
        return [f"{OPTIONS_FILE}, поле chosen: {opts.chosen} не вариант из options"]
    return []


def _walk_strings(value: Any, path: tuple[Any, ...] = ()) -> Iterable[tuple[tuple[Any, ...], str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _walk_strings(v, (*path, k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _walk_strings(v, (*path, i))


def _only_foreign_objection_basis(opts: OptionsFile, claim_id: str) -> bool:
    """Гипотеза встречается в options.yaml, и только как basis возражений вариантов, отличных от chosen."""
    if opts.chosen is None:
        return False
    raw = opts.model_dump(mode="json", by_alias=True, exclude_none=True)
    seen = False
    pattern = re.compile(rf"(?<![\w-]){re.escape(claim_id)}(?!\d)")
    for path, text in _walk_strings(raw):
        if not pattern.search(text):
            continue
        seen = True
        foreign_basis = (
            len(path) == 5
            and path[0] == "options"
            and path[2] == "objections"
            and path[4] == "basis"
            and text.strip() == claim_id
            and raw["options"][path[1]]["id"] != opts.chosen
        )
        if not foreign_basis:
            return False
    return seen


@check("hypotheses_have_rows")
def hypotheses_have_rows(c: Ctx) -> list[str]:
    claims, out = c.claims()
    ver, verrs = c.verification()
    gaps, gerrs = c.gaps()
    opts, oerrs = c.options()
    if claims is None:
        return out
    if ver is None:
        return verrs
    if gaps is None:
        return gerrs
    named = {r.hypothesis for r in ver.rows if r.hypothesis}
    named |= {m.claim for m in ver.premortem}
    named |= {g.hypothesis for g in gaps.gaps if g.hypothesis}
    for cl in claims.claims:
        if cl.type != "гипотеза" or cl.status != "действует" or cl.id in named:
            continue
        if opts is not None and _only_foreign_objection_basis(opts, cl.id):
            continue
        out.append(
            f"{CLAIMS_FILE}, запись {cl.id}: гипотеза действует, а строки плана проверки (rows[].hypothesis "
            f"или premortem[].claim в {VERIFICATION_FILE}) и пробела (gaps[].hypothesis) для неё нет"
        )
    return out


@check("change_mind_in_plan")
def change_mind_in_plan(c: Ctx) -> list[str]:
    opts, out = c.options()
    ver, errs = c.verification()
    if opts is None:
        return out
    if ver is None:
        return errs
    for n, cm in enumerate(opts.recommendation.change_mind, start=1):
        rows = [r for r in ver.rows if r.from_change_mind == n]
        if not rows:
            out.append(
                f"{OPTIONS_FILE}, recommendation.change_mind №{n} («{short(cm.metric)}»): "
                f"в {VERIFICATION_FILE} "
                f"нет строки с from_change_mind: {n}"
            )
        elif not any(_same_threshold(r.change_mind.threshold, cm.threshold) for r in rows):
            out.append(
                f"{VERIFICATION_FILE}, строка {rows[0].id}: порог «{rows[0].change_mind.threshold}» "
                "не совпадает "
                f"с порогом «{cm.threshold}» записи change_mind №{n} из {OPTIONS_FILE}"
            )
    return out


@check("premortem_defensive")
def premortem_defensive(c: Ctx) -> list[str]:
    ver, out = c.verification()
    claims, errs = c.claims()
    if ver is None:
        return out
    if claims is None:
        return errs
    kind = {cl.id: cl.type for cl in claims.claims}
    rows = {r.id: r for r in ver.rows}
    if len(ver.premortem) != 3:
        out.append(f"{VERIFICATION_FILE}, premortem: причин {len(ver.premortem)}, нужно ровно три")
    ids = [m.row for m in ver.premortem]
    if len(set(ids)) != len(ids):
        out.append(f"{VERIFICATION_FILE}, premortem: у причин одинаковая строка row, нужны три разные")
    for n, m in enumerate(ver.premortem, start=1):
        row = rows.get(m.row)
        if row is None:
            out.append(f"{VERIFICATION_FILE}, premortem №{n}: строки {m.row} нет среди rows")
        elif row.kind != "защитный":
            out.append(
                f"{VERIFICATION_FILE}, premortem №{n}: строка {m.row} имеет kind «{row.kind}», "
                "нужен «защитный»"
            )
        if kind.get(m.claim) != "гипотеза":
            out.append(
                f"{VERIFICATION_FILE}, premortem №{n}: {m.claim} — не гипотеза "
                f"(тип «{kind.get(m.claim, 'нет в ' + CLAIMS_FILE)}»)"
            )
    return out


@check("undesired_effects_present")
def undesired_effects_present(c: Ctx) -> list[str]:
    ver, out = c.verification()
    if ver is None:
        return out
    if not ver.undesired_effects:
        return [
            f"{VERIFICATION_FILE}, undesired_effects: пусто; "
            "запишите нежелательные последствия со строкой row"
        ]
    rows = {r.id for r in ver.rows}
    return [
        f"{VERIFICATION_FILE}, undesired_effects №{n}: строки {e.row} нет среди rows"
        for n, e in enumerate(ver.undesired_effects, start=1)
        if e.row not in rows
    ]


@check("strategy_sections_filled")
def strategy_sections_filled(c: Ctx) -> list[str]:
    out: list[str] = []
    for rel, names in ((PRODUCT_STRATEGY, PRODUCT_SECTIONS), (COMMS_STRATEGY, COMMS_SECTIONS)):
        text, errs = c.doc(rel)
        if text is None:
            out += errs
            continue
        for name in names:
            body = find_section(text, name)
            if body is None:
                out.append(f"{rel}: нет раздела «{name}» (заголовок `## {name}`)")
            elif is_empty_body(body):
                out.append(f"{rel}: раздел «{name}» пуст")
    return out


_BRACKET = re.compile(r"\[([^\[\]\n]*)\]")
_MARK = re.compile(r"(?<![\w-])([OО])-([0-9]+)(?!\d)")
_MARK_LOOSE = re.compile(r"(?<![\w-])[OО][ \t]*-?[ \t]*[0-9]+")


def _marks(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Метки обещаний в квадратных скобках: ([(буква, номер)], скобки неразборной формы).

    В одних скобках может стоять несколько номеров: `[О-2, О-1]`, `[О-2; О-1]`. Скобка, в которой
    есть что-то похожее на номер обещания, но не `О-n` («[О2]», «[О 2]»), — неразборной формы.
    """
    marks: list[tuple[str, str]] = []
    bad: list[str] = []
    for m in _BRACKET.finditer(dashes(text)):
        inner = m.group(1)
        found = _MARK.findall(inner)
        marks += found
        if len(_MARK_LOOSE.findall(inner)) > len(found):
            bad.append(f"[{inner.strip()}]")
    return list(dict.fromkeys(marks)), list(dict.fromkeys(bad))


def _bad_brackets(rel: str, bad: list[str]) -> list[str]:
    return [
        f"{rel}: скобка «{b}» неразборной формы: метка пишется [О-n] (русская О, дефис, номер)" for b in bad
    ]


@check("promise_labels_resolve")
def promise_labels_resolve(c: Ctx) -> list[str]:
    text, out = c.doc(COMMS_STRATEGY)
    prom, errs = c.promises()
    if text is None:
        return out
    if prom is None:
        return errs
    known = {p.id for p in prom.promises}
    marks, bad = _marks(text)
    out += _bad_brackets(COMMS_STRATEGY, bad)
    one_pager = c.text(ONE_PAGER)
    if one_pager is not None:
        out += _bad_brackets(ONE_PAGER, _marks(one_pager)[1])
    if not marks:
        return [
            f"{COMMS_STRATEGY}: нет ни одной метки [О-n]; "
            f"пометьте обещающие фразы номерами из {PROMISES_FILE}",
            *out,
        ]
    for letter, n in marks:
        if letter == "O":
            out.append(f"{COMMS_STRATEGY}: метка [O-{n}] написана латинской O, нужна русская О")
        elif f"О-{n}" not in known:
            out.append(f"{COMMS_STRATEGY}: метка [О-{n}] не ведёт на обещание из {PROMISES_FILE}")
    return out


@check("unconfirmed_promise_not_released")
def unconfirmed_promise_not_released(c: Ctx) -> list[str]:
    prom, out = c.promises()
    if prom is None:
        return out
    status = {p.id: p.status for p in prom.promises}
    for rel in (COMMS_STRATEGY, ONE_PAGER):
        text, errs = c.doc(rel)
        if text is None:
            out += errs
            continue
        for letter, n in _marks(text)[0]:
            if status.get(f"О-{n}") == "не подтверждено":
                out.append(
                    f"{rel}: метка [{letter}-{n}] ведёт на обещание со статусом «не подтверждено»: "
                    "уберите фразу из текста для аудитории"
                )
    return out


# ---------------------------------------------------------------------------
# А-21: номера в документах
# ---------------------------------------------------------------------------


@check("document_refs_resolve")
def document_refs_resolve(c: Ctx) -> list[str]:
    docs = c.outputs()
    if not docs:
        return ["в outputs/ нет документов *.md: нечего проверять, документы пишутся шагами 3–5"]
    sources, _ = c.sources()
    claims, _ = c.claims()
    gaps, _ = c.gaps()
    plan, _ = c.plan()
    opts, _ = c.options()
    ver, _ = c.verification()
    prom, _ = c.promises()
    registry: dict[str, tuple[str, set[str] | None]] = {
        "И": (SOURCES_FILE, {s.id for s in sources.sources} if sources else None),
        "У": (CLAIMS_FILE, {x.id for x in claims.claims} if claims else None),
        "П": (GAPS_FILE, {g.id for g in gaps.gaps} if gaps else None),
        "ВП": (PLAN_FILE, {q.id for q in plan.questions} if plan else None),
        "Вар": (OPTIONS_FILE, {o.id for o in opts.options} if opts else None),
        "ПП": (VERIFICATION_FILE, {r.id for r in ver.rows} if ver else None),
        "О": (PROMISES_FILE, {p.id for p in prom.promises} if prom else None),
    }
    out: list[str] = []
    no_file: dict[tuple[str, str], list[str]] = {}
    for rel in docs:
        text = dashes(c.text(rel) or "")
        seen: set[str] = set()
        for raw, cyr, n in _sloppy(text):
            if cyr == "О" and rel not in PROMISE_DOCS:
                continue
            if raw not in seen:
                seen.add(raw)
                out.append(
                    f"{rel}: номер «{raw}» написан не по форме, нужно {cyr}-{n} "
                    "(русские буквы, дефис, число, без пробелов)"
                )
        for letters, n in _TOKEN.findall(text):
            token = f"{letters}-{n}"
            if token in seen:
                continue
            seen.add(token)
            cyr = letters.translate(_TO_CYR)
            if cyr not in PREFIXES or (cyr == "О" and rel not in PROMISE_DOCS):
                continue
            if letters != cyr:
                out.append(
                    f"{rel}: номер {token} написан латинскими буквами, нужно {cyr}-{n} (буквы русские)"
                )
                continue
            file, ids = registry[cyr]
            if ids is None:
                no_file.setdefault((rel, file), []).append(token)
            elif token not in ids:
                out.append(f"{rel}: номера {token} нет в {file}: поправьте номер или допишите запись")
    for (rel, file), tokens in no_file.items():
        listed = ", ".join(tokens[:5]) + (" …" if len(tokens) > 5 else "")
        out.append(f"{rel}: номера {listed} ведут в {file}, а файла нет или он не читается")
    return _first(out)


# ---------------------------------------------------------------------------
# А-22: слова предметной области
# ---------------------------------------------------------------------------

_WORD = re.compile(r"[A-Za-zА-Яа-яЁё]+")


class DomainWords(BaseModel):
    """`knowledge/rubrics/domain-words.yaml`: слова и сочетания области по умолчанию."""

    model_config = ConfigDict(extra="forbid")
    words: list[str] = Field(min_length=1)


def knowledge_dir() -> Path:
    import os

    env = os.environ.get("STRATEG_KNOWLEDGE_DIR")
    if env:
        return Path(env)
    here = Path(__file__).resolve().parents[2] / "knowledge"
    if here.is_dir():
        return here
    from strateg.cli import find_repo_root  # импорт здесь: cli импортирует модули команд

    repo = find_repo_root()
    if repo is None:
        raise CliError(
            "не найден корень репозитория (pyproject.toml); задайте папку знаний в STRATEG_KNOWLEDGE_DIR"
        )
    return repo / "knowledge"


def load_domain_words(path: Path | None = None) -> list[str]:
    path = path or knowledge_dir() / "rubrics" / "domain-words.yaml"
    try:
        return DomainWords.model_validate(yaml.safe_load(path.read_bytes().decode("utf-8-sig"))).words
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValidationError) as exc:
        raise CliError(f"список слов предметной области {path.name} не читается", [str(exc)[:200]]) from exc


@cache
def _lemmas(word: str) -> frozenset[str]:
    """Леммы слова: нормальные формы нескольких первых разборов pymorphy3 (омонимы), ё как е."""
    from strateg import safety

    low = word.casefold().replace("ё", "е")
    parses = safety._analyzer().parse(low)[:3]
    return frozenset({low, *(p.normal_form.replace("ё", "е") for p in parses)})


def phrase_lemmas(phrase: str) -> list[str]:
    """Леммы слов сочетания подряд: нормальная форма первого разбора каждого слова."""
    from strateg import safety

    return [
        safety._analyzer().parse(w.casefold())[0].normal_form.replace("ё", "е") for w in _WORD.findall(phrase)
    ]


def lemma_tokens(text: str) -> list[frozenset[str]]:
    return [_lemmas(w) for w in _WORD.findall(text)]


def _contains(tokens: list[frozenset[str]], phrase: list[str]) -> bool:
    n = len(phrase)
    return any(all(phrase[j] in tokens[i + j] for j in range(n)) for i in range(len(tokens) - n + 1))


def grounding_text(root: Path, journal: list[JournalEvent]) -> str:
    """Запрос, рамка, снимки sources/ и материалы inputs/: всё, чем слово из документа можно обосновать."""
    parts: list[str] = []
    frame_path = root / FRAME_FILE
    if frame_path.is_file():
        try:
            parts += [t for _, t in _walk_strings(proj.read_yaml(frame_path))]
        except CliError:
            pass
    for e in journal:
        if e.gate is not None and e.gate.frame is not None:
            parts += [t for _, t in _walk_strings(e.gate.frame.model_dump(mode="json"))]
    for folder in ("sources", "inputs"):
        base = root / folder
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_file() and p.suffix.lower() in (".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".html"):
                parts.append(p.read_bytes().decode("utf-8", errors="replace"))
    return "\n".join(parts)


@check("domain_words_grounded")
def domain_words_grounded(c: Ctx) -> list[str]:
    docs = c.outputs()
    if not docs:
        return ["в outputs/ нет документов *.md: нечего проверять, документы пишутся шагами 3–5"]
    phrases = {w: phrase_lemmas(w) for w in load_domain_words()}
    ground = lemma_tokens(grounding_text(c.root, c.journal))
    out: list[str] = []
    for rel in docs:
        tokens = lemma_tokens(c.text(rel) or "")
        bad = [w for w, ph in phrases.items() if _contains(tokens, ph) and not _contains(ground, ph)]
        if bad:
            out.append(
                f"{rel}: слова «{'», «'.join(bad)}» нет ни в запросе и рамке, ни в снимках sources/, "
                "ни в материалах inputs/: уберите слово или обоснуйте его источником"
            )
    return out


# ---------------------------------------------------------------------------
# А-23 … А-25
# ---------------------------------------------------------------------------


@check("project_check_clean")
def project_check_clean(c: Ctx) -> list[str]:
    errors = check_project(c.root)
    if not errors:
        return []
    shown = "; ".join(errors[:2]) + (" …" if len(errors) > 2 else "")
    return [
        f"проверка проекта нашла ошибок: {len(errors)}; исправьте и повторите "
        f"(список — `strateg check`): {shown}"
    ]


@check("premortem_stop_recorded")
def premortem_stop_recorded(c: Ctx) -> list[str]:
    ver, out = c.verification()
    if ver is None:
        return out
    last_b4 = max(
        (i for i, e in enumerate(c.journal) if e.gate is not None and e.gate.gate == "В4"), default=None
    )
    stops = [
        e.message or ""
        for e in (c.journal[last_b4 + 1 :] if last_b4 is not None else [])
        if e.kind == "действие" and e.name == STOP_ACTION
    ]
    for n, m in enumerate(ver.premortem, start=1):
        if not m.hits_choice:
            continue
        ids = (m.row, m.claim)
        if not any(re.search(rf"(?<![\w-]){re.escape(i)}(?!\d)", s) for s in stops for i in ids):
            out.append(
                f"{VERIFICATION_FILE}, premortem №{n}: hits_choice: true, а после ворот В4 нет внеплановой "
                f'остановки с номером {m.row} или {m.claim}: `strateg stop --reason "... {m.row} ..."`'
            )
    return out


@check("recommendation_has_independent_fact")
def recommendation_has_independent_fact(c: Ctx) -> list[str]:
    opts, out = c.options()
    claims, errs = c.claims()
    if opts is None:
        return out
    if claims is None:
        return errs
    by_id = {cl.id: cl for cl in claims.claims}
    for cid in opts.recommendation.claims:
        cl = by_id.get(cid)
        if cl and cl.type == "факт" and any(q.self_reported is False and q.check for q in cl.quotes):
            return []
    return [
        f"{OPTIONS_FILE}, recommendation.claims: нет факта с проверенной цитатой self_reported: false; "
        "добавьте факт из независимого источника: "
        "рекомендация не держится только на заявлении компании о себе"
    ]

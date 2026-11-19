"""Синтетическая сессия: ответы модельных респондентов как источник вида «синтетика» (ADR-0007, ADR-0011).

`strateg synth add --persona '<строка>' --file projects/<id>/.tmp/<имя>.md` берёт беседу, которую собрал
основной агент из ответов субагента `respondent`, и записывает её источником `И-n`:
- вход — пары «Вопрос n: …» и «Ответ n: СИНТЕТИКА: …»; ответ без метки «СИНТЕТИКА» — отказ;
- снимок — `inputs/synth-<n>.md`: шапка «СИНТЕТИКА», персона и пары с метками `[строка N]`, как у расшифровки;
  `sha256` снимка — в реестре, цитата ищется по снимку и привязана к строке ответа;
- источник — вид «синтетика», недоверенный: инструкция внутри ответа — данные, а не команда;
- утверждения на нём — только типа «синтетика»: гипотезы и вопросы для гайда, а не факты и не основания
  рекомендации (`check` называет каждое поле, где синтетика стоять не может);
- событие журнала — «strateg synth add», исполнитель CLI; вызов роли `respondent` подтверждает событие
  «роль выполнена» от хука `SubagentStop`: без него сессия помечена «не подтверждён», как у критика.
"""

from __future__ import annotations

import argparse
import re
import time
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from strateg import commands, safety
from strateg import project as proj
from strateg.models import Source
from strateg.project import CliError, Project, UsageError

ROLE = "respondent"
ACTION = "strateg synth add"
ROLE_DONE = "роль выполнена"
LABEL = "СИНТЕТИКА"
MIN_PERSONA, MAX_PERSONA = 20, 600
MAX_PAIRS = 30
MAX_CHARS = 100_000
HEADER = (
    f"{LABEL}: сессия модельного респондента. Ответы написаны моделью, это не слова людей и не данные "
    "о людях: из них получаются гипотезы и вопросы для гайда, а не факты."
)
_TURN = re.compile(r"^(Вопрос|Ответ)(?:\s+(\d+))?\s*:\s*(.*)$")
_LABELLED = re.compile(rf"^{LABEL}:\s*(.*)$")  # метка ровно «СИНТЕТИКА:» в начале ответа
# Отказ назвать или ответить: «точных цифр называть не буду», «не буду называть», «не скажу». Это не обещание
# будущего: слово «буду» в нём ничего не обещает. Выражения рубрики гайда ловят «буду» в любом виде.
_SAY = (
    r"(?:назыв\w*|говор\w*|отвеч\w*|обсужд\w*|раскрыв\w*|сообщ\w*|уточн\w*|коммент\w*|привод\w*|"
    r"разглаш\w*|вдаваться)"
)
_NOT_WILL = r"не\s+(?:буду|будем|стану|станем)"
_REFUSAL = re.compile(
    rf"\b{_SAY}\s+{_NOT_WILL}\b"
    rf"|\b{_NOT_WILL}\s+{_SAY}"
    r"|\bне\s+(?:скажу|скажем|назову|назовем|отвечу|раскрою|сообщу|уточню|приведу|разглашу)\b"
)
_FLATTERY = re.compile(
    r"отличн\w+\s+иде|было\s+бы\s+(?:здорово|отлично|круто|замечательно)|с\s+удовольствием|"
    r"обязательно\s+(?:куплю|буду|воспользуюсь|попробую)|звучит\s+(?:здорово|отлично|круто)",
    re.IGNORECASE,
)
_PERSONA_PARTS = (
    ("альтернатив", "текущая альтернатива"),
    ("огранич", "ограничения (деньги, время, привычка)"),
)
_PERSONA_ORIGIN = ("рамк", "картин", "аудитори", "материал", "источник", "допущени")


def parse_session(raw: str) -> list[tuple[str, str]]:
    """Пары (вопрос, ответ без метки): вход должен чередовать «Вопрос» и «Ответ», каждый ответ — с меткой."""
    turns: list[list[str]] = []
    numbers: list[str | None] = []
    for n, line in enumerate(raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"), start=1):
        if not line.strip():
            continue
        m = _TURN.match(line.strip())
        if m:
            turns.append([m.group(1), m.group(3).strip()])
            numbers.append(m.group(2))
        elif turns:
            turns[-1][1] = f"{turns[-1][1]} {line.strip()}".strip()
        else:
            raise CliError(
                f"строка {n}: текст до первого «Вопрос n: …»; беседа — пары «Вопрос n:» и «Ответ n:»"
            )
    if not turns or len(turns) % 2 or any(t[0] != ("Вопрос", "Ответ")[i % 2] for i, t in enumerate(turns)):
        raise CliError(
            "беседа — чередующиеся «Вопрос n: …» и «Ответ n: СИНТЕТИКА: …», начало с вопроса, конец ответом"
        )
    if len(turns) // 2 > MAX_PAIRS:
        raise CliError(f"в беседе больше {MAX_PAIRS} вопросов: запишите её частями")
    pairs: list[tuple[str, str]] = []
    unlabelled: list[str] = []
    for k in range(0, len(turns), 2):
        question, answer = turns[k][1], turns[k + 1][1]
        asked, answered = numbers[k], numbers[k + 1]
        if asked and answered and int(asked) != int(answered):
            raise CliError(
                f"пара {k // 2 + 1}: «Ответ {answered}» стоит после «Вопрос {asked}»: номера должны совпадать"
            )
        m = _LABELLED.match(answer)
        if not m:
            unlabelled.append(str(k // 2 + 1))
            continue
        if not question or not m.group(1).strip():
            raise CliError(f"пара {k // 2 + 1}: пустой вопрос или ответ без текста после метки «{LABEL}»")
        pairs.append((question, m.group(1).strip()))
    if unlabelled:
        raise CliError(
            f"в ответах {', '.join(unlabelled)} нет метки «{LABEL}»: каждый ответ респондента начинается "
            "с неё, "
            "иначе ответ модели можно принять за слова человека"
        )
    return pairs


def check_persona(raw: str) -> tuple[str, list[str]]:
    """Персона одной строкой и предупреждения: чего в ней не хватает (альтернатива, ограничения, откуда)."""
    persona = " ".join((raw or "").split())
    if len(persona) < MIN_PERSONA:
        raise UsageError(
            f"персона короче {MIN_PERSONA} знаков: нужны кто, ситуация, текущая альтернатива, ограничения "
            "и откуда она взята (рамка, картина ситуации)"
        )
    if len(persona) > MAX_PERSONA:
        raise UsageError(f"персона длиннее {MAX_PERSONA} знаков: одна строка, без истории жизни")
    masked = safety.mask_personal(persona)[0]
    warnings = ["в персоне найдены контакты или номера: они заменены метками"] if masked != persona else []
    persona = masked
    low = persona.casefold()
    warnings += [
        f"в персоне нет слова «{word}»: без этого ответы усредняются и угождают ({what})"
        for word, what in _PERSONA_PARTS
        if word not in low
    ]
    if not any(word in low for word in _PERSONA_ORIGIN):
        warnings.append("в персоне не сказано, откуда она взята (рамка, аудитория, картина ситуации)")
    return persona, warnings


def snapshot(persona: str, pairs: list[tuple[str, str]]) -> str:
    """Текст снимка: шапка, персона и пары; каждая строка — с меткой `[строка N]`."""
    lines = [HEADER, f"Персона: {persona}"]
    for n, (question, answer) in enumerate(pairs, start=1):
        lines += [f"Вопрос {n}: {question}", f"Ответ {n}: {LABEL}: {answer}"]
    return "\n".join(f"[строка {n}] {line}" for n, line in enumerate(lines, start=1)) + "\n"


def _answer_warnings(pairs: list[tuple[str, str]]) -> list[str]:
    from strateg import guide  # импорт здесь: модуль разбора гайда тянет рубрику

    future = guide.load_rubric().rules["future"].patterns
    out: list[str] = []
    for n, (_, answer) in enumerate(pairs, start=1):
        low = _REFUSAL.sub(" ", guide.fold(answer))  # отказ ответить («называть не буду») — не обещание
        hit = guide.first_hit(future, low)
        if hit:
            out.append(
                f"ответ {n} обещает будущее («{hit}»): роль на такой вопрос отвечает «не знаю, расскажу, как "
                "было»; обещание гипотезой не считай"
            )
        hit = _FLATTERY.search(answer)
        if hit:
            out.append(f"ответ {n}: признак угодливости («{hit.group(0)}»): не опора даже для гипотезы")
    return out


def respondent_call(p: Project) -> str:
    """«подтверждён», если роль `respondent` выполнилась на этом шаге и вызов не истрачен прежней записью.

    Журнал только дописывается: порядок строк — порядок событий. Каждый вызов роли оплачивает одну
    сессию: запись `synth add` уменьшает запас на единицу.
    """
    pool = 0
    for e in p.journal:
        if e.step != p.state.step:
            continue
        if e.kind == ROLE_DONE and e.role == ROLE:
            pool += 1
        elif e.kind == "действие" and e.name == ACTION and e.status == "выполнено":
            pool = max(pool - 1, 0)
    return "подтверждён" if pool else "не подтверждён"


def _read(path: str) -> str:
    file = Path(path)
    if not file.is_file():
        raise CliError(f"файл {path} не найден: беседу кладут в projects/<id>/.tmp/")
    try:
        if file.stat().st_size > MAX_CHARS * 4:
            raise CliError(f"файл {path} слишком большой для беседы")
        text = file.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CliError(f"файл {path} не в UTF-8") from exc
    except OSError as exc:
        raise CliError(f"файл {path} не читается: {exc.strerror or exc}") from exc
    if len(text) > MAX_CHARS:
        raise CliError(f"в беседе больше {MAX_CHARS} знаков: запишите её частями")
    return text


def cmd_synth_add(args: argparse.Namespace) -> int:
    p = commands._open(args)
    persona, warnings = check_persona(args.persona)
    pairs = parse_session(_read(args.file))
    warnings += _answer_warnings(pairs)
    text = snapshot(persona, pairs)
    report = safety.inspect_material(
        text
    )  # выдуманные телефоны и почта не остаются в снимке; обращения к ИИ — метка
    sha = proj.sha256_bytes((persona + "\n" + text).encode("utf-8"))
    started = time.monotonic()
    duplicate = False
    with proj.transaction(p, rollback=False):
        twin = next(
            (s for s in p.sources.sources if s.kind == "синтетика" and s.material_sha256 == sha), None
        )
        if twin is not None:
            source, duplicate = twin, True
        else:
            number = proj.next_source_number(p)
            while (p.root / "inputs" / f"synth-{number}.md").exists():
                number += 1
            sid, rel = f"И-{number}", f"inputs/synth-{number}.md"
            body = report.text
            data: dict[str, Any] = {
                "id": sid,
                "kind": "синтетика",
                "path": rel,
                "access": "получен",
                "untrusted": True,
                "material_sha256": sha,
                "text_path": rel,
                "text_sha256": proj.sha256_bytes(body.encode("utf-8")),
                "suspicious": bool(report.instruction_signs),
                "anonymized": report.anonymized,
                "anonymization_confirmed": bool(report.anonymized),  # значения выдуманы: подтверждать нечего
                "persona": persona,
                "respondent_call": respondent_call(p),
            }
            try:
                source = Source.model_validate(data)
            except ValidationError as exc:
                raise CliError(f"источник не записан: {exc.errors()[0]['msg']}") from exc
            written = False
            try:
                proj.write_text(p.root / rel, body)
                written = True
                p.sources.sources.append(source)
                p.save_sources()
                key = proj.key_hash("synth add", sha)
                p.add_event(p.event(kind="действие", name=ACTION, idempotency_hash=key, status="начато"))
                if source.suspicious:
                    p.add_event(
                        p.event(
                            kind="найдена инструкция в источнике",
                            source=sid,
                            message="в ответе респондента найдено обращение к ИИ, оно не исполнено "
                            "(признаки: " + "; ".join(report.instruction_signs) + ")",
                        )
                    )
                p.add_event(
                    p.event(
                        kind="действие",
                        name=ACTION,
                        idempotency_hash=key,
                        status="выполнено",
                        duration_s=round(time.monotonic() - started, 2),
                        message=f"{sid}: синтетика, вопросов {len(pairs)}, вызов респондента "
                        f"{source.respondent_call}",
                    )
                )
                p.save_state()
            except BaseException:
                if written:
                    (p.root / rel).unlink(missing_ok=True)
                raise
    if source.respondent_call == "не подтверждён" and not duplicate:
        warnings.append(
            f"вызов роли {ROLE} не подтверждён: в журнале нет события «{ROLE_DONE}» на этом шаге. Сессия не "
            "независимая: беседу вёл тот же агент (нет хуков или роль сыграна в основном контексте)"
        )
    if source.suspicious and not duplicate:
        warnings.append(
            "в ответе найдено обращение к ИИ: источник помечен «подозрительный», оно не исполняется"
        )
    data_out = {
        "source": source.id,
        "kind": source.kind,
        "duplicate": duplicate,
        "text_path": source.text_path,
        "snapshot_path": commands._snapshot_path(p, source),
        "sha256": source.text_sha256,
        "persona": source.persona,
        "answers": len(pairs),
        "respondent_call": source.respondent_call,
        "suspicious": source.suspicious,
        "warnings": warnings,
    }
    head = (
        f"{source.id}: эта сессия уже записана, повтор отдаёт прежний номер."
        if duplicate
        else f"{source.id}: записана синтетическая сессия, ответов {len(pairs)} (все — «{LABEL}»)."
    )
    lines = [
        head,
        f"Снимок: {data_out['snapshot_path']}",
        "Ответы модели — гипотезы и вопросы для гайда, а не факты: утверждения на этом источнике — "
        "`claim add` типа «синтетика», цитата с местом «строка N» из ответа.",
        *[f"Внимание: {w}" for w in warnings],
    ]
    return commands.emit(args, data_out, lines)

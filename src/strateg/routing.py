"""Маршрут роли (ADR-0008): модель и effort роли из `config/routing.yaml` с учётом профиля.

`strateg route <роль>` называет модель роли и пишет выбор в журнал; `--upgrade` — повышение по условию
из таблицы (не больше `times` раз на роль и шаг, счёт по событиям журнала); `strateg route --profile`
переключает профиль проекта («экономнее», «глубже», «обычный»). Таблица — единственный источник выбора
модели для ролей; субагентам и хукам она советует, а не навязывает.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, Strict, ValidationError, model_validator

from strateg import project as proj
from strateg.budget import config_dir
from strateg.commands import _open, emit
from strateg.models import SchemaVersion, Text
from strateg.project import CliError, Project, UsageError
from strateg.project_check import _format_errors

ROUTING_FILE = "routing.yaml"
Level = Literal["сильная", "средняя", "дешёвая"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]
ProfileName = Literal["экономнее", "глубже"]
LEVELS = ("сильная", "средняя", "дешёвая")
DEFAULT_PROFILE = "обычный"
PICK_EVENT = "выбор"
UPGRADE_EVENT = "повышение"
PROFILE_ACTION = "смена профиля"
Count = Annotated[int, Strict(), Field(ge=1)]


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Upgrade(_Config):
    """Условие повышения: когда, до какого уровня и сколько раз на роль и шаг."""

    when: Text
    to: Level
    times: Count


class RoleRoute(_Config):
    level: Level
    effort: Effort
    depth: Count | None = Field(None, description="Исследователь: число источников на вопрос плана")
    upgrade: Upgrade | None = None


class RoleOverride(_Config):
    """Что профиль меняет у роли."""

    level: Level | None = None
    effort: Effort | None = None
    depth: Count | None = None

    @model_validator(mode="after")
    def _rules(self) -> Self:
        if self.level is None and self.effort is None and self.depth is None:
            raise ValueError("профиль ничего не меняет у роли: укажите level, effort или depth")
        return self


class RoutingFile(_Config):
    """Файл `config/routing.yaml`."""

    schema_version: SchemaVersion
    levels: dict[Level, Text] = Field(description="Уровень -> модель (псевдоним Claude Code)")
    roles: dict[Text, RoleRoute] = Field(min_length=1)
    profiles: dict[ProfileName, dict[Text, RoleOverride]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _rules(self) -> Self:
        p: list[str] = []
        lacking = [lv for lv in LEVELS if lv not in self.levels]
        if lacking:
            p.append("в levels нет уровней: " + ", ".join(lacking))
        for name, roles in self.profiles.items():
            for role in roles:
                if role not in self.roles:
                    p.append(f"профиль «{name}»: роли «{role}» нет в roles (есть: {', '.join(self.roles)})")
        if p:
            raise ValueError("\n".join(p))
        return self


def load_routing() -> RoutingFile:
    """`config/routing.yaml`; нет файла, опечатка в роли или уровне — `CliError` с местом ошибки."""
    path = config_dir() / ROUTING_FILE
    try:
        raw = yaml.safe_load(path.read_bytes().decode("utf-8-sig"))
    except OSError as exc:
        raise CliError(f"таблица маршрутов {ROUTING_FILE} не найдена в {path.parent.name}/") from exc
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise CliError(f"таблица маршрутов {ROUTING_FILE} не читается как YAML") from exc
    try:
        return RoutingFile.model_validate(raw)
    except ValidationError as exc:
        raise CliError(
            f"таблица маршрутов {ROUTING_FILE} не по схеме", _format_errors(ROUTING_FILE, (), raw, exc)
        ) from exc


@dataclass(frozen=True)
class Route:
    """Выбор для роли с учётом профиля."""

    role: str
    level: str
    model: str
    effort: str
    depth: int | None
    upgrade: Upgrade | None
    profile: str


def effective(table: RoutingFile, role: str, profile: str, *, level: str | None = None) -> Route:
    if role not in table.roles:
        raise UsageError(f"роли «{role}» нет в {ROUTING_FILE}: есть {', '.join(table.roles)}")
    base = table.roles[role]
    over = table.profiles.get(profile, {}).get(role)  # type: ignore[call-overload]
    lv = level or (over.level if over and over.level else base.level)
    return Route(
        role=role,
        level=lv,
        model=table.levels[lv],  # type: ignore[index]
        effort=over.effort if over and over.effort else base.effort,
        depth=over.depth if over and over.depth else base.depth,
        upgrade=base.upgrade,
        profile=profile,
    )


def upgrades_done(p: Project, role: str) -> int:
    """Сколько повышений роли уже записано на текущем шаге."""
    return sum(
        1
        for e in p.journal
        if e.kind == "выбор модели" and e.role == role and e.name == UPGRADE_EVENT and e.step == p.state.step
    )


def _profile(p: Project) -> str:
    return p.state.profile or DEFAULT_PROFILE


def _route_data(r: Route, *, upgrade: bool) -> dict[str, Any]:
    return {
        "role": r.role,
        "level": r.level,
        "model": r.model,
        "effort": r.effort,
        "depth": r.depth,
        "profile": r.profile,
        "upgrade": upgrade,
    }


def cmd_route(args: argparse.Namespace) -> int:
    role = getattr(args, "role", None)
    profile = getattr(args, "profile", None)
    if role and profile:
        raise UsageError("укажите либо роль, либо --profile: выбор роли и смена профиля — разные команды")
    if not role and not profile:
        raise UsageError(
            "укажите роль (`strateg route critic`) или профиль (`strateg route --profile глубже`)"
        )
    if profile:
        return _set_profile(args, profile)
    return _pick(args, role)


def _set_profile(args: argparse.Namespace, profile: str) -> int:
    p = _open(args)
    table = load_routing()
    if profile != DEFAULT_PROFILE and profile not in table.profiles:
        raise UsageError(f"профиля «{profile}» нет в {ROUTING_FILE}: есть {', '.join(table.profiles)}")
    old = _profile(p)
    if old == profile:
        return emit(args, {"profile": profile, "changed": False}, [f"Профиль уже «{profile}»."])
    changes = sum(1 for e in p.journal if e.kind == "действие" and e.name == PROFILE_ACTION)
    with proj.transaction(p.root):
        p.add_event(
            p.event(
                kind="действие",
                name=PROFILE_ACTION,
                idempotency_hash=proj.key_hash("profile", changes + 1, profile),
                status="выполнено",
                duration_s=0,
                message=f"профиль изменён: {old} -> {profile}",
            )
        )
        p.state.profile = profile  # type: ignore[assignment]
        p.save_state()
    return emit(
        args,
        {"profile": profile, "previous": old, "changed": True},
        [f"Профиль изменён: {old} -> {profile}."],
    )


def _pick(args: argparse.Namespace, role: str) -> int:
    p = _open(args)
    table = load_routing()
    profile = _profile(p)
    base = effective(table, role, profile)
    reason = (getattr(args, "reason", None) or "").strip()
    upgrade = bool(getattr(args, "upgrade", False))
    chosen = base
    if upgrade:
        rule = base.upgrade
        if rule is None:
            raise CliError(f"у роли «{role}» в {ROUTING_FILE} нет условия повышения")
        done = upgrades_done(p, role)
        if done >= rule.times:
            raise CliError(
                f"повышение роли «{role}» на шаге {p.state.step} уже записано {done} раз из {rule.times}: "
                "предел достигнут, выбор остаётся прежним"
            )
        if rule.to == base.level:
            raise CliError(f"роль «{role}» уже на уровне «{rule.to}»: повышать некуда")
        chosen = effective(table, role, profile, level=rule.to)
    name = UPGRADE_EVENT if upgrade else PICK_EVENT
    parts = [reason] if reason else []
    if upgrade:
        assert base.upgrade is not None
        parts.append(f"повышение: {base.level} -> {chosen.level}, условие: {base.upgrade.when}")
    elif not reason:
        parts.append("по таблице маршрутов")
    parts.append(f"профиль {profile}")
    with proj.transaction(p.root):
        p.add_event(
            p.event(
                kind="выбор модели",
                name=name,
                role=role,
                model=chosen.model,
                level=chosen.level,
                effort=chosen.effort,
                message="; ".join(parts),
            )
        )
        p.save_state()
    lines = [
        f"Роль {role}: модель {chosen.model} (уровень «{chosen.level}»), effort {chosen.effort}.",
        f"Профиль: {profile}."
        + (f" Глубина: {chosen.depth} источника(ов) на вопрос плана." if chosen.depth else ""),
    ]
    if upgrade:
        lines.append(f"Повышение: {base.level} -> {chosen.level}; записано в журнал.")
    return emit(args, _route_data(chosen, upgrade=upgrade), lines)

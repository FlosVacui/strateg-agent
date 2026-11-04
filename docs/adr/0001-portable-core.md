# ADR-0001: Переносимое ядро — AGENTS.md и скиллы в `.claude/skills`

| | |
|---|---|
| Статус | принято |
| Дата | 05.11.2026 |
| Кто решил | команда проекта |
| Связано | ДУ-2, ДУ-6, ТР-1, ТР-2, КО-3; [ADR-0006](0006-workflow-gates-modes.md), [ADR-0007](0007-subagents.md) |

## Контекст
Задание допускает любую среду, а проверяющий может открыть репозиторий не в Claude Code. Общий знаменатель сред — `AGENTS.md`, скиллы в формате `SKILL.md` (поля `name` и `description`) и MCP.
- Claude Code читает `AGENTS.md` сам только при отсутствии `CLAUDE.md`, а импорт `@AGENTS.md` в `CLAUDE.md` работает ([документация](https://code.claude.com/docs/en/memory)).
- Скиллы Claude Code ищет в `.claude/skills` и не упоминает `.agents/skills` ([документация](https://code.claude.com/docs/en/skills)).
- Cursor и OpenCode читают и `.claude/skills`, и `.agents/skills` ([Cursor](https://cursor.com/docs/context/skills), [OpenCode](https://opencode.ai/docs/skills/)). Codex читает только `.agents/skills`, GigaCode — только `.gigacode/skills`.
- Симлинки на Windows ненадёжны.

## Варианты
| Вариант | Плюсы | Минусы |
|---|---|---|
| Только `CLAUDE.md` и `.claude/` | Просто | Cursor и OpenCode видят скиллы, но не правила из `CLAUDE.md` |
| `AGENTS.md` — источник правил, `CLAUDE.md` = `@AGENTS.md` + особенности Claude Code; скиллы только в `.claude/skills` | Одни правила везде; Cursor и OpenCode видят скиллы без копий | Codex и GigaCode без копий скиллов не видят |
| То же плюс постоянные копии `.agents/skills` и `.gigacode/skills` в репозитории | Работает и в Codex, GigaCode | Cursor и OpenCode находят обе папки — двойной список скиллов в контексте; копии без проверяемой среды |

## Решение
Источник правил — `AGENTS.md`, короче 200 строк. `CLAUDE.md` содержит `@AGENTS.md` и раздел об особенностях Claude Code: хуки, субагенты, строка статуса. Скиллы живут только в `.claude/skills`. Копии в репозиторий не кладутся. Для Codex и GigaCode скрипт `scripts/sync_skills.py` по запросу создаёт `.agents/skills` или `.gigacode/skills`, README говорит, когда его запускать. CI проверяет, что скрипт даёт копию, совпадающую с источником. Поля frontmatter скиллов — пересечение стандартов: `name` (латиница, совпадает с папкой) и `description` до 1024 символов. Поля только Claude Code не несут смысла, без которого скилл ломается.

## Последствия
- Проще: Cursor и OpenCode открывают проект без правок и без двойного списка скиллов.
- Сложнее: пользователю Codex или GigaCode нужно один раз запустить скрипт.
- Нужно сделать: режим скрипта «создать копию для среды», раздел README «Другие среды», дымовой прогон в Cursor и OpenCode перед выпуском.

## Польза и замер
- Какую пользу ждём: проверяющий без Claude Code продолжает проект в Cursor или OpenCode (сценарий С-12).
- Как замеряем: прогон С-12 в Cursor и OpenCode — агент находит правила и скиллы и продолжает пример; число ручных правок, нужных для запуска; нет ли дублей скиллов в списке среды.
- Результат замера: ожидается.
- Если пользы нет: в README остаются только среды, где С-12 прошёл; для остальных — «не проверено».

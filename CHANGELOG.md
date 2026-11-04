# Изменения

Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/), версии — [SemVer](https://semver.org/lang/ru/).

## [Не выпущено]
### Добавлено
- Каркас репозитория: README, инструкции агента (черновик), CLI `strateg` с командой `doctor`, тесты, CI на Windows, macOS и Linux, проверка секретов, матрица требований.
- README: раздел «Быстрый старт» для Windows и macOS.
- Тест-страж `tests/test_no_internal.py`: в файлах репозитория нет служебного, личных путей и имени автора.
- Тесты для `scripts/check_links.py` и `scripts/sync_skills.py`.

### Изменено
- `strateg doctor`: кодировка консоли определяется до перенастройки вывода (на Windows — по кодовой странице консоли, подсказка `chcp 65001`); отсутствие git — предупреждение; запись проверяется в `projects/` корня репозитория; предупреждения не меняют код выхода.
- `.pre-commit-config.yaml`: только локальные хуки (gitleaks, `uv run ruff`, проверка копии скиллов); версия ruff берётся из `uv.lock`, ничего не собирается при первом коммите.
- `.gitignore`: проекты пользователя закрыты целиком, кроме `projects/example/` и `projects/demo/`; добавлены личные настройки среды (`.claude/settings.local.json`, `CLAUDE.local.md`, `.vscode/`, `.idea/`) и отчёты покрытия.
- CI: каждая проверка — отдельный шаг, `uv sync --locked`, шаг `strateg doctor`, права `contents: read`, отмена устаревших запусков PR; действия обновлены до версий на Node 24 (`checkout@v5`, `setup-uv@v7`).
- `scripts/check_links.py`: понимает `%XX` в ссылках и суффиксы `-1`, `-2` у повторяющихся заголовков, пропускает личные проекты, исправлен путь в описании.
- `scripts/sync_skills.py`: разбор аргументов через argparse; неизвестный флаг — ошибка, копия не трогается.

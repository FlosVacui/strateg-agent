# Модель данных проекта

Форматы файлов `projects/<id>/`. Источник правды — `src/strateg/models.py`, из него `scripts/gen_schemas.py` строит `schemas/`. Проверка папки — `check_project()`. Решения: [ADR](adr/README.md) 0003–0007, 0010, 0011, 0014, 0016.

## Файлы
| Файл | Формат | Кто пишет | Схема |
|---|---|---|---|
| `state.yaml` | YAML; `profile` — профиль выбора моделей (`route --profile`) | только CLI | `state.schema.json` |
| `journal.jsonl` | JSONL, только добавление; «копия проекта» (`new --from`): время — с неё; у «выбор модели» — `role`, `model`, `level`, `effort`; хук пишет «отказ хука», «граница прав не активна», «задание исследователю», «роль выполнена», «ответ человека», «учёт недоступен», «бюджет» | CLI и хук | `journal-event.schema.json` (строка) |
| `cost.jsonl` | JSONL, только добавление; «по сообщению» пишут и хуки, без повторов `message_id`; `web_search_usd` — веб-поиск в `cost_usd` | CLI и хук | `cost-record.schema.json` (строка) |
| `.tmp/cost/offsets.json`, `.tmp/search.sqlite` | смещения разбора; индекс поиска; не в архиве | хук; `search` | — |
| `sources.yaml` | YAML; `other_urls`, `rechecked_at`, `refreshes` | только CLI | `sources.schema.json` |
| `claims.yaml` | YAML | скиллы через CLI | `claims.schema.json` |
| `sources/` | снимки i-<n>, sha256; без записи — ошибка | CLI | — |
| `inputs/` | материалы человека (при ПДн и у интервью — обезличенная копия) и их текст | человек кладёт, текст пишет CLI | — |
| `frame.yaml` | YAML: десять полей `{value, origin[, source]}`; те же в событии В1 (`gate.frame`) | только CLI | `frame.schema.json` |
| `plan.yaml` | YAML: вопросы `ВП-n` с ветками `{if, then}`, `excluded`, `estimate` | только CLI | `plan.schema.json` |
| `gaps.yaml` | YAML: пробелы `П-n` (`close_via`, `hypothesis`) и показатели `metric_coverage` | только CLI | `gaps.schema.json` |
| `options.yaml` | YAML: варианты `Вар-n`, возражения, сравнение, `recommendation`, `chosen` | только CLI | `options.schema.json` |
| `promises.yaml` | YAML: «обещание → возможность → источник», `О-n` | только CLI | `promises.schema.json` |
| `verification.yaml` | YAML: строки `ПП-n` (`from_change_mind`), `premortem`, `undesired_effects` | только CLI | `verification.schema.json` |
| `decisions.yaml` | YAML: решения `Реш-n`; условие отказа — со строкой плана `row`; обязательство — с `done`, `done_note` | только CLI | `decisions.schema.json` |
| `metrics.yaml` | YAML: значения показателей `ПП-n` (ADR-0016) | только CLI (`metric add`) | `metrics.schema.json` |
| `completeness.yaml` | YAML: итог полноты: `by` (`критик` или `агент`), часть А, ответы части Б, `result`, `failed`, `critic_call` (вызов критика подтверждён) | только CLI (`rubric record`) | `completeness.schema.json` |
| `outputs/calc/<имя>.py`, `.json` | расчёт и результат с хешами | `.json` — CLI | `calc-result.schema.json` |
| `history/` | прежние версии: `history/frame.v1.yaml`, `history/outputs/options.v2.md`, `history/release-1/` | CLI (`write`, `rubric record`, `step done 7`) | — |

Семь файлов выводов необязательны, `completeness.yaml` — с шага 6. Документы — Markdown в `outputs/`, поля для проверок — в YAML.

## Общие правила
- Ключи английские, snake_case; значения закрытых списков русские. В каждом файле и записи JSONL есть `schema_version` (сейчас 1). Неизвестные поля и повтор ключа YAML — ошибка. Несовместимое изменение поднимает `schema_version`, пишется в `CHANGELOG.md`.
- Пути — только в полях путей: относительные, через `/`, без `C:\`, `\\сервер`, `..`, `.`, пустых элементов. UTF-8 без BOM, LF. Время — ISO 8601 с поясом; стоимость и длительность — конечные числа. Даты в выводах — `"2026-11-06"`, цифры ASCII.
- Номера: `И-n` источник, `У-n` утверждение, `ВП-n`, `П-n`, `Вар-n`, `О-n`, `ПП-n`, `Реш-n` (буквы русские; латинскую ошибка называет). YAML 1.1: `no`, `1.10`, даты и шаг `3` без кавычек — другой тип.
- `access` источника: «получен», «источник недоступен: защита», «запрещено robots.txt», «нужен браузер», «источник недоступен: ошибка». Статус действия: «начато», «выполнено», «ошибка», «пропущено», «прервано» (начато, не выполнено; `duration_s` — работа шага до перерыва; шаг на паузе (ворота, остановка) не прерывается).
- Материал хранит `material_sha256`: повтор `add` отдаёт прежний номер. Исходник с ПДн — в `.tmp/` до `add --confirm`, затем в `~/.strateg/private/`; расшифровка интервью (`consent`: как получено согласие) идёт так всегда. У синтетики `persona`, `respondent_call`. Утверждение хранит `step` и `attempt`: повтор дублей не даёт.

## Что не выразить схемой — только кодом
**Внутри записи** (валидаторы моделей):
- Утверждения и журнал: факт — проверенная цитата и происхождение «источник»; синтетика — тип и происхождение вместе; у гипотезы основания и способ проверки, у прогноза основания и диапазон, у допущения «откуда взято»; цитата короче 40 знаков — только «точное вхождение»; история статусов не стирается; у действия ключ, статус, длительность.
- `frame.yaml`: `version` от 1 до 1000; `source` только у «из материалов»; `frame` только у В1. `plan.yaml`: `then` веток различаются после нормализации; «пробел» требует `gap`, «открыт» его не допускает.
- `gaps.yaml`: «другая» требует `reason_note`, «закрыт» — `closed_by`; в `metric_coverage` один ответ, вид не повторяется.
- `options.yaml`: состав вариантов; названия вариантов и `drops_if` возражений не повторяются; ячейка — `text` или `why_empty` (не короче 10 знаков, с буквами); эффект и сигнал с текстом — с `forecast`; обратимость — с `what`, `time`, `price`.
- `verification.yaml`: ровно три разные причины пре-мортема; метод — с `strength` и `not_prove`. Владелец (и в решении) — не «все», «команда», «TBD», «n/a» и их написания. Порог (`threshold`) содержит цифру; у условия решения есть порог и дата.

**Между записями и файлами** (`check_project`):
- Ссылки ведут на существующие номера; `plan_question` вида `ВП-n` сверяется с `plan.yaml`, `baseline` вида `П-n` — с `gaps.yaml`; похожее на номер — ошибка с подсказкой.
- Типы: прогноз у ячейки сравнения, факт у обещания, гипотеза у показателя, пре-мортема и `gaps[].hypothesis`, допущение у `gaps[].assumption`, допущение или гипотеза у `decisions[].assumptions`, факт или прогноз у `metric_coverage[].claims`, факт или слово «гипотеза» у `objections[].basis`.
- У рекомендации есть факт с цитатой не «о себе» (`self_reported: false`). Номера `П-n`, `У-n`, `Вар-n` в тексте файлов выхода ведут на реестры.
- Синтетики и опирающегося только на неё (цепочка `basis`) нет в показателе, пре-мортеме, `recommendation.claims` (там же хоть один факт), `metric_coverage[].claims`, `closed_by`, `decisions.basis`, `objections[].basis`; `basis` факта, прогноза, допущения на неё не ведёт; в тексте `options`, `decisions`, `promises` нет `У-n` такого утверждения и `И-n` синтетики (кроме полей гипотез). Отозванное или заменённое нельзя только в рекомендации и обещании.
- Причина пре-мортема ведёт в строку «защитный»; условие решения с `row` — в существующую строку с тем же порогом; порог строки с `from_change_mind` равен порогу записи `recommendation.change_mind`.
- `history/`: имена `<файл>.v<N>.<расширение>`; в `release-<n>/` только файлы выхода и `outputs/*.md`; версии рамки 1…N−1, нехватка — одно сообщение, выше N−1 — ошибка; прежняя версия — рамка с тем же номером.
- `completeness.yaml`: номера частей А и Б есть в рубрике, на все вопросы части Б есть ответ, `failed` — номера «нет», `result` равен выводу по `pass_rule`.
- Источник с признаками закрытых данных нельзя цитировать без подтверждения человека: ошибка — снимок с `closed_markers` без `closed_confirmed`, материал с ПДн без `anonymization_confirmed`, изображение без `no_personal_data_confirmed`.
- У отвеченного В1 есть `frame`, если есть `frame.yaml`; рамка последнего ответа на В1 — одна из версий рамки; метка `[О-n]` в `outputs/comms-strategy.md` ведёт на обещание. Замена: обратная ссылка, те же предмет, период, регион; циклов нет ни в заменах, ни в основаниях; у факта регион источника, источник не синтетика.
- Журнал и состояние: «найдена инструкция» — только у «подозрительного» источника; `state.step` — шаг последнего действия; статусы ворот — по последнему событию ворот; время событий не убывает.
- Расчёт: `claims[].calc` → `outputs/calc/<имя>.json`; число текста равно значению; у факта цитата из каждого входа.
- Запись проверки ссылается на текущий снимок; sha256 файла — `text_sha256`; цитата есть в снимке после нормализации (ADR-0005); отметка выполнения и `message_id` не повторяются (кроме гонки).

**Не проверяется здесь — делают команды CLI:** сверка чисел и отрицаний в цитате (`claim add`); значение против порога (`due`); время и стоимость; запись выходов и версий (`write`); замена и отзыв; «прервано» (`status --brief`); «изменено вне агента» (любая команда).

## Цепочка связей (рамка → ВП → И → У → Вар → О, ПП → Реш)
```
И-n --`quotes[].source`--> У-n --`basis`--> У-m;  У-n <--`replaces` / `replaced_by`--> У-m;  У-n --`dispute.claim`--> У-m
журнал: `source` --> И-n;  `gate.frame` (В1) и `frame.yaml` --`source`--> И-n
У-n --`plan_question`--> ВП-n --`gap`--> П-n --`affects_options`--> Вар-n;  П-n --`closed_by`, `assumption`, `hypothesis`--> У-n
`metric_coverage` --`claims`, `gap`--> У-n, П-n;  ПП-n --`baseline`--> П-n
Вар-n --`differs.from`--> Вар-m;  Вар-n --`objections[].basis`, `forecast`--> У-n;  `recommendation` --`option`, `claims`--> Вар-n, У-n
О-n --`claims`--> У-n (факт);  ПП-n --`hypothesis`--> У-n (гипотеза);  `premortem` --`claim`, `row`--> У-n, ПП-n (защитный)
Реш-n --`option`, `rejected`--> Вар-n;  Реш-n --`basis`, `assumptions`--> У-n;  `conditions[].row` --> ПП-n;  Реш-n <--`replaces` / `replaced_by`--> Реш-m
`metrics.yaml` --`row`--> ПП-n
```

## Признаки приёмки, которые проверяются по файлам
| Признак | Где в файлах |
|---|---|
| С-1: тип; цитата факта; не факт из синтетики; издатель, вид, «о себе»; диапазон, способ проверки | `claims[].type`, `quotes[].check`, `publisher`, `publisher_kind`, `self_reported`, `forecast_range`, `check_method` |
| С-1, С-13: ворота, режим, вопросы, время; стоимость; «изменено вне агента»; полнота | `journal`: `kind`, `gate`, `duration_s`; `cost.jsonl`; `state.yaml: files`; `completeness.yaml` |
| С-2: нет повтора действия; типы и статусы те же | `journal`: `idempotency_hash`, `status`; `claims[].step`, `attempt`; `state.yaml`: `step`, `next_action` |
| С-3, С-4, С-5: прежние версии; замена, отзыв, правка человека | `status`, `status_history`, `replaces`, `replaced_by`, `origin: пользователь`, `kind: исправление`; `history/`, `frame.version`, `gate.frame` |
| С-7, С-8, С-10: синтетика не факт; место цитаты; расчёт; обезличивание; закрытые данные; инструкция в источнике | `type`/`origin: синтетика`, `locator`, `calc`, `anonymized`, `closed_markers`, `closed_confirmed`, `suspicious` |
| С-9: недоступный источник; сбой в журнале | `access`, `access_note`, `fetch.accessed_at`; `kind: ошибка`; `status: прервано` |
| С-14: пути относительные | поля путей (`path`, `text_path`, `files`); `state.yaml` |
| С-1: рамка, откуда поля; действия плана; пробелы, показатели | `frame.yaml`, `gate.frame`, `plan.yaml: branches[].then`; `gaps.yaml`: `reason`, `searched`, `metric_coverage` |
| С-1: варианты, возражения, рекомендация; обещания; план проверки | `options.yaml`; `promises.yaml`; `verification.yaml`: `rows[].kind`, `premortem` |
| С-6: решение, условия отказа с порогом, датой, строкой плана | `decisions.yaml`: `option`, `basis`, `owner`, `conditions`, `review_date`, `commitments` |

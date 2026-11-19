"""Цитата с изображения (ADR-0005, ADR-0011).

У изображения нет текста, поэтому цитата не ищется в снимке: код не может сверить смысл с картинкой.
Вместо этого он проверяет условия записи, и одни и те же для `claim add` и `check`:
- человек подтвердил, что на изображении нет персональных данных (`add --confirm`);
- файл изображения на месте и не менялся с регистрации;
- место записано строго: «экран <название>; элемент <что и где>»;
- факт говорит о том, что видно в названном элементе, а не о поведении и желаниях людей и не о мире за
  экраном (рынок, отрасль, суммы): это гипотеза или другой источник;
- допущение со слов о поведении людей со снимка — отказ, как у факта.
Смысл утверждения код не сверяет и классификатору такие пары не отдаёт: сверяет человек.
"""

from __future__ import annotations

import re
from pathlib import Path

from strateg import project as proj
from strateg.models import (
    IMAGE_CHECK,
    IMAGE_LOCATOR_FORM,
    Claim,
    Quote,
    Source,
    behaviour_words,
    normalize_quote,
    parse_image_locator,
    strip_quoted,
)

# Сведения о мире за экраном: на снимке интерфейса их не видно, даже если баннер их пишет.
_WORLD = re.compile(
    r"(?<![\w-])(?:рын(?:ок|к\w*|оч\w*)|отрасл\w*|млрд|трлн|выручк\w*|прибыл\w*|прогноз\w*)", re.IGNORECASE
)
_STEM_LEN = 4  # слова из общих четырёх первых букв считаются одним словом: «кнопка» и «кнопки»


def from_image(q: Quote) -> bool:
    """Цитата со снимка экрана: описание, которое записал агент, а не текст источника."""
    check = getattr(q, "check", None)  # у пары из задания цитата без записи проверки
    return check is not None and check.method == IMAGE_CHECK


def only_image(c: Claim) -> bool:
    """Действующее утверждение, у которого все цитаты — со снимков экрана."""
    return c.status == "действует" and bool(c.quotes) and all(from_image(q) for q in c.quotes)


def image_sha(root: Path, src: Source, cache: dict[str, str | None] | None = None) -> str | None:
    """sha256 файла изображения; None — файла нет. С `cache` файл читается один раз на источник."""
    if cache is not None and src.id in cache:
        return cache[src.id]
    sha: str | None = None
    if src.path is not None:
        path = root / src.path
        if path.is_file():
            sha = proj.sha256_bytes(path.read_bytes())
    if cache is not None:
        cache[src.id] = sha
    return sha


def locator_problem(locator: str | None) -> str | None:
    if parse_image_locator(locator) is not None:
        return None
    given = f"«{locator}»" if locator else "не указано"
    return (
        f"у цитаты с изображения нужно место «{IMAGE_LOCATOR_FORM}» "
        f"(экран и элемент непустые); сейчас: {given}"
    )


def _stems(text: str) -> set[str]:
    return {w[:_STEM_LEN] for w in re.findall(rf"[^\W\d_]{{{_STEM_LEN},}}", normalize_quote(text))}


def claim_problem(claim_type: str, claim_text: str, quote_text: str, locator: str | None) -> str | None:
    """Утверждение со снимка говорит не о том, что видно: поведение людей, мир за экраном, чужой элемент.

    Правило консервативное: сомнительное — гипотеза. Слова внутри «…» не считаются (видимая подпись).
    """
    if claim_type not in ("факт", "допущение"):
        return None
    for what, text in (("утверждение", claim_text), ("цитата", quote_text)):
        word = behaviour_words(text)
        if word is not None:
            return (
                f"{what} со снимка говорит о поведении или желаниях людей («{word}»), а снимок показывает "
                "только интерфейс: запишите, что видно на экране, а «пользователи не находят…» — "
                "гипотезой с проверкой"
            )
    if claim_type != "факт":
        return None
    world = _WORLD.search(normalize_quote(strip_quoted(claim_text)))
    if world is not None:
        return (
            f"факт со снимка говорит не о том, что видно («{world.group()}»): рынок, отрасль и суммы "
            "со снимка не берутся. Если подпись на экране сообщает это, цитируйте её в «…» и запишите, "
            "что она сообщает; сомневаетесь — запишите гипотезой или возьмите источник с текстом"
        )
    parsed = parse_image_locator(locator)
    if parsed is not None and not _stems(claim_text) & _stems(parsed[1]):
        return (
            f"факт со снимка не называет элемент из места («{parsed[1]}»): опишите в утверждении, что видно "
            "именно в этом элементе; сомневаетесь — запишите гипотезой"
        )
    return None


def source_problem(src: Source, root: Path, cache: dict[str, str | None] | None = None) -> str | None:
    """Источник-изображение не годится для цитаты: нет подтверждения человека или файл изменился."""
    if not src.no_personal_data_confirmed:
        return (
            f"человек не подтвердил, что на изображении {src.id} нет персональных данных: "
            f"`strateg add --confirm {src.id}`; до подтверждения агенту запрещено открывать файл "
            "(хук прав), цитировать его нельзя"
        )
    sha = image_sha(root, src, cache)
    if sha is None:
        return f"файл изображения {src.path} не найден"
    if src.material_sha256 is not None and sha != src.material_sha256:
        return f"файл изображения {src.path} изменён после регистрации: добавьте его заново"
    return None

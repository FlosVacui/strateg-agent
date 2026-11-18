"""Цитата с изображения (ADR-0005, ADR-0011).

У изображения нет текста, поэтому цитата не ищется в снимке: код не может сверить смысл с картинкой.
Вместо этого он проверяет условия записи, и одни и те же для `claim add` и `check`:
- человек подтвердил, что на изображении нет персональных данных (`add --confirm`);
- файл изображения на месте и не менялся с регистрации;
- место записано строго: «экран <название>; элемент <что и где>»;
- факт говорит о том, что видно на экране, а не о поведении и желаниях людей: это гипотеза.
"""

from __future__ import annotations

from pathlib import Path

from strateg import project as proj
from strateg.models import IMAGE_LOCATOR_FORM, Source, behaviour_words, parse_image_locator


def image_sha(root: Path, src: Source) -> str | None:
    """sha256 файла изображения; None — файла нет."""
    if src.path is None:
        return None
    path = root / src.path
    if not path.is_file():
        return None
    return proj.sha256_bytes(path.read_bytes())


def locator_problem(locator: str | None) -> str | None:
    if parse_image_locator(locator) is not None:
        return None
    given = f"«{locator}»" if locator else "не указано"
    return (
        f"у цитаты с изображения нужно место «{IMAGE_LOCATOR_FORM}» "
        f"(экран и элемент непустые); сейчас: {given}"
    )


def behaviour_problem(claim_type: str, claim_text: str) -> str | None:
    """Факт с изображения о поведении или желаниях людей: по снимку экрана этого не видно."""
    if claim_type != "факт":
        return None
    word = behaviour_words(claim_text)
    if word is None:
        return None
    return (
        f"факт с изображения говорит о поведении или желаниях людей («{word}»), а снимок показывает только "
        "интерфейс: запишите, что видно на экране, а «пользователи не находят…» — гипотезой с проверкой"
    )


def source_problem(src: Source, root: Path) -> str | None:
    """Источник-изображение не годится для цитаты: нет подтверждения человека или файл изменился."""
    if not src.no_personal_data_confirmed:
        return (
            f"человек не подтвердил, что на изображении {src.id} нет персональных данных: "
            f"`strateg add --confirm {src.id}`; до подтверждения модель изображение не видит"
        )
    if image_sha(root, src) is None:
        return f"файл изображения {src.path} не найден"
    if src.material_sha256 is not None and image_sha(root, src) != src.material_sha256:
        return f"файл изображения {src.path} изменён после регистрации: добавьте его заново"
    return None

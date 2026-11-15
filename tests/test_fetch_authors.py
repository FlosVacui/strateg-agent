"""Авторы отзывов и комментариев в снимках страниц (ADR-0011): метка по разметке до извлечения текста."""

from __future__ import annotations

import json

import pytest

from helpers import URL, load_yaml, open_fixture
from strateg import fetch as fetchlib
from strateg import safety

NAME = "Олег Нестеров"
BODY = "Подписка стоит сто девяносто девять рублей, это нормально для такого набора услуг."


# Списки признаков записаны и здесь: тест на каждую строку идёт по этой записи, а не по списку из кода.
# Убрали строку из кода — падает сверка списков; добавили — нужен тест на новую строку.
EXPECTED_CLASSES = {
    "commentauthor",
    "commentername",
    "commenter",
    "commentuser",
    "commentusername",
    "commentnickname",
    "commentname",
    "reviewauthor",
    "reviewer",
    "reviewername",
    "reviewuser",
    "reviewusername",
    "reviewnickname",
    "reviewname",
}
EXPECTED_CLASSES_IN_BLOCK = {
    "author",
    "authorname",
    "user",
    "username",
    "nickname",
    "nick",
    "displayname",
    "profilename",
    "fn",
}
EXPECTED_ATTRS = (
    "data-author",
    "data-author-name",
    "data-username",
    "data-user-name",
    "data-nickname",
    "data-reviewer",
    "data-commenter",
    "data-user",
)
EXPECTED_ITEMTYPES = {"review", "comment", "usercomments", "userreview", "questionanswer"}


def test_the_lists_of_markers_are_the_ones_the_tests_cover():
    assert fetchlib.AUTHOR_CLASSES == EXPECTED_CLASSES
    assert fetchlib.AUTHOR_CLASSES_IN_BLOCK == EXPECTED_CLASSES_IN_BLOCK
    assert tuple(fetchlib.AUTHOR_ATTRS) == EXPECTED_ATTRS
    assert fetchlib.REVIEW_ITEMTYPES == EXPECTED_ITEMTYPES


def masked(fragment: str) -> tuple[str, str]:
    html, review = fetchlib.mask_authors(f"<html><body>{fragment}</body></html>")
    return html, review


# --- признаки: по тесту на каждую строку списка ---------------------------------------------------


@pytest.mark.parametrize("cls", sorted(EXPECTED_CLASSES))
def test_each_author_class_is_masked_anywhere(cls):
    html, _ = masked(f'<div><span class="{cls}">{NAME}</span><p>{BODY}</p></div>')
    assert NAME not in html and fetchlib.AUTHOR_LABEL in html and BODY in html


@pytest.mark.parametrize(
    "cls", ["comment-author", "comment__author", "commentAuthor", "Comment_Author", "x review-author y"]
)
def test_spelling_of_the_class_does_not_matter(cls):
    html, _ = masked(f'<div><span class="{cls}">{NAME}</span><p>{BODY}</p></div>')
    assert NAME not in html


@pytest.mark.parametrize("cls", sorted(EXPECTED_CLASSES_IN_BLOCK))
def test_each_general_author_class_is_masked_inside_a_comment_block(cls):
    inside, _ = masked(
        f'<div class="comments"><div><span class="{cls}">{NAME}</span><p>{BODY}</p></div></div>'
    )
    assert NAME not in inside and fetchlib.AUTHOR_LABEL in inside


@pytest.mark.parametrize("cls", sorted(EXPECTED_CLASSES_IN_BLOCK))
def test_general_author_class_outside_a_block_is_an_article_author_and_stays(cls):
    outside, _ = masked(f'<article><span class="{cls}">{NAME}</span><p>{BODY}</p></article>')
    assert NAME in outside and fetchlib.AUTHOR_LABEL not in outside


@pytest.mark.parametrize("kind", sorted(EXPECTED_ITEMTYPES))
def test_each_review_itemtype_masks_itemprop_author(kind):
    html, _ = masked(
        f'<div itemscope itemtype="https://schema.org/{kind.capitalize()}">'
        f'<span itemprop="author">{NAME}</span><p>{BODY}</p></div>'
    )
    assert NAME not in html and fetchlib.AUTHOR_LABEL in html


def test_itemprop_author_with_a_nested_person_is_masked_whole():
    html, _ = masked(
        '<div itemscope itemtype="https://schema.org/Review"><span itemprop="author" itemscope '
        f'itemtype="https://schema.org/Person"><span itemprop="name">{NAME}</span></span><p>{BODY}</p></div>'
    )
    assert NAME not in html and html.count(fetchlib.AUTHOR_LABEL) == 1


@pytest.mark.parametrize("kind", ["NewsArticle", "Article", "BlogPosting", "Report"])
def test_the_author_of_an_article_is_not_masked(kind):
    html, _ = masked(
        f'<article itemscope itemtype="https://schema.org/{kind}"><span itemprop="author">{NAME}</span>'
        f"<p>{BODY}</p></article>"
    )
    assert NAME in html and fetchlib.AUTHOR_LABEL not in html


def test_the_comment_inside_an_article_changes_but_the_article_author_does_not():
    html, _ = masked(
        '<article itemscope itemtype="https://schema.org/NewsArticle">'
        '<span itemprop="author">Автор Статьи</span>'
        '<div itemprop="comment" itemscope itemtype="https://schema.org/Comment">'
        f'<span itemprop="author">{NAME}</span><p>{BODY}</p></div></article>'
    )
    assert "Автор Статьи" in html and NAME not in html and html.count(fetchlib.AUTHOR_LABEL) == 1


@pytest.mark.parametrize("attr", EXPECTED_ATTRS)
def test_each_author_attribute_names_the_author_for_the_block(attr):
    html, _ = masked(
        f'<div class="comment" {attr}="{NAME}"><b>{NAME}</b><p>{BODY} Пишет {NAME.lower()}.</p></div>'
    )
    assert NAME not in html and NAME.lower() not in html and BODY in html


@pytest.mark.parametrize("attr", EXPECTED_ATTRS)
def test_author_attribute_of_the_article_itself_is_not_used(attr):
    html, _ = masked(f'<article {attr}="{NAME}"><b>{NAME}</b><p>{BODY}</p></article>')
    assert NAME in html


def test_numeric_user_id_in_the_attribute_is_ignored():
    html, _ = masked(f'<div class="comment" data-user="48213"><p>{BODY} Номер 48213 в списке.</p></div>')
    assert "48213" in html


@pytest.mark.parametrize(
    "word", ["comment", "review", "feedback", "testimonial", "otzyv", "отзывы", "комментарии"]
)
def test_mention_of_a_nick_inside_a_review_block_is_masked(word):
    html, _ = masked(f'<div class="{word}-list"><p>@olga_k согласна. {BODY} Спросите у @denis.v.</p></div>')
    assert "olga_k" not in html and "denis.v" not in html and html.count(fetchlib.MENTION_LABEL) == 2


def test_mention_outside_a_review_block_and_an_email_stay():
    html, _ = masked(
        f"<article><p>Пишите на help@example.com или в канал @strateg_news. {BODY}</p></article>"
    )
    assert "help@example.com" in html and "@strateg_news" in html


def test_a_whole_block_in_an_author_class_is_not_replaced():
    long_text = "Очень длинный отзыв, который по ошибке лежит внутри блока с классом автора. " * 5
    html, _ = masked(f'<div class="comment"><div class="comment-author">{long_text}</div></div>')
    assert long_text in html


def test_names_in_news_text_and_organizations_are_not_masked():
    html, _ = masked(
        "<article><p>Министр Алексей Петров и компания «Вектор» объявили о повышении цен. "
        "Директор Мария Иванова это подтвердила.</p></article>"
    )
    assert "Алексей Петров" in html and "«Вектор»" in html and "Мария Иванова" in html


# --- страница целиком -----------------------------------------------------------------------------


def test_snapshot_text_has_neither_commenters_nor_json_ld_names():
    html = open_fixture("reviews.html")
    text = safety.inspect_text(fetchlib.extract_page(html).text).text
    for name in ("Иван Котов", "Серёга_77", "olga_k", "Анна Лаптева", "Пётр Сидоров"):
        assert name not in text, name
    assert "Алексей Петров" in text and "«Вектор»" in text  # публичные лица и организации на месте


def test_fallback_text_keeps_labels_instead_of_authors_and_the_article_author():
    html = open_fixture("reviews.html")
    html2, _ = fetchlib.mask_authors(fetchlib.clean_html(html))
    visible = fetchlib.visible_text(html2)
    for name in ("Иван Котов", "Серёга_77", "olga_k", "Анна Лаптева", "Дмитрий"):
        assert name not in visible, name
    assert visible.count(fetchlib.AUTHOR_LABEL) == 4 and fetchlib.MENTION_LABEL in visible
    assert "Мария Иванова" in visible  # автор статьи
    assert "Алексей Петров" in visible


@pytest.fixture
def reviews_page(net, monkeypatch):
    """Страница с отзывами; основной извлекатель не берёт ничего, и в снимок идёт весь видимый текст."""
    monkeypatch.setattr(fetchlib, "extract_html", lambda html: "")
    net.add(URL, open_fixture("reviews.html"))


def test_fetch_masks_authors_before_the_snapshot_is_written(cli, reviews_page, home):
    assert cli("new", "demo")[0] == 0
    code, out, err = cli("fetch", URL, "--json")
    assert code == 0, err
    data = json.loads(out)
    assert data["authors_masked"] == 5  # четыре автора и один «@ник»
    root = home / "demo"
    text = (root / data["snapshot"]).read_text(encoding="utf-8")
    for name in ("Иван Котов", "Серёга_77", "olga_k", "Анна Лаптева", "Дмитрий"):
        assert name not in text, name
    assert text.count(fetchlib.AUTHOR_LABEL) == 4 and fetchlib.MENTION_LABEL in text
    assert "Мария Иванова" in text and "Алексей Петров" in text
    (source,) = load_yaml(root / "sources.yaml")["sources"]
    assert "авторы отзывов" in source["anonymized"]
    assert any("заменены авторы отзывов и комментариев: 5" in w for w in data["warnings"])
    # хеш и цитаты считаются по уже обезличенному тексту
    import hashlib

    assert source["text_sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert fetchlib.AUTHOR_LABEL.encode() in (root / source["text_path"]).read_bytes()


def test_a_quote_with_a_masked_author_is_found_in_the_snapshot(cli, reviews_page, home):
    assert cli("new", "demo")[0] == 0
    assert cli("fetch", URL)[0] == 0
    root = home / "demo"
    claim = {
        "text": "Читатели называют повышение цены неприятным",
        "type": "факт",
        "subject": "реакция читателей",
        "quotes": [
            {
                "source": "И-1",
                "text": "Подписка подорожала, а услуга осталась прежней, это неприятно.",
                "publisher": "компания «Пример-Аналитика»",
                "publisher_kind": "отраслевое исследование",
                "self_reported": False,
            }
        ],
    }
    import yaml

    path = root / ".tmp" / "c.yaml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(yaml.safe_dump(claim, allow_unicode=True), encoding="utf-8")
    code, _, err = cli("claim", "add", "--file", str(path))
    assert code == 0, err


def test_names_in_free_text_of_a_review_are_a_warning_not_a_replacement(cli, reviews_page):
    assert cli("new", "demo")[0] == 0
    code, out, err = cli("fetch", URL, "--json")
    data = json.loads(out)
    warning = next(w for w in data["warnings"] if "имена в свободном тексте" in w)
    assert "Иван" in warning and "не заменены" in warning


def test_default_extractor_drops_the_comments_and_nothing_is_counted(cli, net, home):
    net.add(URL, open_fixture("reviews.html"))
    assert cli("new", "demo")[0] == 0
    code, out, err = cli("fetch", URL, "--json")
    data = json.loads(out)
    assert data["authors_masked"] == 0
    text = (home / "demo" / data["snapshot"]).read_text(encoding="utf-8")
    assert "Иван Котов" not in text and "комментар" not in text.lower()
    assert not any("авторы отзывов" in w for w in data["warnings"])


def test_repeat_without_refresh_returns_the_same_source_and_refresh_rereads(cli, reviews_page, home, net):
    assert cli("new", "demo")[0] == 0
    first = json.loads(cli("fetch", URL, "--json")[1])
    again = json.loads(cli("fetch", URL, "--json")[1])
    assert again["source"] == first["source"] and again["cached"] is True
    refreshed = json.loads(cli("fetch", URL, "--refresh", "--json")[1])
    assert refreshed["refresh"] == "same"  # текст тот же: прежний снимок не трогается
    assert refreshed["source"] == first["source"]


def test_a_snapshot_taken_before_the_masking_is_rechecked_by_refresh(cli, reviews_page, home, monkeypatch):
    """Старый снимок (до замены авторов) при `--refresh` даёт новый источник со ссылкой на прежний."""
    assert cli("new", "demo")[0] == 0
    real = fetchlib.mask_authors
    monkeypatch.setattr(fetchlib, "mask_authors", lambda html: (html, ""))  # поведение до этой версии
    first = json.loads(cli("fetch", URL, "--json")[1])
    root = home / "demo"
    old_text = (root / first["snapshot"]).read_text(encoding="utf-8")
    assert "Иван Котов" in old_text
    monkeypatch.setattr(fetchlib, "mask_authors", real)
    refreshed = json.loads(cli("fetch", URL, "--refresh", "--json")[1])
    assert refreshed["refresh"] == "new" and refreshed["refreshes"] == first["source"]
    new_text = (root / refreshed["snapshot"]).read_text(encoding="utf-8")
    assert "Иван Котов" not in new_text and refreshed["source"] != first["source"]

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
    "pauthor",
    "hcard",
    "vcard",
}
EXPECTED_TAGS = {"cite"}
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
    assert fetchlib.AUTHOR_TAGS_IN_BLOCK == EXPECTED_TAGS
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
def test_each_author_attribute_names_the_author_node_of_the_block(attr):
    html, _ = masked(
        f'<div class="comment" {attr}="{NAME}"><b>{NAME}</b><p>{BODY} Пишет {NAME.lower()}.</p></div>'
    )
    # узел автора заменён, имя ушло и из атрибута; такие же слова в тексте отзыва остаются
    assert NAME not in html and BODY in html and f"Пишет {NAME.lower()}." in html
    assert fetchlib.AUTHOR_LABEL in html


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


# --- блок отзыва: слова класса и id, а не подстрока -----------------------------------------------

ARTICLE_AUTHOR = "Мария Иванова"


@pytest.mark.parametrize(
    "wrapper",
    [
        'class="article-preview"',
        'class="commentary"',
        'class="comments-enabled"',
        'class="review-article"',
        'id="review-2026"',
        'class="page comments-open"',
        'class="no-comments"',
        'class="comment-form"',
        'class="reviewer-guide"',
        'class="review-guide"',
        'class="has-comments"',
        'class="comments-count"',
        'class="comments-closed"',
    ],
)
def test_the_author_of_an_article_inside_a_wrapper_that_only_looks_like_a_review_block_stays(wrapper):
    html, _ = masked(
        f'<div {wrapper}><article><h1>З</h1><span class="author">{ARTICLE_AUTHOR}</span>'
        f"<p>{BODY}</p></article></div>"
    )
    assert ARTICLE_AUTHOR in html and fetchlib.AUTHOR_LABEL not in html


@pytest.mark.parametrize(
    "wrapper",
    [
        'class="comments"',
        'class="comment-list"',
        'class="reviews"',
        'class="review-item"',
        'class="commentList"',
        'class="user_reviews"',
        'class="otzyv"',
        'id="comments"',
        'id="review-48213"',
        'class="comments-wrap"',
    ],
)
def test_the_wrapper_with_a_whole_word_of_a_review_block_is_a_block(wrapper):
    html, _ = masked(f'<div {wrapper}><span class="author">{NAME}</span><p>{BODY}</p></div>')
    assert NAME not in html and fetchlib.AUTHOR_LABEL in html


REAL_BLOCKS = [
    ('class="post-comments"', "username"),
    ('class="article-comments"', "username"),
    ('class="news-comments"', "username"),
    ('class="page-comments"', "user"),
    ('class="story-comments"', "author"),
    ('class="reviews-page"', "username"),
    ('class="reviews-open"', "username"),
    ('class="reviews-with-photos"', "username"),
    ('class="user-reviews-summary"', "username"),
    ('class="reviews-on"', "username"),
    ('id="article-comments"', "username"),
    ('id="comments-news"', "username"),
    ('class="comments-post-list"', "author"),
]


@pytest.mark.parametrize(("wrapper", "author_class"), REAL_BLOCKS)
def test_a_real_comment_block_with_a_page_word_or_an_unrelated_word_is_still_a_block(wrapper, author_class):
    """Слова страницы до слова блока и слова не про состояние блок не отменяют: имя комментатора заменено."""
    item = f'<div class="item"><span class="{author_class}">{NAME}</span><p>{BODY}</p></div>'
    html, _ = masked(f"<div {wrapper}>{item}</div>")
    assert NAME not in html and fetchlib.AUTHOR_LABEL in html


def test_the_data_author_attribute_of_a_comment_post_block_is_replaced():
    html, _ = masked(f'<div class="comment-post" data-author="{NAME}"><p>{BODY}</p></div>')
    assert NAME not in html and fetchlib.AUTHOR_LABEL in html


def test_open_is_a_state_of_a_page_wrapper_and_a_block_state_of_a_reviews_list():
    """`comments-open` у обёртки с заголовком или статьёй — состояние страницы; с отзывами внутри — блок."""
    author = f'<span class="author">{ARTICLE_AUTHOR}</span>'
    page, _ = masked(f'<body class="comments-open"><div><h1>З</h1>{author}</div></body>')
    assert ARTICLE_AUTHOR in page
    block, _ = masked(f'<div class="comments-open"><span class="username">{NAME}</span><p>{BODY}</p></div>')
    assert NAME not in block


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("comments", True),
        ("Comments", True),
        ("commentAuthor-list", True),
        ("review_item", True),
        ("preview", False),
        ("commentary", False),
        ("comments-enabled", False),
        ("review-article", False),
        ("review-2026", False),
        ("review-2026-list", False),
        ("review-48213", True),
        ("overview", False),
        ("post-comments", True),
        ("article-comments", True),
        ("news-comments", True),
        ("reviews-page", True),
        ("reviews-with-photos", True),
        ("comments-news", True),
        ("review-guide", False),
        ("has-comments", False),
        ("comments-count", False),
        ("comments-open", False),  # без узла: состояние
    ],
)
def test_name_is_a_block_by_whole_words(value, expected):
    assert fetchlib._name_is_block(value) is expected


def test_attribute_name_is_replaced_only_in_the_author_node_and_not_in_text():
    html, _ = masked(
        '<div class="comments"><div class="comment" data-author="ан"><b>ан</b>'
        "<p>Апрельский план: язык, ананас и банан остаются.</p></div></div>"
    )
    assert "Апрельский план: язык, ананас и банан остаются." in html and fetchlib.AUTHOR_LABEL in html


def test_attribute_name_on_a_wrapper_does_not_touch_the_news_text():
    html, _ = masked(
        f'<div class="comments-wrap" data-author="{ARTICLE_AUTHOR}"><article>'
        f"<p>Директор {ARTICLE_AUTHOR} сообщила о планах.</p></article></div>"
    )
    assert f"Директор {ARTICLE_AUTHOR} сообщила о планах." in html


# --- ещё признаки автора внутри блока отзыва ------------------------------------------------------


@pytest.mark.parametrize(
    "author",
    [
        f"<cite>{NAME}</cite>",
        f'<span class="p-author h-card">{NAME}</span>',
        f'<span class="h-card">{NAME}</span>',
        f'<span class="vcard">{NAME}</span>',
        f'<a class="u-url p-name" href="/u/ivan">{NAME}</a>',
        f'<a rel="author" href="/u/ivan">{NAME}</a>',
        f'<a rel="nofollow author" href="/u/ivan">{NAME}</a>',
    ],
)
def test_microformats_cite_and_rel_author_inside_a_review_block_are_authors(author):
    html, _ = masked(f'<div class="comments"><div class="comment">{author}<p>{BODY}</p></div></div>')
    assert NAME not in html and fetchlib.AUTHOR_LABEL in html


@pytest.mark.parametrize(
    "author",
    [
        f"<cite>{NAME}</cite>",
        f'<span class="p-author h-card">{NAME}</span>',
        f'<a class="u-url p-name" href="/u/ivan">{NAME}</a>',
        f'<a rel="author" href="/u/ivan">{NAME}</a>',
    ],
)
def test_the_same_markup_outside_a_review_block_is_the_author_of_the_article(author):
    html, _ = masked(f"<article>{author}<p>{BODY}</p></article>")
    assert NAME in html and fetchlib.AUTHOR_LABEL not in html


def test_probes_of_other_markup_from_the_development_corpus():
    """Пробы разметки отзывов (корпус разработки, 9 из 12): что находится, а что остаётся (ADR-0011)."""
    from instruction_corpus_independent import AUTHOR_PROBES

    found = {}
    for name, html, _was in AUTHOR_PROBES:
        out, _ = fetchlib.mask_authors(f"<html><body>{html}</body></html>")
        found[name] = "Иван Котов" not in out and "ivan_k" not in out
        assert "Иван Котов" not in fetchlib.visible_text(out) or not found[name]
    assert sum(found.values()) == 9 and len(found) == 12
    assert [n for n, ok in found.items() if not ok] == [
        "b в начале комментария",
        "ссылка user-link в отзыве",
        "класс who без слов блока",
    ]


# --- снимок, снятый до замены авторов, после `fetch --refresh` ------------------------------------


@pytest.fixture
def two_snapshots(cli, reviews_page, home, monkeypatch):
    """И-1 снят «прежней версией» (авторов не заменяли), И-2 — перечитан с заменой."""
    assert cli("new", "demo")[0] == 0
    real = fetchlib.mask_authors
    monkeypatch.setattr(fetchlib, "mask_authors", lambda html: (html, ""))
    first = json.loads(cli("fetch", URL, "--json")[1])
    monkeypatch.setattr(fetchlib, "mask_authors", real)
    return first, home / "demo"


def test_refresh_says_the_previous_snapshot_stays_and_may_hold_names(cli, two_snapshots):
    first, root = two_snapshots
    code, out, err = cli("fetch", URL, "--refresh", "--json")
    assert code == 0, err
    data = json.loads(out)
    assert data["refresh"] == "new" and data["prior_unmasked"] == first["source"]
    note = next(w for w in data["warnings"] if "снят до замены авторов отзывов" in w)
    assert first["source"] in note and "остался в sources/" in note and "могут быть имена" in note
    assert "Иван Котов" in (root / first["snapshot"]).read_text(encoding="utf-8")  # он действительно остался


def test_status_and_pack_do_not_hide_the_previous_snapshot(cli, two_snapshots, tmp_path):
    first, root = two_snapshots
    assert cli("fetch", URL, "--refresh")[0] == 0
    out = cli("status")[1]
    assert f"прежний снимок {first['source']} снят до замены авторов отзывов" in out
    assert json.loads(cli("status", "--json")[1])["sources"]["unmasked_snapshots"] == [first["source"]]
    code, out, err = cli("pack", "--out", str(tmp_path / "demo.zip"), "--json")
    assert code == 0, err
    assert json.loads(out)["unmasked_snapshots"] == [first["source"]]
    code, out, err = cli("pack", "--out", str(tmp_path / "demo2.zip"))
    assert "имена в них не заменены" in out and "в архиве нет" not in out.lower().replace(
        "в архиве нет .tmp", ""
    )
    assert f"прежний снимок {first['source']}" in out


def test_pack_of_a_clean_project_keeps_its_promise(cli, reviews_page, tmp_path):
    assert cli("new", "demo")[0] == 0
    assert cli("fetch", URL)[0] == 0
    code, out, err = cli("pack", "--out", str(tmp_path / "demo.zip"))
    assert code == 0, err
    assert "исходников материалов с персональными данными." in out and "прежние снимки" not in out


def test_no_note_when_both_snapshots_were_masked_or_the_text_is_the_same(cli, reviews_page):
    assert cli("new", "demo")[0] == 0
    assert cli("fetch", URL)[0] == 0
    data = json.loads(cli("fetch", URL, "--refresh", "--json")[1])
    assert data["refresh"] == "same" and not data["prior_unmasked"]
    assert json.loads(cli("status", "--json")[1])["sources"]["unmasked_snapshots"] == []


def test_the_second_refresh_does_not_name_a_previous_snapshot_whose_authors_are_already_replaced(
    cli, two_snapshots, net
):
    """И-1 снят до замены, И-2 — с метками. Третья перепроверка называет только И-1."""
    first, root = two_snapshots
    second = json.loads(cli("fetch", URL, "--refresh", "--json")[1])
    assert second["prior_unmasked"] == first["source"]
    page = open_fixture("reviews.html").replace("</body>", f"<p>{BODY} Добавлено позже.</p></body>")
    net.add(URL, page)
    third = json.loads(cli("fetch", URL, "--refresh", "--json")[1])
    assert third["refresh"] == "new" and third["refreshes"] == second["source"]
    assert not third["prior_unmasked"]
    assert not any("снят до замены авторов отзывов" in w for w in third["warnings"])
    assert json.loads(cli("status", "--json")[1])["sources"]["unmasked_snapshots"] == [first["source"]]
    out = cli("status")[1]
    assert f"прежний снимок {first['source']} снят до замены" in out
    assert f"прежний снимок {second['source']} снят до замены" not in out

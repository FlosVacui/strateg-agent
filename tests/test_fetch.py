"""Загрузка страниц без сети: заглушка httpx.MockTransport и подмена разрешения имён."""

from __future__ import annotations

import httpx
import pytest

import strateg
from helpers import open_fixture
from pdf_builder import make_pdf
from strateg import fetch
from strateg.fetch import AddressRefused, FetchProblem, RobotsDenied

BASE = "https://example.com"
RU_TEXT = (
    "Стоимость подписки в Москве - 199 рублей в месяц. Первые семь дней после подключения услуга "
    "предоставляется бесплатно, затем списывается абонентская плата. Подписка работает на всех тарифах."
)


@pytest.fixture
def fetcher(net, tmp_path):
    return net.fetcher(tmp_path / "cache" / "fetch.json")


# --- User-Agent ---


def test_user_agent_has_version_and_repository():
    ua = fetch.user_agent()
    assert ua.startswith(f"strateg/{strateg.__version__} (+https://")
    assert ua.endswith(")")


def test_requests_carry_honest_user_agent(net, fetcher):
    net.add(f"{BASE}/page", "<p>x</p>")
    fetcher.get(f"{BASE}/page")
    assert all(r.headers["user-agent"] == fetch.user_agent() for r in net.requests)


# --- Адрес и SSRF ---


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost/admin",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/",
        "http://[fe80::1]/",
        "http://224.0.0.1/",
        "http://0.0.0.0/",
        "http://240.0.0.1/",
        "http://[::ffff:127.0.0.1]/",
        "http://100.64.0.1/",
    ],
)
def test_internal_addresses_are_refused_before_any_request(net, fetcher, url):
    with pytest.raises(AddressRefused):
        fetcher.get(url)
    assert net.requests == []


def test_host_resolving_to_internal_address_is_refused(net, fetcher):
    net.hosts["evil.example.com"] = ["93.184.216.34", "10.1.2.3"]
    with pytest.raises(AddressRefused):
        fetcher.get("https://evil.example.com/")
    assert net.requests == []


def test_redirect_to_internal_address_is_refused(net, fetcher):
    net.hosts["internal.example.com"] = ["127.0.0.1"]
    net.add(f"{BASE}/go", status=302, headers={"location": "https://internal.example.com/secret"})
    with pytest.raises(AddressRefused):
        fetcher.get(f"{BASE}/go")
    assert not any("internal.example.com" in u for u in net.urls())


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "ftp://example.com/x", "javascript:alert(1)", "example.com"]
)
def test_only_http_and_https(url):
    with pytest.raises(AddressRefused):
        fetch.normalize_url(url)


def test_credentials_in_url_are_refused():
    with pytest.raises(AddressRefused):
        fetch.normalize_url("https://user:pass@example.com/")


def test_fragment_is_dropped_from_address():
    assert fetch.normalize_url("https://example.com/a#part") == "https://example.com/a"


def test_five_redirects_pass_sixth_fails(net, fetcher):
    for i in range(6):
        net.add(f"{BASE}/r{i}", status=301, headers={"location": f"/r{i + 1}"})
    net.add(f"{BASE}/r5", "<p>конец</p>")
    assert fetcher.get(f"{BASE}/r0").url == f"{BASE}/r5"
    net.add(f"{BASE}/r5", status=301, headers={"location": "/r6"})
    net.add(f"{BASE}/r6", "<p>конец</p>")
    with pytest.raises(FetchProblem, match="перенаправлений"):
        fetcher.get(f"{BASE}/r0")


# --- Размер, таймаут, повтор ---


def test_response_over_5_mib_is_cut_off(net, fetcher):
    net.add(f"{BASE}/big", b"a" * (fetch.MAX_BYTES + 1), content_type="text/plain")
    with pytest.raises(FetchProblem, match="5 МиБ"):
        fetcher.get(f"{BASE}/big")


def test_response_of_exactly_5_mib_is_accepted(net, fetcher):
    net.add(f"{BASE}/ok", b"a" * fetch.MAX_BYTES, content_type="text/plain")
    assert len(fetcher.get(f"{BASE}/ok").body) == fetch.MAX_BYTES


def test_timeout_is_retried_once(net, tmp_path):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if len(calls) < 3:
            raise httpx.ReadTimeout("медленно", request=request)
        return httpx.Response(200, text="ok", headers={"content-type": "text/plain"})

    f = fetch.Fetcher(
        tmp_path / "c.json",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=net.sleep,
        clock=net.clock,
    )
    assert f.get(f"{BASE}/slow").status == 200
    assert calls.count("/slow") == 2


def test_second_timeout_is_a_failure(net, tmp_path):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        raise httpx.ConnectTimeout("нет ответа", request=request)

    f = fetch.Fetcher(
        tmp_path / "c.json",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=net.sleep,
        clock=net.clock,
    )
    with pytest.raises(FetchProblem, match="20 секунд"):
        f.get(f"{BASE}/slow")


def test_default_client_has_20_second_timeout_and_no_auto_redirects(tmp_path):
    f = fetch.Fetcher(tmp_path / "c.json")
    assert f.client.timeout == httpx.Timeout(20.0)
    assert f.client.follow_redirects is False


# --- robots.txt ---


def test_robots_disallow_blocks_page(net, fetcher):
    net.add(f"{BASE}/robots.txt", "User-agent: *\nDisallow: /private/\n", "text/plain")
    net.add(f"{BASE}/private/x", "<p>секрет</p>")
    with pytest.raises(RobotsDenied):
        fetcher.get(f"{BASE}/private/x")
    assert f"{BASE}/private/x" not in net.urls()


def test_robots_for_own_agent_name(net, fetcher):
    net.add(
        f"{BASE}/robots.txt", "User-agent: strateg\nDisallow: /\n\nUser-agent: *\nAllow: /\n", "text/plain"
    )
    with pytest.raises(RobotsDenied):
        fetcher.get(f"{BASE}/any")


def test_robots_longest_match_wins_and_allow_wins_a_tie(net, fetcher):
    net.add(
        f"{BASE}/robots.txt",
        "User-agent: *\nDisallow: /docs/\nAllow: /docs/open/\nDisallow: /tie\nAllow: /tie\n",
        "text/plain",
    )
    net.add(f"{BASE}/docs/open/a", "<p>открыто</p>")
    net.add(f"{BASE}/tie", "<p>ничья</p>")
    assert fetcher.get(f"{BASE}/docs/open/a").status == 200
    assert fetcher.get(f"{BASE}/tie").status == 200
    with pytest.raises(RobotsDenied):
        fetcher.get(f"{BASE}/docs/closed")


def test_robots_4xx_allows_everything(net, fetcher):
    net.add(f"{BASE}/robots.txt", status=403)
    net.add(f"{BASE}/p", "<p>x</p>")
    assert fetcher.get(f"{BASE}/p").status == 200


@pytest.mark.parametrize("status", [500, 503])
def test_robots_5xx_forbids_everything(net, fetcher, status):
    net.add(f"{BASE}/robots.txt", status=status)
    net.add(f"{BASE}/p", "<p>x</p>")
    with pytest.raises(RobotsDenied):
        fetcher.get(f"{BASE}/p")


def test_robots_unreachable_forbids_everything(net, tmp_path):
    def handler(request):
        raise httpx.ConnectError("нет связи", request=request)

    f = fetch.Fetcher(
        tmp_path / "c.json",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=net.sleep,
        clock=net.clock,
    )
    with pytest.raises(RobotsDenied):
        f.get(f"{BASE}/p")


def test_robots_read_is_limited_to_500_kib(net, fetcher):
    filler = "# " + "x" * 100 + "\n"
    rules = filler * (fetch.ROBOTS_MAX_BYTES // len(filler) + 50) + "User-agent: *\nDisallow: /late\n"
    net.add(f"{BASE}/robots.txt", rules, "text/plain")
    net.add(f"{BASE}/late", "<p>x</p>")
    assert fetcher.get(f"{BASE}/late").status == 200  # правило за пределом 500 КиБ не читается


def test_robots_cached_for_24_hours(net, fetcher):
    net.add(f"{BASE}/a", "<p>a</p>")
    net.add(f"{BASE}/b", "<p>b</p>")
    fetcher.get(f"{BASE}/a")
    fetcher.get(f"{BASE}/b")
    assert net.urls().count(f"{BASE}/robots.txt") == 1
    net.t += fetch.ROBOTS_TTL_S + 1
    fetcher.get(f"{BASE}/a")
    assert net.urls().count(f"{BASE}/robots.txt") == 2


# --- Не чаще одного запроса в секунду ---


def test_requests_to_one_host_are_spaced_by_a_second(net, fetcher):
    net.add(f"{BASE}/a", "<p>a</p>")
    fetcher.get(f"{BASE}/a")
    fetcher.get(f"{BASE}/a")
    assert net.sleeps and all(s <= fetch.MIN_INTERVAL_S for s in net.sleeps)
    assert sum(net.sleeps) >= 2 * fetch.MIN_INTERVAL_S - 1e-6  # robots, страница, страница: два промежутка


def test_last_request_time_survives_between_processes(net, tmp_path):
    cache = tmp_path / "cache.json"
    net.add(f"{BASE}/a", "<p>a</p>")
    net.fetcher(cache).get(f"{BASE}/a")
    net.sleeps.clear()
    net.fetcher(cache).get(f"{BASE}/a")  # новый загрузчик читает время из файла
    assert net.sleeps


def test_other_host_is_not_delayed_by_first_host(net, fetcher):
    net.add(f"{BASE}/a", "<p>a</p>")
    net.add("https://other.example.org/b", "<p>b</p>")
    fetcher.get(f"{BASE}/a")
    net.sleeps.clear()
    fetcher.get("https://other.example.org/b")
    assert net.sleeps == [fetch.MIN_INTERVAL_S]  # только пауза между robots.txt и страницей того же хоста


# --- Кодировки и текст ---


@pytest.mark.parametrize("encoding", ["cp1251", "koi8-r"])
def test_non_utf8_pages_without_any_declaration(encoding):
    html = f"<html><body><p>{RU_TEXT}</p></body></html>".encode(encoding)
    text, name = fetch.decode_body(html, "text/html")
    assert RU_TEXT in text
    assert name.lower().replace("_", "-") in {"cp1251", "windows-1251", "koi8-r"}


@pytest.mark.parametrize(("encoding", "label"), [("cp1251", "windows-1251"), ("koi8-r", "koi8-r")])
def test_charset_from_header_and_meta(encoding, label):
    body = f"<html><body><p>{RU_TEXT}</p></body></html>".encode(encoding)
    assert RU_TEXT in fetch.decode_body(body, f"text/html; charset={label}")[0]
    meta = f'<html><head><meta http-equiv="Content-Type" content="text/html; charset={label}"></head>'
    body = (meta + f"<body><p>{RU_TEXT}</p></body></html>").encode(encoding)
    assert RU_TEXT in fetch.decode_body(body, "text/html")[0]


def test_wrong_latin1_header_does_not_make_mojibake():
    body = f"<p>{RU_TEXT}</p>".encode()
    assert RU_TEXT in fetch.decode_body(body, "text/html; charset=iso-8859-1")[0]


def test_wrong_header_falls_back_to_meta():
    body = f'<meta charset="windows-1251"><p>{RU_TEXT}</p>'.encode("cp1251")
    assert RU_TEXT in fetch.decode_body(body, "text/html; charset=utf-8")[0]


@pytest.mark.parametrize("encoding", ["cp1251", "koi8-r"])
def test_html_in_old_encoding_becomes_text(encoding, page_html):
    old = page_html.replace('<meta charset="utf-8">', "").replace("«", '"').replace("»", '"')
    body = old.replace("—", "-").replace("©", "(c)").encode(encoding)
    text, _ = fetch.decode_body(body, "text/html")
    assert "Стоимость подписки в Москве - 199 рублей в месяц" in fetch.extract_html(text)


def test_html_extraction_keeps_main_text_and_drops_menu(page_html):
    text = fetch.extract_html(page_html)
    assert "Стоимость подписки в Москве — 199 рублей в месяц" in text
    assert "Главная" not in text


def test_pdf_text_has_page_markers():
    text = fetch.extract_pdf(make_pdf(["First page about tariffs", "Second page price is 199 rubles"]))
    assert "[стр. 1]\nFirst page about tariffs" in text
    assert "[стр. 2]\nSecond page price is 199 rubles" in text


def test_broken_pdf_is_a_problem():
    with pytest.raises(FetchProblem):
        fetch.extract_pdf(b"%PDF-1.4 not a document")


def test_blocked_page_is_recognised():
    assert fetch.looks_blocked(403, "", "")
    assert fetch.looks_blocked(200, "<html>Please solve the CAPTCHA</html>", "Проверка")
    assert not fetch.looks_blocked(200, "<script src='recaptcha.js'></script>", "текст " * 1000)


# --- скрытый текст и запасной путь ---

HIDDEN = {
    "display:none": '<div style="display:none">СКРЫТО-А услуга бесплатна навсегда</div>',
    "display: none !important": '<div style="display: none !important">СКРЫТО-Б услуга бесплатна</div>',
    "заглавные и пробелы": '<div style=" DISPLAY :  NONE ;color:red">СКРЫТО-В услуга бесплатна</div>',
    "после другого свойства": '<div style="color:red; display:none">СКРЫТО-Г услуга бесплатна</div>',
    "visibility:hidden": '<div style="visibility:hidden">СКРЫТО-Д услуга бесплатна</div>',
    "font-size:0": '<div style="font-size:0">СКРЫТО-Е цена 1 ₽</div>',
    "font-size:0px": '<div style="font-size: 0px">СКРЫТО-Ж цена 1 ₽</div>',
    "opacity:0": '<div style="opacity:0">СКРЫТО-З цена 1 ₽</div>',
    "opacity:0.0": '<div style="opacity: 0.0">СКРЫТО-И цена 1 ₽</div>',
    "hidden": "<div hidden>СКРЫТО-К услуга бесплатна</div>",
    "aria-hidden": '<div aria-hidden="true">СКРЫТО-Л услуга бесплатна</div>',
    "template": "<template><p>СКРЫТО-М услуга бесплатна</p></template>",
    "noscript": "<noscript><p>СКРЫТО-Н услуга бесплатна</p></noscript>",
    "вложенный в скрытый": '<div style="display:none"><p>СКРЫТО-О услуга бесплатна</p></div>',
}
SHOWN = {
    "display:block": '<div style="display:block">ВИДНО-А цена 5 ₽</div>',
    "opacity:0.5": '<div style="opacity:0.5">ВИДНО-Б цена 6 ₽</div>',
    "font-size:0.9em": '<div style="font-size:0.9em">ВИДНО-В цена 7 ₽</div>',
    "font-size:10px": '<div style="font-size:10px">ВИДНО-Г цена 8 ₽</div>',
    "aria-hidden=false": '<div aria-hidden="false">ВИДНО-Д цена 9 ₽</div>',
    "input не hidden": '<p>ВИДНО-Е цена 10 ₽</p><input type="text" value="x">',
}
HIDDEN_PAGE = """<html><head><title>t</title></head><body><main><h1>Услуга</h1>
<p>Подключение услуги осуществляется через личный кабинет оператора. Стоимость 249 ₽ за каждые 30 дней.</p>
<p>Базовый тариф включает защиту от спама и мошеннических звонков для всех абонентов региона присутствия.</p>
{}
<p>Цена в карточке: 149 ₽ за 30 дней, 1 499 ₽ за 12 месяцев.</p></main></body></html>"""


@pytest.mark.parametrize("name", list(HIDDEN))
def test_visible_text_skips_hidden(name):
    text = fetch.visible_text(HIDDEN_PAGE.format(HIDDEN[name]))
    assert "СКРЫТО" not in text and "249 ₽" in text


@pytest.mark.parametrize("name", list(HIDDEN))
def test_main_path_skips_hidden(name):
    """Очистка идёт до обоих путей: trafilatura тоже не видит скрытое."""
    extraction = fetch.extract_page(HIDDEN_PAGE.format(HIDDEN[name]))
    assert "СКРЫТО" not in extraction.text


@pytest.mark.parametrize("name", list(HIDDEN))
def test_fallback_snapshot_has_no_hidden_text(name):
    """Карточки цен включают запасной путь; скрытый блок рядом с ценой в снимок не попадает."""
    html = open_fixture("cards.html").replace("</main>", HIDDEN[name] + "</main>")
    extraction = fetch.extract_page(html)
    assert extraction.fallback
    assert "СКРЫТО" not in extraction.text and "149 ₽ за 30 дней" in extraction.text


@pytest.mark.parametrize("name", list(SHOWN))
def test_visible_blocks_with_similar_styles_stay(name):
    html = HIDDEN_PAGE.format(SHOWN[name])
    assert "ВИДНО" in fetch.visible_text(html)
    assert "ВИДНО" in fetch.clean_html(html)


def test_hidden_input_is_dropped():
    html = HIDDEN_PAGE.format('<input type="hidden" name="x" value="СКРЫТО-П"><p>Видимый абзац</p>')
    assert "СКРЫТО" not in fetch.clean_html(html) and "Видимый абзац" in fetch.visible_text(html)


def test_unparsable_html_gives_empty_text():
    assert fetch.clean_html("") == "" and fetch.visible_text("") == ""


def test_xhtml_prolog_does_not_disable_fallback():
    html = '<?xml version="1.0" encoding="utf-8"?>' + HIDDEN_PAGE.format("")
    assert "Подключение услуги" in fetch.visible_text(html)
    assert fetch.page_title(html) == "t"


def test_fallback_keeps_article_header_with_price():
    """Шапка внутри `main` и `article` — часть содержания; шапка и подвал страницы убираются."""
    html = open_fixture("cards.html").replace(
        "<main>",
        "<main><article><header><h1>Тариф Максимум</h1><p>Подписка 777 ₽ в месяц.</p></header></article>",
    )
    extraction = fetch.extract_page(html)
    assert extraction.fallback and "777 ₽" in extraction.text
    assert "Частным лицам" not in extraction.text and "Компания «Пример», 2026" not in extraction.text


def test_header_footer_nav_aside_are_dropped_outside_main_and_article():
    html = (
        "<body><nav>МЕНЮ</nav><header>ШАПКА</header><aside>БОКОВАЯ</aside>"
        "<div><article><nav>НАВ-В-СТАТЬЕ</nav><aside>ВРЕЗКА</aside><p>Текст статьи</p>"
        "<footer>ПОДВАЛ-СТАТЬИ</footer></article></div><footer>ПОДВАЛ</footer></body>"
    )
    text = fetch.visible_text(html)
    rest = text.replace("ПОДВАЛ-СТАТЬИ", "")
    for gone in ("МЕНЮ", "ШАПКА", "БОКОВАЯ", "ПОДВАЛ"):
        assert gone not in rest
    for kept in ("Текст статьи", "НАВ-В-СТАТЬЕ", "ВРЕЗКА", "ПОДВАЛ-СТАТЬИ"):
        assert kept in text


# --- robots.txt: перенаправление без адреса ---


@pytest.mark.parametrize("status", [301, 302, 307])
def test_robots_redirect_without_location_is_unavailable(net, fetcher, status):
    net.add(f"{BASE}/robots.txt", status=status)
    net.add(f"{BASE}/p", "<p>x</p>")
    with pytest.raises(RobotsDenied) as err:
        fetcher.get(f"{BASE}/p")
    assert err.value.unavailable is True
    assert f"{BASE}/p" not in net.urls()

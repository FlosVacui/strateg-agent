"""Сеть `fetch` (ADR-0004, ADR-0011): соединение на проверенный адрес, `robots.txt` не читается по http.

Без сети: вместо настоящего сетевого слоя стоит запись вызовов, вместо DNS — подмена `resolve_host`.
"""

from __future__ import annotations

import httpx
import pytest

from conftest import PUBLIC_IP
from strateg import fetch
from strateg.fetch import AddressRefused, FetchProblem, RobotsDenied

BASE = "https://example.com"


class Recorder:
    """Сетевой слой вместо настоящего: помнит, к какому адресу просили соединиться, и не соединяется."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.calls.append((host, port))
        raise ConnectionRefusedError("в тесте соединения нет")

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise AssertionError("сокет файловой системы не должен вызываться")

    def sleep(self, seconds):
        pass


def pinned_client() -> tuple[httpx.Client, Recorder]:
    transport = fetch.PinnedTransport()
    recorder = Recorder()
    transport._pool._network_backend.inner = recorder
    return httpx.Client(transport=transport, follow_redirects=False, trust_env=False), recorder


# --- закрепление проверенного адреса --------------------------------------------------------------


def test_default_client_goes_through_the_pinned_backend(tmp_path):
    fetcher = fetch.Fetcher(tmp_path / "fetch.json")
    transport = fetcher.client._transport
    assert isinstance(transport, fetch.PinnedTransport)
    assert isinstance(transport._pool._network_backend, fetch.PinnedBackend)


def test_connection_goes_to_the_checked_address_not_to_the_name(net):
    net.hosts["site.example.com"] = [PUBLIC_IP]
    client, recorder = pinned_client()
    with pytest.raises(ConnectionRefusedError), client.stream("GET", "http://site.example.com/page"):
        pass
    assert recorder.calls == [(PUBLIC_IP, 80)]


def test_https_connection_also_goes_to_the_address_and_the_url_keeps_the_name(net):
    net.hosts["site.example.com"] = [PUBLIC_IP]
    client, recorder = pinned_client()
    request = client.build_request("GET", "https://site.example.com/page")
    with pytest.raises(ConnectionRefusedError), client.send(request, stream=True):
        pass
    # соединение по IP, а имя осталось в адресе и заголовке Host: TLS пойдёт с именем сайта
    assert recorder.calls == [(PUBLIC_IP, 443)]
    assert request.url.host == "site.example.com" and request.headers["host"] == "site.example.com"


def test_dns_changed_between_the_check_and_the_connection_is_refused_before_any_connect(net, monkeypatch):
    """Подмена DNS: проверка адреса видит публичный адрес, при соединении имя уже указывает на внутренний."""
    answers = iter([[PUBLIC_IP], ["10.0.0.5"]])
    monkeypatch.setattr(fetch, "resolve_host", lambda host: next(answers))
    client, recorder = pinned_client()
    fetch.check_address("http://rebind.example.com/")  # первая проверка: публичный адрес
    with (
        pytest.raises(AddressRefused, match="внутренний"),
        client.stream("GET", "http://rebind.example.com/"),
    ):
        pass
    assert recorder.calls == []  # к внутреннему адресу не соединялись


def test_fetcher_refuses_a_page_whose_name_turned_internal_after_the_checks(net, tmp_path, monkeypatch):
    answers = iter(
        [[PUBLIC_IP], ["127.0.0.1"]]
    )  # проверка адреса страницы (robots.txt уже в кэше), соединение
    monkeypatch.setattr(fetch, "resolve_host", lambda host: next(answers))
    client, recorder = pinned_client()
    fetcher = fetch.Fetcher(
        tmp_path / "cache" / "fetch.json", client=client, sleep=net.sleep, clock=net.clock
    )
    fetcher._save(
        {"robots": {"http://rebind.example.com": {"at": net.t, "mode": "allow", "text": "", "ttl": 3600}}}
    )
    with pytest.raises(AddressRefused, match="локальный"):
        fetcher.get("http://rebind.example.com/page")
    assert recorder.calls == []


@pytest.mark.parametrize(
    "addresses", [[PUBLIC_IP, "10.1.2.3"], ["192.168.0.1", PUBLIC_IP], ["::1"], ["169.254.169.254"]]
)
def test_any_internal_address_among_the_answers_refuses_the_connection(net, addresses):
    net.hosts["mixed.example.com"] = addresses
    client, recorder = pinned_client()
    with pytest.raises(AddressRefused), client.stream("GET", "http://mixed.example.com/"):
        pass
    assert recorder.calls == []


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "[::1]", "10.0.0.1", "[::ffff:10.0.0.1]", "localhost", "a.localhost"]
)
def test_internal_addresses_in_the_url_are_refused_by_the_backend_too(net, host):
    client, recorder = pinned_client()
    with pytest.raises(AddressRefused), client.stream("GET", f"http://{host}/"):
        pass
    assert recorder.calls == []


def test_the_next_checked_address_is_tried_when_the_first_does_not_answer(net):
    net.hosts["two.example.com"] = [PUBLIC_IP, "93.184.216.35"]
    client, recorder = pinned_client()
    with pytest.raises(ConnectionRefusedError), client.stream("GET", "http://two.example.com/"):
        pass
    assert recorder.calls == [(PUBLIC_IP, 80), ("93.184.216.35", 80)]


def test_unresolved_name_is_a_fetch_problem_not_a_crash(monkeypatch):
    def gone(host):
        raise FetchProblem("источник недоступен: ошибка", f"имя {host} не найдено")

    monkeypatch.setattr(fetch, "resolve_host", gone)
    client, recorder = pinned_client()
    with pytest.raises(FetchProblem), client.stream("GET", "http://nowhere.example.com/"):
        pass


def test_unix_socket_is_refused():
    backend = fetch.PinnedBackend(Recorder())
    with pytest.raises(AddressRefused):
        backend.connect_unix_socket("/tmp/x.sock")


def test_transport_is_not_created_when_httpx_changed_its_pool(monkeypatch):
    def bare(self, **kwargs):
        pass

    monkeypatch.setattr(httpx.HTTPTransport, "__init__", bare)
    with pytest.raises(RuntimeError, match="закрепление"):
        fetch.PinnedTransport()


# --- robots.txt и переход с https на http ---------------------------------------------------------


def test_robots_redirect_from_https_to_http_is_refused_and_the_http_file_is_not_read(net, tmp_path):
    net.add(f"{BASE}/robots.txt", status=301, headers={"location": "http://example.com/robots.txt"})
    net.add(
        "http://example.com/robots.txt", "User-agent: *\nAllow: /\n"
    )  # если бы его прочли, страница была бы открыта
    net.add(f"{BASE}/page", "<p>x</p>")
    fetcher = net.fetcher(tmp_path / "cache" / "fetch.json")
    with pytest.raises(RobotsDenied) as caught:
        fetcher.get(f"{BASE}/page")
    assert (
        caught.value.unavailable is True
    )  # robots.txt недоступен: как при 5xx, по RFC 9309 загрузка запрещена
    assert "http://example.com/robots.txt" not in net.urls()
    assert f"{BASE}/page" not in net.urls()


def test_robots_redirect_to_another_https_address_is_still_followed(net, tmp_path):
    net.add(f"{BASE}/robots.txt", status=301, headers={"location": "https://www.example.com/robots.txt"})
    net.add("https://www.example.com/robots.txt", "User-agent: *\nDisallow: /private\n")
    net.add(f"{BASE}/page", "<p>Страница доступна и содержит достаточно текста для проверки.</p>")
    fetcher = net.fetcher(tmp_path / "cache" / "fetch.json")
    assert fetcher.get(f"{BASE}/page").status == 200
    assert "https://www.example.com/robots.txt" in net.urls()


def test_robots_over_http_for_an_http_page_is_unchanged(net, tmp_path):
    net.add("http://example.com/robots.txt", "User-agent: *\nDisallow: /private\n")
    net.add(
        "http://example.com/page", "<p>Страница по http: адрес разрешён, robots.txt читается по http.</p>"
    )
    fetcher = net.fetcher(tmp_path / "cache" / "fetch.json")
    assert fetcher.get("http://example.com/page").status == 200
    assert "http://example.com/robots.txt" in net.urls()


def test_robots_http_redirect_to_https_is_followed(net, tmp_path):
    net.add(
        "http://example.com/robots.txt", status=301, headers={"location": "https://example.com/robots.txt"}
    )
    net.add("https://example.com/robots.txt", "User-agent: *\nDisallow: /page\n")
    fetcher = net.fetcher(tmp_path / "cache" / "fetch.json")
    with pytest.raises(RobotsDenied) as caught:
        fetcher.get("http://example.com/page")
    assert caught.value.unavailable is False  # robots.txt прочитан и запрещает страницу


def test_robots_unavailable_behaviour_is_the_same_as_before(net, tmp_path):
    """5xx и недоступность — запрещено, 4xx — разрешено (RFC 9309); недоступность помнится недолго."""
    net.add(f"{BASE}/robots.txt", status=503)
    fetcher = net.fetcher(tmp_path / "cache" / "fetch.json")
    assert fetcher.robots_state(f"{BASE}/page") == "недоступен"
    net.responses.pop(f"{BASE}/robots.txt")
    net.add(f"{BASE}/robots.txt", status=404)
    net.t += fetch.ROBOTS_UNAVAILABLE_TTL_S + 1
    assert fetcher.robots_state(f"{BASE}/page") == "разрешено"

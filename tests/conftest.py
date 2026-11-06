"""Общие заготовки тестов CLI: проекты во временной папке, сеть-заглушка, управляемые часы."""

from __future__ import annotations

import datetime as dt
import io
import sys
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from helpers import URL
from strateg import commands, fetch
from strateg import project as proj
from strateg.cli import main

PUBLIC_IP = "93.184.216.34"
PAGES = Path(__file__).parent / "fixtures" / "pages"


class Clock:
    """Часы проекта: время стоит, пока тест не сдвинет его."""

    def __init__(self) -> None:
        self.value = dt.datetime(2026, 11, 6, 10, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=3)))

    def advance(self, seconds: float) -> None:
        self.value += dt.timedelta(seconds=seconds)

    def __call__(self) -> dt.datetime:
        return self.value


class Net:
    """Сеть-заглушка: ответы по адресам, журнал запросов, разрешение имён и часы загрузчика."""

    def __init__(self) -> None:
        self.responses: dict[str, tuple[int, dict[str, str], bytes]] = {}
        self.requests: list[httpx.Request] = []
        self.hosts: dict[str, list[str]] = {}
        self.t = 1_000_000.0
        self.sleeps: list[float] = []

    def add(
        self,
        url: str,
        body: bytes | str = b"",
        content_type: str = "text/html; charset=utf-8",
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.responses[url] = (status, {"content-type": content_type, **(headers or {})}, data)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        hit = self.responses.get(str(request.url))
        if hit is None:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(404, text="not found")
        status, headers, body = hit
        return httpx.Response(status, headers=headers, content=body)

    def resolve(self, host: str) -> list[str]:
        return self.hosts.get(host, [PUBLIC_IP])

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds

    def clock(self) -> float:
        return self.t

    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def fetcher(self, cache: Path) -> fetch.Fetcher:
        return fetch.Fetcher(cache, client=self.client(), sleep=self.sleep, clock=self.clock)


@pytest.fixture
def clock(monkeypatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(proj, "now", c)
    return c


@pytest.fixture
def net(monkeypatch) -> Net:
    n = Net()
    monkeypatch.setattr(fetch, "resolve_host", n.resolve)
    monkeypatch.setattr(commands, "make_fetcher", n.fetcher)
    return n


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """Папка проектов во временной папке; активного проекта и переменных среды нет."""
    folder = tmp_path / "projects"
    monkeypatch.setenv(proj.PROJECTS_ENV, str(folder))
    monkeypatch.delenv(proj.PROJECT_ENV, raising=False)
    return folder


@pytest.fixture
def cli(capsys, home, clock) -> Callable[..., tuple[int, str, str]]:
    def run(*argv: str, stdin: str | None = None) -> tuple[int, str, str]:
        old = sys.stdin
        if stdin is not None:
            sys.stdin = io.TextIOWrapper(io.BytesIO(stdin.encode("utf-8")), encoding="utf-8")
        try:
            code = main(list(argv))
        finally:
            sys.stdin = old
        out = capsys.readouterr()
        return code, out.out, out.err

    return run


@pytest.fixture
def page_html() -> str:
    return (PAGES / "tariff.html").read_text(encoding="utf-8")


@pytest.fixture
def started(cli, net, page_html, home):
    """Проект demo с одним снимком И-1 (регион RU-MOW)."""
    net.add(URL, page_html)
    assert cli("new", "demo")[0] == 0
    code, out, err = cli("fetch", URL, "--region", "RU-MOW")
    assert code == 0, err
    return home / "demo"

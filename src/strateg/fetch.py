"""Загрузка страницы для `strateg fetch` (ADR-0004): безопасный запрос, `robots.txt`, кодировка, текст.

- только http и https; перед каждым запросом и после каждого перенаправления адрес хоста проверяется,
  локальные, внутренние, link-local, multicast и зарезервированные запрещены; не больше 5 перенаправлений;
- соединение идёт на тот адрес, который прошёл проверку: сетевой слой (`PinnedBackend`) сам разрешает имя при
  соединении, проверяет все адреса и соединяется по IP, имя остаётся в `Host` и в TLS; подмена DNS между
  проверкой и соединением внутреннего адреса не откроет;
- таймаут 20 секунд и один повтор; ответ больше 5 МиБ обрывается;
- `robots.txt` по RFC 9309 через protego: читается не больше 500 КиБ, кэш не дольше 24 часов;
  4xx — можно всё, 5xx, недоступность и перенаправление без адреса — запрещено; перенаправление `robots.txt`
  с https на http — отказ читать его по http: файл считается недоступным;
- не чаще одного запроса в секунду к хосту, время последнего запроса — в кэше проекта;
- честный `User-Agent`: `strateg/<версия> (+<адрес репозитория>)`;
- кодировка по байтам: заголовок, `<meta>`, charset-normalizer;
- HTML — очистка от скрытого, затем trafilatura, PDF — pypdfium2 (текст по страницам с метками `[стр. N]`).

Капчи и защиту от ботов модуль не обходит: такой ответ — отказ с понятной причиной.
"""

from __future__ import annotations

import codecs
import contextlib
import ipaddress
import json
import re
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from urllib.parse import urljoin

import charset_normalizer
import httpx
from protego import Protego

from strateg import __version__, storage

PRODUCT_TOKEN = "strateg"
MAX_BYTES = 5 * 1024 * 1024
ROBOTS_MAX_BYTES = 500 * 1024
ROBOTS_TTL_S = 24 * 3600
ROBOTS_UNAVAILABLE_TTL_S = 10 * 60  # недоступность robots.txt помним недолго: сайт мог поправиться
TIMEOUT_S = 20.0
MAX_REDIRECTS = 5
MIN_INTERVAL_S = 1.0
MIN_TEXT_CHARS = 20  # короче — страница без текста (строится скриптом)
_REDIRECTS = {301, 302, 303, 307, 308}
_WEAK_CHARSETS = {"iso-8859-1", "latin-1", "latin1", "ascii", "us-ascii", "windows-1252", "cp1252"}
_BLOCK_MARKERS = (
    "captcha",
    "капча",
    "recaptcha",
    "hcaptcha",
    "cf-chl",
    "just a moment",
    "checking your browser",
    "ddos-guard",
    "are you a robot",
    "не робот",
    "подозрительная активность",
    "access denied",
)


class FetchProblem(Exception):
    """Загрузка не удалась. `access` — состояние доступа источника из модели, `note` — пояснение."""

    def __init__(self, access: str, note: str) -> None:
        super().__init__(note)
        self.access = access
        self.note = note


class AddressRefused(Exception):
    """Адрес запрещён правилами безопасности (не http/https, внутренняя сеть, логин в адресе)."""


class RobotsDenied(Exception):
    """Загрузка запрещена: `Disallow` в robots.txt или (`unavailable`) сам robots.txt недоступен."""

    def __init__(self, url: str, unavailable: bool = False) -> None:
        super().__init__(url)
        self.url = url
        self.unavailable = unavailable


def user_agent() -> str:
    """`strateg/<версия> (+<адрес репозитория>)`; адрес — из `[project.urls]` в `pyproject.toml`."""
    repo = _repository_url()
    return f"{PRODUCT_TOKEN}/{__version__}" + (f" (+{repo})" if repo else "")


def _repository_url() -> str | None:
    try:
        urls = metadata.metadata("strateg").get_all("Project-URL") or []
    except metadata.PackageNotFoundError:
        urls = []
    for item in urls:
        name, _, value = item.partition(",")
        if name.strip().lower() in ("repository", "source", "homepage") and value.strip():
            return value.strip()
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if pyproject.is_file():
        import tomllib

        with contextlib.suppress(OSError, tomllib.TOMLDecodeError):
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            for value in data.get("project", {}).get("urls", {}).values():
                return str(value)
    return None


# ---------------------------------------------------------------------------
# Проверка адреса
# ---------------------------------------------------------------------------


def resolve_host(host: str) -> list[str]:
    """Адреса хоста. Отдельная функция: тесты подменяют разрешение имён."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise FetchProblem("источник недоступен: ошибка", f"имя {host} не найдено") from exc
    return sorted({str(info[4][0]).split("%")[0] for info in infos})


def forbidden_ip(text: str) -> str | None:
    """Причина запрета адреса или None, если адрес публичный."""
    ip = ipaddress.ip_address(text)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    for flag, reason in (
        (ip.is_loopback, "локальный"),
        (ip.is_private, "внутренний"),
        (ip.is_link_local, "link-local"),
        (ip.is_multicast, "multicast"),
        (ip.is_unspecified, "неопределённый"),
        (ip.is_reserved, "зарезервированный"),
        (not ip.is_global, "не публичный"),
    ):
        if flag:
            return reason
    return None


def normalize_url(raw: str) -> str:
    """Адрес в одном виде, без фрагмента. Только http и https."""
    raw = raw.strip()
    if not re.match(r"^https?://", raw, re.IGNORECASE):
        raise AddressRefused("адрес должен начинаться с http:// или https://")
    try:
        url = httpx.URL(raw)
    except (httpx.InvalidURL, ValueError) as exc:
        raise AddressRefused("адрес не разобрать: проверьте, нет ли в нём пробелов и лишних знаков") from exc
    if url.scheme not in ("http", "https") or not url.host:
        raise AddressRefused("адрес должен начинаться с http:// или https:// и содержать имя сайта")
    if url.userinfo:
        raise AddressRefused("адрес с логином и паролем не принимается")
    return str(url.copy_with(fragment=None))


def check_address(url: str) -> None:
    """Хост не ведёт во внутреннюю сеть: проверяются все его адреса. Нарушение — AddressRefused."""
    pinned_addresses(httpx.URL(url).host)


def pinned_addresses(host: str) -> list[str]:
    """Адреса хоста, проверенные в момент соединения; внутренний среди них — отказ (AddressRefused).

    Имя разрешается здесь заново и ровно один раз на соединение: то, что проверено, и то, к чему идёт
    соединение, — один и тот же список. Подмена ответа DNS между проверкой адреса в `check_address` и
    соединением ничего не меняет: `check_address` даёт понятное сообщение заранее, а окно закрывает
    эта проверка.
    """
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        if host.lower() == "localhost" or host.lower().endswith((".localhost", ".local", ".internal")):
            raise AddressRefused(f"адрес {host} локальный: загрузка внутренних адресов запрещена") from None
        addresses = resolve_host(host)
    if not addresses:
        raise AddressRefused(f"имя {host} не дало ни одного адреса")
    for addr in addresses:
        reason = forbidden_ip(addr)
        if reason:
            raise AddressRefused(f"адрес {host} ведёт на {reason} адрес ({addr}): загрузка запрещена")
    return addresses


class PinnedBackend:
    """Сетевой слой httpcore: соединение идёт на адрес, который прошёл проверку, а не на имя хоста.

    Имя хоста остаётся в запросе (заголовок `Host`) и в TLS: `httpcore` начинает рукопожатие с именем
    из адреса страницы, поэтому проверка сертификата и SNI те же, что без закрепления.
    """

    def __init__(self, inner) -> None:
        self.inner = inner

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        last: Exception | None = None
        for addr in pinned_addresses(host):
            try:
                return self.inner.connect_tcp(
                    addr, port, timeout=timeout, local_address=local_address, socket_options=socket_options
                )
            except Exception as exc:  # noqa: BLE001 - следующий проверенный адрес; последняя ошибка уйдёт наверх
                last = exc
        assert last is not None
        raise last

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise AddressRefused("соединение через сокет файловой системы запрещено")

    def sleep(self, seconds):
        return self.inner.sleep(seconds)


class PinnedTransport(httpx.HTTPTransport):
    """Транспорт httpx с закреплением проверенного адреса (`PinnedBackend`).

    Сетевой слой подменяется во внутреннем пуле соединений httpx: публичного способа задать его у httpx нет.
    Если устройство пула изменилось, транспорт не создаётся: без закрепления соединяться нельзя.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        pool = getattr(self, "_pool", None)
        backend = getattr(pool, "_network_backend", None)
        if backend is None:
            raise RuntimeError("httpx изменил устройство пула соединений: закрепление адреса не подключено")
        pool._network_backend = PinnedBackend(backend)


# ---------------------------------------------------------------------------
# Загрузчик
# ---------------------------------------------------------------------------


@dataclass
class Reply:
    status_code: int
    headers: httpx.Headers
    content: bytes


@dataclass
class Page:
    url: str  # итоговый адрес
    status: int
    content_type: str
    body: bytes  # после снятия сжатия


class Fetcher:
    """Запросы с проверкой адреса, `robots.txt`, паузой между запросами к хосту и кэшем в проекте."""

    def __init__(
        self,
        cache_file: Path,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cache_file = cache_file
        self.client = client or httpx.Client(
            transport=PinnedTransport(),
            timeout=httpx.Timeout(TIMEOUT_S),
            follow_redirects=False,
            trust_env=False,
        )
        self.sleep = sleep
        self.clock = clock
        self.ua = user_agent()

    # --- кэш ---
    def _load(self) -> dict:
        try:
            data = json.loads(self.cache_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(data, ensure_ascii=False, indent=1) + "\n"
        # атомарно: соседний процесс не увидит обрывка; кэш необязателен, занятый файл загрузку не ломает
        with contextlib.suppress(storage.FileBusy):
            storage.atomic_write_bytes(self.cache_file, text.encode("utf-8"), 2.0)

    def _throttle(self, host: str) -> None:
        data = self._load()
        last = data.setdefault("last_request", {}).get(host)
        wait = (last + MIN_INTERVAL_S - self.clock()) if last is not None else 0.0
        if wait > 0:
            self.sleep(wait)
        data["last_request"][host] = self.clock()
        self._save(data)

    # --- один запрос ---
    def _request(self, url: str, limit: int, *, truncate: bool = False) -> Reply:
        """GET с паузой, таймаутом и одним повтором. Ответ больше `limit`: обрыв или усечение."""
        headers = {
            "User-Agent": self.ua,
            "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,text/plain;q=0.8,*/*;q=0.5",
            "Accept-Language": "ru,en;q=0.5",
        }
        host = httpx.URL(url).host
        last_error = "ошибка сети"
        for _attempt in range(2):
            self._throttle(host)
            try:
                with self.client.stream("GET", url, headers=headers) as resp:
                    declared = resp.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > limit and not truncate:
                        raise FetchProblem(
                            "источник недоступен: ошибка", f"ответ больше {limit // (1024 * 1024)} МиБ"
                        )
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in resp.iter_bytes():
                        size += len(chunk)
                        if size > limit:
                            if not truncate:
                                raise FetchProblem(
                                    "источник недоступен: ошибка",
                                    f"ответ больше {limit // (1024 * 1024)} МиБ, загрузка оборвана",
                                )
                            chunks.append(chunk[: len(chunk) - (size - limit)])
                            break
                        chunks.append(chunk)
                    if resp.status_code >= 500 and _attempt == 0:
                        last_error = f"ответ {resp.status_code}"
                        continue
                    return Reply(resp.status_code, resp.headers, b"".join(chunks))
            except httpx.TimeoutException:
                last_error = f"сайт не ответил за {int(TIMEOUT_S)} секунд"
            except httpx.TransportError as exc:
                last_error = f"сайт не открылся ({type(exc).__name__})"
        raise FetchProblem("источник недоступен: ошибка", last_error)

    # --- robots.txt ---
    def _robots(self, url: str) -> Protego | None:
        """Правила `robots.txt` сайта; None — всё запрещено (5xx или недоступность)."""
        parts = httpx.URL(url)
        origin = f"{parts.scheme}://{parts.netloc.decode('ascii')}"
        data = self._load()
        entry = data.get("robots", {}).get(origin)
        if not entry or self.clock() - entry["at"] > entry.get("ttl", ROBOTS_TTL_S):
            entry = self._download_robots(origin)
            data = self._load()
            data.setdefault("robots", {})[origin] = entry
            self._save(data)
        if entry["mode"] == "deny":
            return None
        if entry["mode"] == "allow":
            return Protego.parse("")
        return Protego.parse(entry["text"])

    def _download_robots(self, origin: str) -> dict:
        url = origin + "/robots.txt"
        entry = {"at": self.clock(), "mode": "deny", "text": "", "ttl": ROBOTS_UNAVAILABLE_TTL_S}
        try:
            for _hop in range(MAX_REDIRECTS + 1):
                check_address(url)
                resp = self._request(url, ROBOTS_MAX_BYTES, truncate=True)
                if resp.status_code in _REDIRECTS and resp.headers.get("location"):
                    target = normalize_url(urljoin(url, resp.headers["location"]))
                    if url.startswith("https:") and target.startswith("http:"):
                        break  # robots.txt по https не читается по http: правил нет, он считается недоступным
                    url = target
                    continue
                if 300 <= resp.status_code < 400:
                    break  # перенаправление без адреса: правил нет, robots.txt считается недоступным
                if resp.status_code < 300:
                    entry.update(mode="rules", ttl=ROBOTS_TTL_S)
                    entry["text"] = resp.content.decode("utf-8-sig", errors="replace")
                elif resp.status_code < 500:
                    entry.update(mode="allow", ttl=ROBOTS_TTL_S)
                break
        except (FetchProblem, AddressRefused):
            entry["mode"] = "deny"
        return entry

    def robots_state(self, url: str) -> str:
        """«разрешено», «запрещено» (Disallow) или «недоступен» (robots.txt не получен: 5xx, нет связи)."""
        rules = self._robots(url)
        if rules is None:
            return "недоступен"
        return "разрешено" if rules.can_fetch(url, PRODUCT_TOKEN) else "запрещено"

    def robots_allowed(self, url: str) -> bool:
        return self.robots_state(url) == "разрешено"

    # --- страница ---
    def get(self, raw_url: str) -> Page:
        """Страница по адресу: проверка адреса и `robots.txt` на каждом переходе, не больше 5 переходов."""
        url = normalize_url(raw_url)
        for _hop in range(MAX_REDIRECTS + 1):
            check_address(url)
            state = self.robots_state(url)
            if state != "разрешено":
                raise RobotsDenied(url, unavailable=state == "недоступен")
            resp = self._request(url, MAX_BYTES)
            location = resp.headers.get("location")
            if resp.status_code in _REDIRECTS and location:
                target = normalize_url(urljoin(url, location))
                if url.startswith("https:") and target.startswith("http:"):
                    raise FetchProblem(
                        "источник недоступен: ошибка", "перенаправление с https на http запрещено"
                    )
                url = target
                continue
            return Page(url, resp.status_code, resp.headers.get("content-type", ""), resp.content)
        raise FetchProblem("источник недоступен: ошибка", f"больше {MAX_REDIRECTS} перенаправлений")


# ---------------------------------------------------------------------------
# Кодировка и текст
# ---------------------------------------------------------------------------


def _canon(name: str) -> str | None:
    try:
        return codecs.lookup(name.strip().strip("\"'")).name
    except LookupError:
        return None


def _decodes(body: bytes, enc: str) -> str | None:
    try:
        return body.decode(enc)
    except (UnicodeDecodeError, LookupError):
        return None


def decode_body(body: bytes, content_type: str = "") -> tuple[str, str]:
    """Текст и название кодировки: BOM, заголовок, `<meta>`, utf-8, charset-normalizer."""
    for bom, enc in (
        (codecs.BOM_UTF8, "utf-8"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
    ):
        if body.startswith(bom):
            text = _decodes(body, "utf-8-sig" if enc == "utf-8" else enc)
            if text is not None:
                return text, enc
    declared: list[str] = []
    m = re.search(r"charset\s*=\s*([\w\-:.]+)", content_type, re.IGNORECASE)
    header = _canon(m.group(1)) if m else None
    meta = re.search(rb"<meta[^>]+charset\s*=\s*[\"']?\s*([\w\-:.]+)", body[:4096], re.IGNORECASE)
    meta_enc = _canon(meta.group(1).decode("ascii", "ignore")) if meta else None
    weak = {_canon(w) for w in _WEAK_CHARSETS}
    # Слабая кодировка в заголовке (latin-1 по умолчанию) не главнее <meta> и самого текста.
    if header and header not in weak:
        declared.append(header)
    if meta_enc:
        declared.append(meta_enc)
    for enc in declared:
        text = _decodes(body, enc)
        if text is not None:
            return text, enc
    text = _decodes(body, "utf-8")
    if text is not None:
        return text, "utf-8"
    best = charset_normalizer.from_bytes(body).best()
    if best is not None and best.encoding:
        enc = _canon(best.encoding) or best.encoding
        return str(best), enc
    if header:
        text = _decodes(body, header)
        if text is not None:
            return text, header
    return body.decode("utf-8", errors="replace"), "utf-8"


def looks_blocked(status: int, visible: str, extracted: str) -> bool:
    """Ответ-вызов: капча или защита от ботов. Обход не предусмотрен, ответ становится пробелом.

    Признаки ищутся только в видимом тексте и заголовке страницы, а не в скриптах и разметке.
    """
    if status in (401, 403, 429):
        return True
    low = visible[:50000].lower()
    return len(extracted) < 3000 and any(marker in low for marker in _BLOCK_MARKERS)


_BLOCK_TAGS = {
    "p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "table", "section", "article",
    "br", "dt", "dd", "blockquote", "pre", "main", "form", "fieldset", "label", "button",
    "details", "summary",
}  # fmt: skip
_INLINE_BREAK_TAGS = {"a", "span", "td", "th", "option", "b", "strong", "em", "i", "small", "sup", "sub"}
_DROP_XPATH = "//script|//style|//svg|//iframe|//head"
_PAGE_FRAME_XPATH = "|".join(
    f"//{t}[not(ancestor::main or ancestor::article)]" for t in ("nav", "header", "footer", "aside")
)
_HIDDEN_XPATH = "//template|//noscript|//*[@hidden]|//*[@aria-hidden]|//input[@type]|//*[@style]"
_XML_PROLOG = re.compile(r"^[\ufeff\s]*<\?xml[^>]*\?>")
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_ZERO = re.compile(r"0*\.?0*(?:[a-z%]+)?")  # «0», «0px», «0.0em», «0%»


def _hidden_by_style(style: str) -> bool:
    """Встроенный стиль прячет элемент: display:none, visibility:hidden, font-size:0, opacity:0."""
    for decl in _CSS_COMMENT.sub("", style).split(";"):
        name, _, value = decl.partition(":")
        value = value.lower().replace("!important", "").strip()
        match name.strip().lower():
            case "display":
                hidden = value == "none"
            case "visibility":
                hidden = value in ("hidden", "collapse")
            case "font-size" | "opacity":
                hidden = bool(_ZERO.fullmatch(value)) and any(c.isdigit() for c in value)
            case _:
                hidden = False
        if hidden:
            return True
    return False


def _is_hidden(el) -> bool:
    attrs = el.attrib
    return (
        el.tag in ("template", "noscript")
        or "hidden" in attrs
        or attrs.get("aria-hidden", "").strip().lower() == "true"
        or (el.tag == "input" and attrs.get("type", "").strip().lower() == "hidden")
        or _hidden_by_style(attrs.get("style", ""))
    )


def _drop(doc, xpath: str, *, only_hidden: bool = False) -> None:
    for node in doc.xpath(xpath):
        if node.getparent() is not None and (not only_hidden or _is_hidden(node)):
            node.drop_tree()


def _parse(html: str):
    from lxml import html as lxml_html

    try:
        return lxml_html.fromstring(_XML_PROLOG.sub("", html, count=1))
    except (ValueError, lxml_html.etree.ParserError):
        return None


def clean_html(html: str) -> str:
    """HTML без скрытого: `hidden`, `aria-hidden`, `<template>`, `<noscript>`, `<input type=hidden>` и
    элементов с встроенным стилем `display:none`, `visibility:hidden`, `font-size:0`, `opacity:0`.

    Одна очистка перед обоими путями извлечения. Скрытое классами и внешними стилями не видно без
    отрисовки страницы. Не разобрать — пустая строка.
    """
    from lxml import html as lxml_html

    doc = _parse(html)
    if doc is None:
        return ""
    _drop(doc, _HIDDEN_XPATH, only_hidden=True)
    return lxml_html.tostring(doc, encoding="unicode")


_ATTR_TEXT = ("alt", "title", "aria-label", "aria-description", "placeholder")


def hidden_text(html: str) -> str:
    """Текст, которого нет в снимке: скрытые элементы, комментарии HTML, `alt`, `title`, `aria-label`.

    В снимок он не идёт, по нему только ищут обращения к ИИ.
    """
    doc = _parse(html)
    if doc is None:
        return ""
    parts = [n.text_content() for n in doc.xpath(_HIDDEN_XPATH) if _is_hidden(n)]
    parts += [c.text for c in doc.xpath("//comment()") if c.text]
    for el in doc.iter():
        if not isinstance(el.tag, str):
            continue
        parts += [el.attrib[name] for name in _ATTR_TEXT if el.attrib.get(name)]
        if (
            el.tag == "input"
            and el.attrib.get("type", "").strip().lower() == "hidden"
            and el.attrib.get("value")
        ):
            parts.append(el.attrib["value"])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Авторы отзывов и комментариев (ADR-0011): метка вместо имени и ника до извлечения текста
# ---------------------------------------------------------------------------

AUTHOR_LABEL = "[автор отзыва]"
MENTION_LABEL = "[ник]"
MAX_AUTHOR_CHARS = 200  # длиннее — это не имя, а целый блок: его не заменяем

# Признаки автора — разметка, а не угадывание по тексту. Список в одном месте; на каждую строку есть тест.
# Блок отзыва или комментария: тип schema.org (последняя часть адреса `itemtype`) или слово в `class` и `id`.
REVIEW_ITEMTYPES = frozenset({"review", "comment", "usercomments", "userreview", "questionanswer"})
REVIEW_BLOCK_WORDS = ("comment", "review", "feedback", "testimonial", "otzyv", "otzov", "kommentar")
REVIEW_BLOCK_WORDS_RU = ("отзыв", "коммент")
# Классы, которые называют автора отзыва или комментария: значимы где угодно. Сравниваются без «-» и «_»,
# без учёта регистра: `comment-author`, `comment__author`, `commentAuthor` — один признак.
AUTHOR_CLASSES = frozenset(
    {
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
)
# Общие классы автора: значимы только внутри блока отзыва или комментария (вне его это автор статьи).
AUTHOR_CLASSES_IN_BLOCK = frozenset(
    {"author", "authorname", "user", "username", "nickname", "nick", "displayname", "profilename", "fn"}
)
# Атрибуты, в которых разметка называет автора: само имя заменяется в тексте блока.
AUTHOR_ATTRS = (
    "data-author",
    "data-author-name",
    "data-username",
    "data-user-name",
    "data-nickname",
    "data-reviewer",
    "data-commenter",
    "data-user",
)
_MENTION = re.compile(r"(?<![\w.@])@\w[\w.]{1,31}")


def _class_keys(el) -> list[str]:
    return [t.replace("-", "").replace("_", "").lower() for t in el.attrib.get("class", "").split()]


def _is_review_itemtype(el) -> bool:
    kinds = {t.rstrip("/").rsplit("/", 1)[-1].lower() for t in el.attrib.get("itemtype", "").split()}
    return bool(kinds & REVIEW_ITEMTYPES)


def _is_review_block(el) -> bool:
    if _is_review_itemtype(el):
        return True
    names = (
        (el.attrib.get("class", "") + " " + el.attrib.get("id", "")).lower().replace("-", "").replace("_", "")
    )
    return any(w in names for w in REVIEW_BLOCK_WORDS + REVIEW_BLOCK_WORDS_RU)


def _in_review_block(el) -> bool:
    return any(_is_review_block(a) for a in el.iterancestors() if isinstance(a.tag, str))


def _microdata_author_in_review(el) -> bool:
    """`itemprop="author"` внутри Review и Comment; у Article и NewsArticle это автор статьи, он остаётся."""
    for anc in el.iterancestors():
        if isinstance(anc.tag, str) and "itemscope" in anc.attrib:
            return _is_review_itemtype(anc)
    return _in_review_block(el)  # разметка без itemscope: решает блок вокруг


def _is_author_node(el) -> bool:
    if not isinstance(el.tag, str):
        return False
    props = el.attrib.get("itemprop", "").lower().split()
    if "author" in props or "reviewer" in props or "creator" in props:
        return _microdata_author_in_review(el)
    keys = set(_class_keys(el))
    if keys & AUTHOR_CLASSES:
        return True
    return bool(keys & AUTHOR_CLASSES_IN_BLOCK) and _in_review_block(el)


def _replace_name(el, name: str, label: str) -> int:
    """Заменяет имя из атрибута разметки во всех текстах внутри блока."""
    n = 0
    pattern = re.compile(re.escape(name), re.IGNORECASE)
    for node in el.iter():
        if not isinstance(node.tag, str):
            continue
        if node.text and pattern.search(node.text):
            node.text, k = pattern.subn(label, node.text)
            n += k
        if node is not el and node.tail and pattern.search(node.tail):
            node.tail, k = pattern.subn(label, node.tail)
            n += k
    return n


def mask_authors(html: str) -> tuple[str, str]:
    """HTML с метками вместо авторов отзывов и комментариев и текст блоков отзывов (для предупреждения).

    Замена идёт в дереве до извлечения текста, иначе разметка, по которой автор узнаётся, уже потеряна.
    Меняются: узел `itemprop="author"` внутри Review и Comment, узлы с классами автора (`AUTHOR_CLASSES`,
    а внутри блока отзыва и `AUTHOR_CLASSES_IN_BLOCK`), имя из атрибутов `data-author` и подобных,
    упоминания `@ник` внутри блока отзыва. Автор статьи, имена в самом тексте и названия организаций
    остаются. Имя в свободном тексте отзыва разметкой не находится.
    """
    doc = _parse(html)
    if doc is None:
        return html, ""
    done: set = set()
    for el in list(doc.iter()):
        if not isinstance(el.tag, str) or any(a in done for a in el.iterancestors()):
            continue
        if _is_author_node(el):
            if len(el.text_content().strip()) > MAX_AUTHOR_CHARS:
                continue
            for child in list(el):
                el.remove(child)
            el.text = AUTHOR_LABEL
            done.add(el)
            continue
        if _is_review_block(el) or _in_review_block(el):  # `data-author` у статьи — автор статьи, не отзыва
            for attr in AUTHOR_ATTRS:
                name = el.attrib.get(attr, "").strip()
                if len(name) >= 2 and not name.isdigit():
                    _replace_name(el, name, AUTHOR_LABEL)
                    el.attrib[attr] = AUTHOR_LABEL  # имя не остаётся и в разметке, которая уйдёт дальше
    blocks = {el for el in doc.iter() if isinstance(el.tag, str) and _is_review_block(el)}
    top = [el for el in blocks if not any(a in blocks for a in el.iterancestors())]
    review_text: list[str] = []
    for block in top:
        for node in block.iter():
            if not isinstance(node.tag, str):
                continue
            if node.text:
                node.text = _MENTION.sub(MENTION_LABEL, node.text)
            if node is not block and node.tail:
                node.tail = _MENTION.sub(MENTION_LABEL, node.tail)
        texts = block.xpath(".//text()[not(ancestor::script or ancestor::style)]")
        review_text.append(" ".join(" ".join(texts).split()))
    from lxml import html as lxml_html

    return lxml_html.tostring(doc, encoding="unicode"), "\n".join(review_text)


def page_title(html: str) -> str:
    doc = _parse(html)
    if doc is None:
        return ""
    return " ".join(" ".join(doc.xpath("//title//text()")).split())


def visible_text(html: str) -> str:
    """Весь видимый текст страницы: без скрытого, скриптов, стилей и рамки страницы; строки по блокам.

    Навигация, шапка, подвал и боковая колонка отбрасываются только вне `main` и `article`.
    """
    doc = _parse(html)
    if doc is None:
        return ""
    _drop(doc, _HIDDEN_XPATH, only_hidden=True)
    _drop(doc, _DROP_XPATH)
    _drop(doc, _PAGE_FRAME_XPATH)
    for el in doc.iter():
        if not isinstance(el.tag, str):
            continue
        if el.tag in _BLOCK_TAGS:
            el.text = "\n" + (el.text or "")
            el.tail = "\n" + (el.tail or "")
        elif el.tag in _INLINE_BREAK_TAGS:
            el.tail = " " + (el.tail or "")  # соседние элементы не склеиваются в одно слово
    lines: list[str] = []
    for raw in doc.text_content().split("\n"):
        line = " ".join(raw.split())
        if line and (not lines or lines[-1] != line):
            lines.append(line)
    return "\n".join(lines)


_COUNTED_NUMBER = re.compile(r"\d+(?: \d{3}(?!\d))*(?:[.,]\d+)?")


def _significant_numbers(text: str) -> set[str]:
    out = set()
    for m in _COUNTED_NUMBER.finditer(text.replace("\u00a0", " ")):
        value = m.group().replace(" ", "")
        if len(value) >= 2:
            out.add(value)
    return out


_PRICE = re.compile(r"(\d+(?: \d{3}(?!\d))*(?:[.,]\d+)?)\s*(?:₽|руб|р\.|\$|€|usd|eur)", re.IGNORECASE)


def _prices(text: str) -> set[str]:
    """Цены видимого текста: число перед знаком валюты или словом «руб»."""
    return {m.group(1).replace(" ", "") for m in _PRICE.finditer(text.replace(" ", " "))}


@dataclass
class Extraction:
    text: str
    extractor: str  # «название версия», в метаданных снимка
    fallback: bool  # основной извлекатель потерял числа, взят видимый текст
    note: str = ""
    review_text: str = ""  # текст блоков отзывов и комментариев (с метками): для предупреждения об именах


def _lib_version(lib: str) -> str:
    return f"{lib} {metadata.version(lib)}"


def extract_html(html: str) -> str:
    """Основной путь: trafilatura с полнотой (`favor_recall`, таблицы)."""
    import trafilatura

    text = trafilatura.extract(
        html,
        output_format="txt",
        include_comments=False,
        include_tables=True,
        include_links=False,
        favor_recall=True,
        deduplicate=False,
    )
    return text or ""


def extract_page(html: str) -> Extraction:
    """Текст страницы: trafilatura, а если в нём заметно меньше чисел, чем в видимом тексте, то видимый текст.

    Карточки цен, опции и ответы раскрывающихся блоков trafilatura принимает за шаблон страницы и
    выбрасывает; цены при этом теряются. Запасной путь берёт весь видимый текст без скриптов, стилей и
    навигации. Способ пишется в метаданные снимка (`extractor`).
    """
    html, review_text = mask_authors(clean_html(html))
    main = extract_html(html)
    visible = visible_text(html)
    in_main, in_visible = _significant_numbers(main), _significant_numbers(visible)
    missing = in_visible - in_main
    lost_prices = bool(_prices(visible) - in_main)
    lost_numbers = lost_prices or (len(missing) >= 2 and len(in_main) < 0.7 * len(in_visible))
    empty_main = len(main.strip()) < MIN_TEXT_CHARS and len(visible) >= 200
    if lost_numbers or empty_main:
        why = (
            f"в основном тексте нет {len(missing)} из {len(in_visible)} чисел видимого текста, "
            f"в том числе цен: {len(_prices(visible) - in_main)}"
            if lost_numbers
            else "основной текст пуст"
        )
        return Extraction(visible, f"lxml-visible-text {metadata.version('lxml')}", True, why, review_text)
    return Extraction(main, _lib_version("trafilatura"), False, review_text=review_text)


def extractor_name(kind: str) -> str:
    return _lib_version("pypdfium2" if kind == "PDF" else "trafilatura")


def extract_pdf(body: bytes) -> str:
    """Текст PDF по страницам: перед каждой страницей строка `[стр. N]` — по ней ищется `locator`."""
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(body)
    except pdfium.PdfiumError as exc:
        raise FetchProblem("источник недоступен: ошибка", "PDF не открывается") from exc
    parts: list[str] = []
    try:
        for number in range(len(pdf)):
            page = pdf[number]
            textpage = page.get_textpage()
            try:
                text = textpage.get_text_bounded().replace("\r\n", "\n").replace("\r", "\n").strip()
            finally:
                textpage.close()
                page.close()
            if text:
                parts.append(f"[стр. {number + 1}]\n{text}")
    finally:
        pdf.close()
    return "\n\n".join(parts)


def is_pdf(page: Page) -> bool:
    return "pdf" in page.content_type.lower() or page.body.startswith(b"%PDF-")

"""Проба Playwright MCP по stdio без среды разработки: сервер из `.mcp.json`, локальная страница.

Запуск из корня репозитория:  python scripts/probe_playwright_mcp.py [--out ПАПКА] [--browser msedge|chrome]

Что делает: запускает сервер ровно командой из `.mcp.json`, поднимает на
127.0.0.1 страницы из `tests/fixtures/pages/`, ходит по JSON-RPC: `initialize`, `tools/list`,
`browser_navigate`, `browser_take_screenshot`, затем переход по ссылке, которая перенаправляет на другое
имя хоста. Внешние сайты не открываются. Печатает список инструментов, ответы `browser_navigate` и путь
файла снимка. Нужны Node (с npm) и браузер; пакет `npx` скачивает при первом запуске. В обычный `pytest`
не входит: тест в `tests/test_ux_targets.py` запускает её только по флагу `STRATEG_PROBE_PLAYWRIGHT=1`.
Только стандартная библиотека.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGES = ROOT / "tests" / "fixtures" / "pages"


def server_command() -> list[str]:
    """Ровно команда из `.mcp.json`: `command` и `args` без изменений."""
    entry = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["playwright"]
    exe = shutil.which(entry["command"])
    if exe is None:
        raise SystemExit(f"не найден {entry['command']}")
    return [exe, *entry.get("args", [])]


class Handler(http.server.SimpleHTTPRequestHandler):
    """Страницы из `tests/fixtures/pages/`; `/go` перенаправляет на то же место под именем `localhost`."""

    def do_GET(self) -> None:  # noqa: N802 - имя задано стандартной библиотекой
        if self.path.startswith("/go"):
            port = self.server.server_address[1]
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{port}/cards.html")
            self.end_headers()
            return
        super().do_GET()

    def log_message(self, *args: object) -> None:
        pass


class Rpc:
    def __init__(self, argv: list[str], env: dict[str, str]) -> None:
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd=ROOT,  # рабочая папка сервера в Claude Code — корень проекта (предположение)
        )
        self.seq = 0

    def send(self, payload: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def call(self, method: str, params: dict | None = None) -> dict:
        self.seq += 1
        self.send({"jsonrpc": "2.0", "id": self.seq, "method": method, "params": params or {}})
        assert self.proc.stdout is not None
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise SystemExit("сервер закрыл вывод до ответа")
            msg = json.loads(line.decode("utf-8"))
            if msg.get("id") == self.seq:
                return msg

    def close(self) -> None:
        for stream in (self.proc.stdin, self.proc.stdout):
            if stream:
                stream.close()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def texts(result: dict) -> list[str]:
    parts = result.get("result", {}).get("content", [])
    return [p.get("text", "") for p in parts if p.get("type") == "text"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", help="временная папка пробы; снимки лягут в <папка>/strateg-shots")
    ap.add_argument("--browser", help="msedge или chrome (по умолчанию из .mcp.json)")
    args = ap.parse_args()
    out = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="strateg-probe-"))
    out.mkdir(parents=True, exist_ok=True)
    # временная папка ОС — внутри `out`: снимки лягут в `out/strateg-shots`, как у скрипта запуска
    env = {**os.environ, "TEMP": str(out), "TMP": str(out), "TMPDIR": str(out)}
    if args.browser:
        env["STRATEG_BROWSER"] = args.browser
    argv = server_command()

    httpd = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Handler, directory=str(PAGES))
    )
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    rpc = Rpc(argv, env)
    report: dict[str, object] = {"output_dir": str(out)}
    try:
        init = rpc.call(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "strateg-probe", "version": "1"},
            },
        )
        report["server"] = init["result"]["serverInfo"]
        rpc.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools = rpc.call("tools/list")["result"]["tools"]
        report["tools"] = sorted(t["name"] for t in tools)
        nav = rpc.call(
            "tools/call",
            {"name": "browser_navigate", "arguments": {"url": f"http://127.0.0.1:{port}/cards.html"}},
        )
        report["navigate"] = texts(nav)
        shot = rpc.call("tools/call", {"name": "browser_take_screenshot", "arguments": {}})
        report["screenshot_text"] = texts(shot)
        report["screenshot_parts"] = [p.get("type") for p in shot.get("result", {}).get("content", [])]
        redirect = rpc.call(
            "tools/call", {"name": "browser_navigate", "arguments": {"url": f"http://127.0.0.1:{port}/go"}}
        )
        report["navigate_redirect"] = texts(redirect)
        shots = out / "strateg-shots"
        report["files"] = sorted(str(p.relative_to(shots)) for p in shots.rglob("*") if p.is_file())
    finally:
        rpc.close()
        httpd.shutdown()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    ok = bool(report.get("files")) and "browser_navigate" in report.get("tools", [])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

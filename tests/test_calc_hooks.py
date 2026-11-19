"""Расчёты и хук прав: агент пишет скрипт только в `outputs/calc/`, запускает его командой CLI."""

from __future__ import annotations

from test_hooks_rules import allowed, decide, refused
from test_hooks_rules_zones import with_active

SCRIPT = {"file_path": "projects/demo/outputs/calc/total.py", "content": "import strateg_calc"}


def test_main_agent_writes_the_script_and_runs_the_commands():
    svc = with_active("demo")
    allowed(decide("Write", SCRIPT, svc=svc))
    allowed(decide("Bash", {"command": "uv run strateg calc run projects/demo/outputs/calc/total.py"}))
    allowed(decide("Bash", {"command": "uv run strateg calc check"}))
    allowed(decide("Bash", {"command": "uv run strateg data check И-1"}))


def test_script_is_not_written_outside_the_zone_and_not_by_other_roles():
    svc = with_active("demo")
    for folder in ("calc", "sources", "inputs"):
        refused(decide("Write", {**SCRIPT, "file_path": f"projects/demo/{folder}/total.py"}, svc=svc))
    refused(decide("Write", {**SCRIPT, "file_path": "projects/other/outputs/calc/total.py"}, svc=svc))
    refused(decide("Write", SCRIPT, "critic"), "запись")
    refused(decide("Write", SCRIPT, "researcher"), "запись")

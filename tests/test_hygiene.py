"""Guards against the failure the brief says ends the process: ticket-specific or hand-typed answers in code."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = [p for p in (ROOT / "qra").rglob("*.py")]


def test_no_ticket_ids_in_the_agent_source():
    pattern = re.compile(r"\b(?:T\d{3}|E\d{2})\b")
    hits = [(p.name, m.group(0)) for p in SOURCE for m in pattern.finditer(p.read_text(encoding="utf-8"))]
    assert not hits, f"ticket or example ids found in agent code: {hits}"


def test_no_secrets_or_attribution_in_tracked_text():
    banned = re.compile(r"nrd_round_|AIza[0-9A-Za-z_-]{20,}|claude|anthropic|co-authored", re.I)
    for path in [*SOURCE, *(ROOT / "tests").glob("*.py"), *(ROOT / "docs").glob("*.md")]:
        if path.name == "test_hygiene.py":
            continue
        text = path.read_text(encoding="utf-8")
        assert not banned.search(text), f"{path.name} contains a secret or an attribution string"

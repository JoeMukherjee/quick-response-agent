"""No sampling parameters are set on any model call: the models run on their own defaults (Gemini 3 and later guidance)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "qra"


def test_no_temperature_top_p_or_top_k_is_ever_passed_to_a_model():
    pattern = re.compile(r"\b(temperature|top_p|top_k)\s*[=:]", re.I)
    for path in ROOT.rglob("*.py"):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]  # a comment may say it
            assert not pattern.search(code), f"{path.name}:{n} sets a sampling parameter"

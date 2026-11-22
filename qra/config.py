"""Settings come from environment variables only (a .env file is loaded for local runs)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    pass


# Every decision threshold lives here, fixed a priori from the client's rules and NOT tuned on the practice
# tickets. Hard rules escalate early (0.3) because a wrong reply costs far more than a wrong handoff.
THRESHOLDS = {
    "yes": 0.7,               # a statement counts as true at or above this
    "unsettled": 0.3,         # between this and "yes" the question is not settled
    "hard_rule": 0.3,         # refund, cancellation, personal data, security, legal, churn -> human
    "injection": 0.5,         # ticket text trying to instruct the assistant -> human
    "spam_close": 0.85,       # close only at or above this ...
    "asks_help_max": 0.3,     # ... and only if the customer is clearly not asking for help
    "language_min": 0.6,      # below this, or "other", the language is unsure -> human
    "faq_candidate": 0.5,     # FAQ entries at or above this are given to the drafter (at most max_entries)
    "faq_resolves": 0.7,      # at least one entry must score this to attempt a reply
    "coverage": 0.7,          # second-stage "the entries answer everything asked" must reach this
    "verify_sentence": 0.7,   # each reply sentence must be supported by the entries at or above this
    "verify_promise": 0.3,    # reply promising something outside the entries -> reject at or above this
    "max_entries": 3,
}


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable {name} (see .env.example)")
    return value


@dataclass(frozen=True)
class Settings:
    helpdesk_url: str
    helpdesk_key: str = field(repr=False)
    candidate_email: str = ""
    candidate_name: str = ""
    gemini_api_key: str = field(default="", repr=False)
    model_judge: str = "gemini-decision-1"
    model_draft: str = "gemini-3.8-flash"
    state_dir: Path = ROOT / "state"
    log_dir: Path = ROOT / "logs"
    helpdesk_rps: float = 4.0  # the helpdesk answers 429 above 5 requests per second
    gemini_tpm: int = 45_000  # stay under the 50k tokens-per-minute free tier

    @classmethod
    def from_env(cls, require_gemini: bool = True) -> "Settings":
        load_dotenv(ROOT / ".env")
        return cls(
            helpdesk_url=_required("HELPDESK_URL").rstrip("/"),
            helpdesk_key=_required("HELPDESK_KEY"),
            candidate_email=_required("CANDIDATE_EMAIL"),
            candidate_name=os.environ.get("CANDIDATE_NAME", ""),
            gemini_api_key=_required("GEMINI_API_KEY") if require_gemini else os.environ.get("GEMINI_API_KEY", ""),
            model_judge=os.environ.get("MODEL_JUDGE", "gemini-decision-1"),
            model_draft=os.environ.get("MODEL_DRAFT", "gemini-3.8-flash"),
        )

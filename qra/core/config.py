"""Settings come from environment variables only (a .env file is loaded for local runs)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent.parent


class ConfigError(RuntimeError):
    pass


# How much gemini-3.8-flash thinks before answering, per role. One place, so the roles cannot drift apart.
# The drafter writes from facts it is given, so "low" (it does not spend reasoning tokens); the second opinion reviews
# and decides, so "medium" (a measured ~230 reasoning tokens on a simple case, about twice the latency).
THINKING_LEVELS = {"draft": "low", "opinion": "medium"}


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
    "verify_unsupported": 0.7,  # a reply sentence judged to state something outside the entries/ticket is rejected from "yes" (0.7)
    "verify_promise": 0.3,    # reply promising something outside the entries -> reject at or above this
    "max_entries": 3,
    "faq_resolves_pick": 0.6,    # single-choice help-centre question: the top entry must reach this
    "faq_candidate_pick": 0.25,  # ... and entries from this up are given to the drafter
    "resample_margin": 0.1,      # a deciding probability this close to its threshold is asked about once more
    "opinion_floor": 0.5,        # second opinion (on by default): consulted when coverage is in [floor, ceiling) ...
    "opinion_ceiling": 0.8,      # ... and below the floor no opinion can turn a handoff into a reply
}


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable {name} (see .env.example)")
    return value


def _default_backend() -> str:
    """Jev on TypeSafe is the recommended decision model. JUDGE_BACKEND=gemini chooses Decision 1; with no choice made and no
    TypeSafe key, the service falls back to Decision 1 so it still runs for anyone who only has a Gemini key."""
    choice = os.environ.get("JUDGE_BACKEND", "").strip().lower()
    if choice in ("gemini", "typesafe"):
        return choice
    return "typesafe" if os.environ.get("TYPESAFE_API_KEY", "").strip() else "gemini"


@dataclass(frozen=True)
class Settings:
    helpdesk_url: str
    helpdesk_key: str = field(repr=False)
    candidate_email: str = ""
    candidate_name: str = ""
    gemini_api_key: str = field(default="", repr=False)
    model_judge: str = "gemini-decision-1"
    model_draft: str = "gemini-3.5-flash-lite"  # replies and handoff notes: 2.7x faster, 21% fewer output tokens, same decisions on 38 of 40 tickets
    model_opinion: str = "gemini-3.8-flash"    # the second-opinion agent: reasoning, so it keeps the stronger model
    state_dir: Path = ROOT / "state"
    log_dir: Path = ROOT / "logs"
    helpdesk_rps: float = 4.0  # the helpdesk answers 429 above 5 requests per second
    gemini_tpm: int = 45_000  # stay under the 50k tokens-per-minute free tier
    judge_backend: str = "typesafe"  # "typesafe": Jev on TypeSafe (recommended: faster, cheaper); "gemini": gemini-decision-1 (more accurate on the labelled examples)
    typesafe_api_key: str = field(default="", repr=False)
    model_typesafe: str = "jev-1.14.0"  # pinned: thresholds are tuned against one version, an alias can move
    retrieval: str = "exhaustive"  # "exhaustive": the judge scores every entry; "cascade": a sharded index shortlists first
    shortlist_size: int = 6        # entries the judge sees in cascade mode (24 is the production default; 6 shows the effect on 12 entries)
    second_opinion: bool = True  # an agent reviews borderline reply candidates; SECOND_OPINION=0 / --no-second-opinion turns it off

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
            model_draft=os.environ.get("MODEL_DRAFT", "gemini-3.5-flash-lite"),
            model_opinion=os.environ.get("MODEL_OPINION", "gemini-3.8-flash"),
            judge_backend=_default_backend(),
            typesafe_api_key=os.environ.get("TYPESAFE_API_KEY", ""),
            model_typesafe=os.environ.get("MODEL_TYPESAFE", "jev-1.14.0"),
            retrieval="cascade" if os.environ.get("RETRIEVAL", "").strip().lower() == "cascade" else "exhaustive",
            shortlist_size=int(os.environ.get("SHORTLIST_SIZE", "6") or 6),
            second_opinion=os.environ.get("SECOND_OPINION", "1").strip().lower() not in ("0", "false", "no", "off"),
        )

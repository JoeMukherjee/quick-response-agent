"""Failure taxonomy. Every failure the agent can meet falls into exactly one class, and each class has one behaviour:

  FatalAuthError   an invalid or unauthorised key (helpdesk 401, Gemini invalid key/403). The ONLY condition that
                   stops a run: nothing can succeed, so the run aborts cleanly with a message (no traceback).
  JudgeError       transient model failure (5xx, 429, timeout, bad structure, other 4xx). The ticket stays open,
                   the failure is logged and counted, the run continues and exits non-zero, a re-run finishes it.
  PermanentJudgeError  a genuine safety block of one ticket's content: that ticket goes to a human.
  HelpdeskError    typed helpdesk failures (see helpdesk.py), each handled where it occurs.
"""
from __future__ import annotations


class FatalAuthError(RuntimeError):
    """The credentials are invalid. Retrying or continuing cannot help."""


# Substrings that mark an invalid-key response from the Gemini API (the SDK raises APIError with these in the text).
INVALID_KEY_MARKERS = ("api key not valid", "api_key_invalid", "permission_denied", "invalid api key",
                       "api key expired", "unauthenticated")
# Decision API error codes that mean "this content was blocked", not "our request is broken".
SAFETY_MARKERS = ("safety", "prohibited_content", "blocklist", "image_safety", "content_blocked")


def looks_like_invalid_key(text: str, code: int | None = None) -> bool:
    low = (text or "").lower()
    return code in (401, 403) or any(m in low for m in INVALID_KEY_MARKERS)


def looks_like_safety_block(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in SAFETY_MARKERS)

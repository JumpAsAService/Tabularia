"""Masks the personal data for whoever reads without being in charge.

An observer sees the administration panels, and those panels hold emails, IP
addresses and the trace of what each did. They are the data an administrator needs to
decide, and that whoever only looks does not need at all: knowing WHAT happens in the
installation is enough for them, not WHO is behind it.

It is not a concession to a public demo — even if it was born there. An observer who
checks that things run without being able to identify the people is the more
defensible of the two figures, and the panels stay readable: an activity stays
attributed stably (the same email always gives the same mask), so «these three
sign-ins are the same person» still shows.

An administrator does not go through here: whoever is in charge sees.
"""
from __future__ import annotations


def mask_email(email: str | None) -> str:
    """`alice.rossi@esempio.it` → `a***@esempio.it`.

    The domain stays: it says whether the access comes from inside or outside the
    organization, which is useful information and identifies nobody. The first letter
    keeps two people in the same list apart without saying who they are."""
    text = (email or "").strip()
    if "@" not in text:
        return "***" if text else ""
    locale, _, domain = text.rpartition("@")
    return f"{locale[:1]}***@{domain}" if locale else f"***@{domain}"

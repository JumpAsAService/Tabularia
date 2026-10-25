"""Removes the secrets from the texts that reach the user.

Needed because the engine puts the storage's credentials INSIDE the SQL: without a
`named collection`, every source becomes

    s3('<url>', '<access key>', '<secret key>', 'Parquet')

ClickHouse masks the secrets by parsing the query, but when the query does not PARSE
there is nothing to mask and the error message carries the fragment in clear (audit
2026-09-19, A2: reproduced with fake keys). From there the text goes up to the editor's
toast, to `Run.error_detail` stored in the database and, through the assistant, to the
model's provider.

It is redacted at the SOURCE — in the process that knows the secrets — so every consumer
downstream gets clean text without having to remember.

It does not replace the `named collection`, which stays the right solution: there the
keys do not enter the query at all. This is the safety net for the installations that do
not use it, and for any secret that one day ended up in an exception's message.
"""
from __future__ import annotations

import re

MASK = "***"
# below this length nothing is redacted: a "key" of a few characters would appear
# everywhere by chance and eat the message
_MIN_LEN = 8


def _secrets() -> list[str]:
    from app.core.config import get_settings

    cfg = get_settings()
    raw_values: list[str] = []

    def add(v) -> None:
        if v is None:
            return
        text = v.get_secret_value() if hasattr(v, "get_secret_value") else str(v)
        if text and len(text) >= _MIN_LEN:
            raw_values.append(text)

    add(cfg.storage.access_key)
    add(cfg.storage.secret_key)
    ch = getattr(cfg, "clickhouse_external", None)
    if ch is not None:
        add(getattr(ch, "password", None))
        add(getattr(ch, "ai_password", None))
    bq = getattr(cfg, "bigquery", None)
    if bq is not None:
        add(getattr(bq, "credentials_b64", None))
    # the longest first: when a secret holds another, the outer one is redacted first
    # and no fragments remain
    return sorted(set(raw_values), key=len, reverse=True)


def redact_secrets(text: str | None) -> str | None:
    """Replaces every known secret found in the text with `***`.

    It never raises: an error here would turn an error message into another error, and
    the caller is handling a fault already."""
    if not text:
        return text
    try:
        clean = text
        for secret in _secrets():
            if secret in clean:
                clean = clean.replace(secret, MASK)
        return clean
    except Exception:  # pragma: no cover — the redaction must never propagate
        return text


# functions that touch the storage or the network: they hold URLs, buckets, paths
# and — on ClickHouse without a named collection — the keys
_STORAGE_NAMES = (
    "s3", "s3Cluster", "url", "urlCluster", "remote", "remoteSecure",
    "azureBlobStorage", "gcs", "hdfs",
)
_OPENING = re.compile(r"\b(" + "|".join(_STORAGE_NAMES) + r")\s*\(", re.IGNORECASE)


def _end_of_arguments(text: str, start: int) -> int | None:
    """The index of the parenthesis closing the one opened at `start`, or None when the text
    ENDS before. Counts the parentheses only outside quotes, because a URL or a path may
    hold some."""
    depth = 0
    quote = None
    i = start
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "'\"`":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def redact_storage_args(text: str | None) -> str | None:
    """Empties the arguments of `s3(...)`, `url(...)` and the like.

    ClickHouse's `query_log` masks the SECRET key by itself, but not the ACCESS key, and
    with it the endpoint, the bucket and the object's path stay in clear — checked on a
    ClickHouse 24.8 on 2026-09-28. In a list of slow queries those arguments say nothing
    useful: whoever looks needs to know WHICH query weighs, not on which key.

    Two traps, both found live:

    - the text may be TRUNCATED (the `query_log` is read in slices): the parenthesis never
      closes, and the access key is right inside. Without a closing it is redacted to the
      end — better to lose a query's tail than publish a credential;
    - `s3(` may appear INSIDE a string (`WHERE name LIKE '%s3(%'`): there it is not a
      call and is left alone, otherwise a legitimate query is spoiled.
    """
    if not text:
        return text
    try:
        outside = []
        i = 0
        quote = None
        while i < len(text):
            c = text[i]
            if quote:                      # inside a string: just copied
                outside.append(c)
                if c == "\\" and i + 1 < len(text):
                    outside.append(text[i + 1])
                    i += 2
                    continue
                if c == quote:
                    quote = None
                i += 1
                continue
            if c in "'\"`":
                quote = c
                outside.append(c)
                i += 1
                continue
            m = _OPENING.match(text, i)
            if m:
                opens = m.end() - 1
                closes = _end_of_arguments(text, opens)
                outside.append(text[i:m.end()])
                outside.append("\u2026)")
                if closes is None:         # truncated: cut here
                    return "".join(outside)
                i = closes + 1
                continue
            outside.append(c)
            i += 1
        return "".join(outside)
    except Exception:  # pragma: no cover
        return text

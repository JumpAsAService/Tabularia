"""The release this code is: the version `pyproject.toml` declares.

The engine keeps what it computed (the step cache's copies, the counts, the column profiles,
the dashboards' numbers) under fingerprints of what was asked. What a release computes under
the same question may differ from what the one before did (a fix of a cast is exactly that),
so the release is part of those fingerprints: after an upgrade nothing computed before is
read again, and what is left expires as anything no longer read does.

The release and not a number to raise by hand: a forgotten number serves the old results
silently, and two lines of releases (main and a long-term support line) would each raise
their own. Every release has another version (the release workflow checks this very file
against the tag), so the cost is a cache that starts empty once per upgrade.

Read once per process from the file beside `app/` (the image copies it there, the
development stack mounts it), never on the way of a request.
"""
from __future__ import annotations

import logging
import tomllib
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"
UNKNOWN = "unknown"


def read() -> str:
    """The version the project file declares, or UNKNOWN when it cannot be read: a cache that
    does not tell the releases apart is no reason not to start."""
    try:
        with open(PYPROJECT, "rb") as f:
            version = tomllib.load(f)["project"]["version"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as e:
        logger.warning("release: %s does not say the version (%r): the caches cannot tell this release from another", PYPROJECT, e)
        return UNKNOWN
    return str(version).strip() or UNKNOWN


@lru_cache(maxsize=1)
def current() -> str:
    return read()

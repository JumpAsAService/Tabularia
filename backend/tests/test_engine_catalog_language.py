"""The engine catalog (GET /engines) is English: the new-flow menu showed the
BigQuery description in Italian in the English interface (1.2.0 getting-started
run). The interface translates the descriptions it knows; this is the text API
clients and unknown engines get."""
import re

from app.engine import ENGINE_CATALOG

ITALIAN = re.compile(r"\b(motore|progetto|predefinito|configurato|imposta|senza|trasporto|legge|girano|operazioni|dialetto)\b", re.I)


def test_every_engine_description_is_english():
    for e in ENGINE_CATALOG:
        assert not ITALIAN.search(e["description"]), (e["id"], e["description"])

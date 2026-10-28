"""The names of the months and of the days in Tabularia's five languages, written by the
placeholders' readable formats (`{today:MMMM yyyy}`, app/core/placeholders.py) and by the
reports' formats (app/reports/formats.py): one table, so both write «ottobre» the same way.
"""
from __future__ import annotations

from typing import NamedTuple

LANGUAGES = ("en", "it", "de", "es", "fr")


class Names(NamedTuple):
    months: tuple[str, ...]
    months_short: tuple[str, ...]
    days: tuple[str, ...]  # Monday first
    days_short: tuple[str, ...]


NAMES = {
    "en": Names(
        ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
         "November", "December"),
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
        ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
        ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
    ),
    "it": Names(
        ("gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre",
         "novembre", "dicembre"),
        ("gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic"),
        ("lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"),
        ("lun", "mar", "mer", "gio", "ven", "sab", "dom"),
    ),
    "de": Names(
        ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
         "November", "Dezember"),
        ("Jan.", "Feb.", "März", "Apr.", "Mai", "Juni", "Juli", "Aug.", "Sept.", "Okt.", "Nov.", "Dez."),
        ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"),
        ("Mo.", "Di.", "Mi.", "Do.", "Fr.", "Sa.", "So."),
    ),
    "es": Names(
        ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
         "noviembre", "diciembre"),
        ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sept", "oct", "nov", "dic"),
        ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"),
        ("lun", "mar", "mié", "jue", "vie", "sáb", "dom"),
    ),
    "fr": Names(
        ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
         "novembre", "décembre"),
        ("janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."),
        ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"),
        ("lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."),
    ),
}


def names(lang: str | None) -> Names:
    """The names of a language by its code; English for anything else."""
    return NAMES.get((lang or "en").lower()[:2], NAMES["en"])

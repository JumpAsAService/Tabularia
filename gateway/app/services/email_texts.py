"""The texts of the emails the product sends, in the five languages of the interface.

A notice goes to each recipient in the language they chose in the interface
(`users.locale`, written by PUT /users/me/locale); an address that is not a user
of the installation, or a user who never chose, gets English. The recipients of
one notice are split by language: one message per language (`by_language`).

Used by services/notifier.py (a scheduled flow failed) and
services/contract_notifier.py (a data contract changed state).
"""
from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy import func
from sqlmodel import Session, select

LANGUAGES = ("en", "it", "fr", "de", "es")
DEFAULT = "en"

TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "when": "When:",
        "run": "Run:",
        "engine": "Engine:",
        "error": "Error:",
        "origin": "Origin:",
        "contract": "Contract:",
        "rows": "Rows:",
        "flow_subject": "[Tabularia] Flow «{flow}» failed",
        "flow_intro": "The flow «{flow}» did not succeed.",
        "flow_footer": "Automatic message from Tabularia. To stop receiving it, remove the addresses from the flow's notifications.",
        "contract_subject": "[Tabularia] Data contract of «{ds}»: {title}",
        "contract_intro": "Data contract of the datasource «{ds}»: {title}.",
        "contract_version": "version {version}",
        "contract_run": "(run #{run})",
        "contract_broken": "Rules not met:",
        "contract_more": "… and {n} more",
        "contract_unevaluable": "The contract could not be evaluated: {error}",
        "contract_footer": "Automatic message from Tabularia. To stop receiving it, remove the addresses from the data contract's notifications.",
        "title_refused": "update refused",
        "title_failed": "contract broken",
        "title_warning": "rules not met (warning)",
        "title_passed": "met again",
        "phrase_refused": "An update broke a blocking rule and was NOT published: the datasource still serves the previous data.",
        "phrase_failed": "The data the datasource serves break a blocking rule.",
        "phrase_warning": "The data the datasource serves break some non-blocking rules.",
        "phrase_passed": "All the rules are met again.",
        "origin_refresh": "refresh of the datasource",
        "origin_publish": "run of a flow",
        "origin_freshness": "freshness (the time since the last update)",
        "severity_warning": "warning",
        "severity_error": "blocking",
        "rule_on": "on {where}",
        "rule_unevaluable": "not evaluable ({error})",
        "rule_rows": "{n} rows",
        "rule_observed": "observed {value}",
    },
    "it": {
        "when": "Quando:",
        "run": "Esecuzione:",
        "engine": "Motore:",
        "error": "Errore:",
        "origin": "Origine:",
        "contract": "Contratto:",
        "rows": "Righe:",
        "flow_subject": "[Tabularia] Flusso «{flow}» fallito",
        "flow_intro": "Il flusso «{flow}» non è andato a buon fine.",
        "flow_footer": "Messaggio automatico di Tabularia. Per non riceverlo più, togli gli indirizzi dalle notifiche del flusso.",
        "contract_subject": "[Tabularia] Data contract di «{ds}»: {title}",
        "contract_intro": "Data contract della datasource «{ds}»: {title}.",
        "contract_version": "versione {version}",
        "contract_run": "(esecuzione #{run})",
        "contract_broken": "Regole non rispettate:",
        "contract_more": "… e altre {n}",
        "contract_unevaluable": "Il contratto non si è potuto valutare: {error}",
        "contract_footer": "Messaggio automatico di Tabularia. Per non riceverlo più, togli gli indirizzi dalle notifiche del data contract.",
        "title_refused": "aggiornamento rifiutato",
        "title_failed": "contratto violato",
        "title_warning": "regole non rispettate (avviso)",
        "title_passed": "di nuovo rispettato",
        "phrase_refused": "Un aggiornamento violava una regola bloccante e NON è stato pubblicato: la datasource serve ancora i dati precedenti.",
        "phrase_failed": "I dati che la datasource serve non rispettano una regola bloccante.",
        "phrase_warning": "I dati che la datasource serve non rispettano alcune regole non bloccanti.",
        "phrase_passed": "Tutte le regole sono di nuovo rispettate.",
        "origin_refresh": "refresh della datasource",
        "origin_publish": "esecuzione di un flusso",
        "origin_freshness": "freschezza (il tempo passato dall'ultimo aggiornamento)",
        "severity_warning": "avviso",
        "severity_error": "bloccante",
        "rule_on": "su {where}",
        "rule_unevaluable": "non valutabile ({error})",
        "rule_rows": "{n} righe",
        "rule_observed": "osservato {value}",
    },
    "fr": {
        "when": "Quand :",
        "run": "Exécution :",
        "engine": "Moteur :",
        "error": "Erreur :",
        "origin": "Origine :",
        "contract": "Contrat :",
        "rows": "Lignes :",
        "flow_subject": "[Tabularia] Le flux «{flow}» a échoué",
        "flow_intro": "Le flux «{flow}» n'a pas abouti.",
        "flow_footer": "Message automatique de Tabularia. Pour ne plus le recevoir, retirez les adresses des notifications du flux.",
        "contract_subject": "[Tabularia] Data contract de «{ds}» : {title}",
        "contract_intro": "Data contract de la source de données «{ds}» : {title}.",
        "contract_version": "version {version}",
        "contract_run": "(exécution #{run})",
        "contract_broken": "Règles non respectées :",
        "contract_more": "… et {n} autres",
        "contract_unevaluable": "Le contrat n'a pas pu être évalué : {error}",
        "contract_footer": "Message automatique de Tabularia. Pour ne plus le recevoir, retirez les adresses des notifications du data contract.",
        "title_refused": "mise à jour refusée",
        "title_failed": "contrat violé",
        "title_warning": "règles non respectées (avertissement)",
        "title_passed": "de nouveau respecté",
        "phrase_refused": "Une mise à jour violait une règle bloquante et n'a PAS été publiée : la source de données sert toujours les données précédentes.",
        "phrase_failed": "Les données servies par la source de données violent une règle bloquante.",
        "phrase_warning": "Les données servies par la source de données violent certaines règles non bloquantes.",
        "phrase_passed": "Toutes les règles sont de nouveau respectées.",
        "origin_refresh": "actualisation de la source de données",
        "origin_publish": "exécution d'un flux",
        "origin_freshness": "fraîcheur (le temps écoulé depuis la dernière mise à jour)",
        "severity_warning": "avertissement",
        "severity_error": "bloquante",
        "rule_on": "sur {where}",
        "rule_unevaluable": "non évaluable ({error})",
        "rule_rows": "{n} lignes",
        "rule_observed": "observé {value}",
    },
    "de": {
        "when": "Wann:",
        "run": "Ausführung:",
        "engine": "Engine:",
        "error": "Fehler:",
        "origin": "Ursprung:",
        "contract": "Vertrag:",
        "rows": "Zeilen:",
        "flow_subject": "[Tabularia] Fluss «{flow}» fehlgeschlagen",
        "flow_intro": "Der Fluss «{flow}» ist nicht erfolgreich gelaufen.",
        "flow_footer": "Automatische Nachricht von Tabularia. Um sie nicht mehr zu erhalten, entfernen Sie die Adressen aus den Benachrichtigungen des Flusses.",
        "contract_subject": "[Tabularia] Data Contract von «{ds}»: {title}",
        "contract_intro": "Data Contract der Datenquelle «{ds}»: {title}.",
        "contract_version": "Version {version}",
        "contract_run": "(Ausführung #{run})",
        "contract_broken": "Nicht eingehaltene Regeln:",
        "contract_more": "… und {n} weitere",
        "contract_unevaluable": "Der Vertrag konnte nicht ausgewertet werden: {error}",
        "contract_footer": "Automatische Nachricht von Tabularia. Um sie nicht mehr zu erhalten, entfernen Sie die Adressen aus den Benachrichtigungen des Data Contracts.",
        "title_refused": "Aktualisierung abgelehnt",
        "title_failed": "Vertrag verletzt",
        "title_warning": "Regeln nicht eingehalten (Warnung)",
        "title_passed": "wieder eingehalten",
        "phrase_refused": "Eine Aktualisierung verletzte eine blockierende Regel und wurde NICHT veröffentlicht: Die Datenquelle liefert weiterhin die vorherigen Daten.",
        "phrase_failed": "Die Daten der Datenquelle verletzen eine blockierende Regel.",
        "phrase_warning": "Die Daten der Datenquelle verletzen einige nicht blockierende Regeln.",
        "phrase_passed": "Alle Regeln werden wieder eingehalten.",
        "origin_refresh": "Aktualisierung der Datenquelle",
        "origin_publish": "Ausführung eines Flusses",
        "origin_freshness": "Aktualität (die Zeit seit der letzten Aktualisierung)",
        "severity_warning": "Warnung",
        "severity_error": "blockierend",
        "rule_on": "auf {where}",
        "rule_unevaluable": "nicht auswertbar ({error})",
        "rule_rows": "{n} Zeilen",
        "rule_observed": "beobachtet {value}",
    },
    "es": {
        "when": "Cuándo:",
        "run": "Ejecución:",
        "engine": "Motor:",
        "error": "Error:",
        "origin": "Origen:",
        "contract": "Contrato:",
        "rows": "Filas:",
        "flow_subject": "[Tabularia] El flujo «{flow}» falló",
        "flow_intro": "El flujo «{flow}» no terminó correctamente.",
        "flow_footer": "Mensaje automático de Tabularia. Para dejar de recibirlo, quita las direcciones de las notificaciones del flujo.",
        "contract_subject": "[Tabularia] Data contract de «{ds}»: {title}",
        "contract_intro": "Data contract de la fuente de datos «{ds}»: {title}.",
        "contract_version": "versión {version}",
        "contract_run": "(ejecución #{run})",
        "contract_broken": "Reglas no cumplidas:",
        "contract_more": "… y {n} más",
        "contract_unevaluable": "No se pudo evaluar el contrato: {error}",
        "contract_footer": "Mensaje automático de Tabularia. Para dejar de recibirlo, quita las direcciones de las notificaciones del data contract.",
        "title_refused": "actualización rechazada",
        "title_failed": "contrato incumplido",
        "title_warning": "reglas no cumplidas (aviso)",
        "title_passed": "cumplido de nuevo",
        "phrase_refused": "Una actualización incumplía una regla bloqueante y NO se publicó: la fuente de datos sigue sirviendo los datos anteriores.",
        "phrase_failed": "Los datos que sirve la fuente de datos incumplen una regla bloqueante.",
        "phrase_warning": "Los datos que sirve la fuente de datos incumplen algunas reglas no bloqueantes.",
        "phrase_passed": "Todas las reglas se cumplen de nuevo.",
        "origin_refresh": "actualización de la fuente de datos",
        "origin_publish": "ejecución de un flujo",
        "origin_freshness": "frescura (el tiempo desde la última actualización)",
        "severity_warning": "aviso",
        "severity_error": "bloqueante",
        "rule_on": "en {where}",
        "rule_unevaluable": "no evaluable ({error})",
        "rule_rows": "{n} filas",
        "rule_observed": "observado {value}",
    },
}


def language_of(code: Optional[str]) -> str:
    return code if code in TEXTS else DEFAULT


def text(lang: str, key: str, **values) -> str:
    return TEXTS[language_of(lang)][key].format(**values)


def labelled(lang: str, rows: list[tuple[str, str]]) -> list[str]:
    """«Label: value» lines with the values aligned, in that language."""
    labels = [text(lang, k) for k, _ in rows]
    width = max(len(x) for x in labels) + 1
    return [f"{label.ljust(width)}{value}" for label, (_, value) in zip(labels, rows)]


def by_language(session: Session, addresses: Iterable[str]) -> list[tuple[str, list[str]]]:
    """The recipients grouped by the language they get, in the order they were
    written: a user of the installation gets the language chosen in the interface,
    anyone else English."""
    from app.models import User

    addresses = list(addresses)
    lower = [a.lower() for a in addresses]
    chosen = dict(session.exec(
        select(func.lower(User.email), User.locale).where(func.lower(User.email).in_(lower), User.locale.is_not(None))  # type: ignore[union-attr]
    ).all()) if addresses else {}
    groups: dict[str, list[str]] = {}
    for a in addresses:
        groups.setdefault(language_of(chosen.get(a.lower())), []).append(a)
    return list(groups.items())

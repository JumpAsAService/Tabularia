"""Connessioni esterne: database (PostgreSQL/MySQL/MariaDB/ClickHouse/Trino)
e object storage S3-compatibile (db_type="s3": AWS, MinIO, R2, Wasabi…).

Per le connessioni S3 le colonne sono riusate con questo mapping:
host = endpoint URL (vuoto = AWS), username = access key id,
password_encrypted = secret access key, database = bucket di default,
db_schema = region.

Tutte le operazioni richiedono la capability CONNECT sul progetto della
connessione (ortogonale a VIEW/EDIT: chi gestisce i dati di una cartella non
usa credenziali DB per questo; MANAGE la include). La barriera di sicurezza è
QUI: chi può usare una connessione legge tutto ciò che le sue credenziali
leggono — consigliata un'utenza DB read-only.

La password/secret è cifrata a riposo (Fernet) e non esce mai dalle API: verso
l'engine viaggia ancora cifrata (`password_encrypted`, stessa chiave).

SFTP (db_type="sftp"): host, port (22), username, and the password OR a private
key with its passphrase (`private_key_encrypted`), chosen by `extra.auth`. The
server's host key is accepted by trust on first use: ONLY a test the engine made
writes it (`test_connection`), with an audit line, and a change of host or port
forgets it. Any host is allowed, as for databases: CONNECT is the barrier.

HTTP API (db_type="api"): `host` = the base URL, `username` = the basic user or the
OAuth2 client id, the encrypted secret = the API key / token / password / client
secret, `extra` = {auth, key_name, key_in, token_url, scope, client_auth, headers,
test_path}. A new host (or OAuth2 token URL) needs the secret typed again: it must
not follow the connection to another server. Any address is allowed: CONNECT is
the barrier, as for databases and SFTP.
"""
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Query, status, Request
from sqlalchemy import or_
from sqlmodel import Session, select

from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.lingua import lingua_della_richiesta
from app.core.engine_client import get_engine_client
from app.db.session import get_session
from app.deps.auth import get_current_user
from app.deps.permissions import ensure_can
from app.models import Connection, Datasource, Flow, Project, User
from app.services import audit
from app.models.permission import Capability
from app.schemas.models import (
    ApiCallSpec, ConnectionCreate, ConnectionOut, ConnectionTest, ConnectionUpdate, Page, PlaceholderPreviewQuery,
    S3FilesQuery, SftpFilesQuery, SharePointFilesQuery,
)
from app.services.pagination import paginate
from app.services import permissions as perm_service
from app.services.copy_names import commit_copy, free_copy_name
from app.core.routing import RottaCheRilascia

logger = logging.getLogger(__name__)

router = APIRouter(route_class=RottaCheRilascia, tags=["connections"])

SUPPORTED_DB_TYPES = {"postgresql", "mysql", "mariadb", "clickhouse", "trino", "oracle", "sqlserver", "s3", "smtp", "sharepoint", "sftp", "api"}
# connection types that are not databases: no table to read, none to write
NOT_DATABASES = ("smtp", "sharepoint", "sftp", "api")
# SQL Server: come trattare il TLS (`extra.tls`); vuoto = verify
_TLS_SQLSERVER = ("verify", "trust", "none")


def _to_out(conn: Connection) -> ConnectionOut:
    # NON model_dump(): dopo un commit (es. record_audit) l'istanza è "expired" e
    # model_dump torna {} → 500 con "11 validation errors". L'accesso per
    # attributo ricarica i valori dal DB (sessione ancora aperta).
    fields = {f: getattr(conn, f) for f in ConnectionOut.model_fields if f not in ("has_password", "has_private_key")}
    return ConnectionOut(**fields, has_password=bool(conn.password_encrypted),
                         has_private_key=bool(conn.private_key_encrypted))


def _get_connection(session: Session, conn_id: int) -> Connection:
    conn = session.get(Connection, conn_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    return conn


def _name_taken(session: Session, project_id: int, name: str, exclude_id: int | None = None) -> bool:
    stmt = select(Connection).where(Connection.project_id == project_id, Connection.name == name)
    if exclude_id is not None:
        stmt = stmt.where(Connection.id != exclude_id)
    return session.exec(stmt).first() is not None


def _valid_extra(extra: str | None, db_type: str | None = None) -> str:
    """Le opzioni del tipo sono testo opaco per il gateway, ma devono essere un
    OGGETTO JSON: accettarne uno rotto sposterebbe l'errore da chi salva la
    connessione a chi, giorni dopo, si chiede perché l'email non parte. Quelle che
    l'engine legge davvero (il TLS di SQL Server) si controllano qui."""
    import json as _json

    grezzo = (extra or "{}").strip() or "{}"
    try:
        data = _json.loads(grezzo)
    except _json.JSONDecodeError:
        raise HTTPException(status_code=422, detail="The connection options are not valid JSON")
    if not isinstance(data, dict):
        raise HTTPException(status_code=422, detail="The connection options must be a JSON object")
    if db_type == "sqlserver" and data.get("tls") not in (None, "", *_TLS_SQLSERVER):
        raise HTTPException(status_code=422, detail="SQL Server: the TLS option must be verify, trust or none")
    if db_type == "smtp":
        errors = _smtp_option_errors(data)
        if errors:
            raise HTTPException(status_code=422, detail="SMTP options: " + "; ".join(errors))
    if db_type == "sftp" and data.get("auth") not in (None, "", *SFTP_AUTH):
        raise HTTPException(status_code=422, detail="SFTP: auth must be password or key")
    if db_type == "api":
        error = _api_option_error(data)
        if error:
            raise HTTPException(status_code=422, detail=f"API: {error}")
    return grezzo


_TLS_SMTP = ("starttls", "ssl", "none")


def _smtp_option_errors(data: dict) -> list[str]:
    """What is wrong with the options of an SMTP connection, checked when it is
    saved: a wrong type there used to surface as a 500 at every flow launch."""
    errors = []
    if data.get("tls") not in (None, "", *_TLS_SMTP):
        errors.append("tls must be starttls, ssl or none")
    for key in ("from_address", "from_name"):
        if data.get(key) is not None and not isinstance(data[key], str):
            errors.append(f"{key} must be text")
    if _domain_list(data.get("allowed_domains")) is None:
        errors.append("allowed_domains must be a list of domains or a comma-separated text")
    return errors


def _domain_list(raw) -> list[str] | None:
    """The allowed domains as a list, or None when the value is not a list of text
    nor a comma-separated text."""
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, list) or not all(isinstance(d, str) for d in raw):
        return None
    return [d.strip().lower().lstrip("@") for d in raw if d.strip()]


def _db_options(db_type: str, extra: str | None) -> dict:
    """Le opzioni del tipo che l'engine usa per collegarsi (oggi: il TLS di SQL
    Server). Gli altri database non ne hanno: un oggetto vuoto."""
    if db_type != "sqlserver":
        return {}
    import json as _json

    try:
        data = _json.loads(extra or "{}")
    except _json.JSONDecodeError:
        data = {}
    tls = data.get("tls") if isinstance(data, dict) else None
    return {"tls": tls if tls in _TLS_SQLSERVER else "verify"}


def smtp_options(conn: Connection) -> dict:
    """Opzioni SMTP dalla colonna `extra` (JSON). Tollerante: una connessione
    creata prima di questa colonna, o con JSON rotto, non deve far esplodere un
    run — si ricade sui default."""
    import json as _json

    try:
        data = _json.loads(conn.extra or "{}")
        return data if isinstance(data, dict) else {}
    except _json.JSONDecodeError:
        return {}


def allowed_email_domains(conn: Connection) -> list[str]:
    """Domini a cui questa connessione può spedire. Elenco VUOTO = nessun limite
    (chi non ne ha bisogno non se ne accorge); valorizzato = barriera vera contro
    l'uso del nodo email come canale di uscita verso indirizzi arbitrari."""
    domains = _domain_list(smtp_options(conn).get("allowed_domains"))
    if domains is None:
        # a malformed restriction must not read as "no restriction": no email leaves
        raise HTTPException(
            status_code=422,
            detail=f"The allowed domains of the SMTP connection '{conn.name}' are malformed: fix the connection",
        )
    return domains


def _smtp_payload(conn: Connection) -> dict:
    opts = smtp_options(conn)
    return {
        "db_type": "smtp",
        "host": conn.host,
        "port": conn.port or 587,
        "username": conn.username,
        "password_encrypted": conn.password_encrypted,
        "from_address": opts.get("from_address") or "",
        "from_name": opts.get("from_name") or "",
        "tls": opts.get("tls") or "starttls",
    }


def engine_connection_payload(conn: Connection) -> dict:
    """Il payload `connection` per l'engine: password/secret ANCORA cifrata."""
    if conn.db_type == "sharepoint":
        return _sharepoint_payload(conn.host, conn.username, conn.password_encrypted, conn.database, conn.extra)
    if conn.db_type == "smtp":
        return _smtp_payload(conn)
    if conn.db_type == "sftp":
        return _sftp_payload(conn.host, conn.port, conn.username, conn.password_encrypted, conn.private_key_encrypted,
                             conn.extra, conn.host_key_type, conn.host_key_fingerprint)
    if conn.db_type == "s3":
        return _s3_payload(conn.host, conn.username, conn.password_encrypted, conn.database, conn.db_schema)
    if conn.db_type == "api":
        return _api_payload(conn.host, conn.username, conn.password_encrypted, conn.extra)
    return {
        "db_type": conn.db_type,
        "host": conn.host,
        "port": conn.port,
        "username": conn.username,
        "password_encrypted": conn.password_encrypted,
        "database": conn.database,
        "db_schema": conn.db_schema,
        "extra": _db_options(conn.db_type, conn.extra),
    }


def _sharepoint_payload(host: str, username: str, secret_encrypted: str, database: str, extra: str | None) -> dict:
    """SharePoint riusa le colonne esistenti, senza migrazioni: `host` = URL del
    sito, `username` = client id dell'app registrata su Entra ID, il campo cifrato
    = il suo secret, `database` = tenant id, `extra.library` = la raccolta documenti
    (vuota = quella predefinita). Gli endpoint Microsoft NON sono configurabili da
    qui: lasciarli scegliere a chi crea la connessione sarebbe un modo per far
    chiamare al backend un indirizzo qualunque con un token in mano."""
    import json as _json

    try:
        opts = _json.loads(extra or "{}")
    except _json.JSONDecodeError:
        opts = {}
    return {
        "tenant_id": (database or "").strip(),
        "client_id": (username or "").strip(),
        "client_secret_encrypted": secret_encrypted,
        "site_url": (host or "").strip(),
        "library": str(opts.get("library") or "").strip() if isinstance(opts, dict) else "",
    }


def _check_sharepoint(host: str, username: str, database: str) -> None:
    if not (host or "").strip().lower().startswith("https://"):
        raise HTTPException(status_code=422, detail="Invalid SharePoint site URL: expected https://<company>.sharepoint.com/sites/<name>")
    if not (username or "").strip() or not (database or "").strip():
        raise HTTPException(status_code=422, detail="The tenant id and the client id of the app registered in Entra ID are required")


# ── HTTP API ───────────────────────────────────────────────────────────────────
API_AUTH = ("none", "api_key", "bearer", "basic", "oauth2")
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def api_options(extra: str | None) -> dict:
    """The options of an API connection (`extra`), tolerant of a broken JSON."""
    import json as _json

    try:
        data = _json.loads(extra or "{}")
    except _json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _url_error(url: str, what: str) -> str | None:
    p = urlsplit((url or "").strip())
    if p.scheme not in ("http", "https") or not p.hostname:
        return f"invalid {what} '{url}': expected http(s)://host[:port][/path]"
    if p.username or p.password:
        return f"the {what} cannot hold a user or a password: use the authentication"
    if p.query or p.fragment:
        return (f"the {what} cannot hold a query (?…) or a fragment (#…): put the parameters in the datasource "
                "and the key in the authentication")
    return None


def _api_option_error(data: dict) -> str | None:
    """What is wrong with the options of an API connection (None = nothing)."""
    auth = data.get("auth") or "none"
    if auth not in API_AUTH:
        return "auth must be none, api_key, bearer, basic or oauth2"
    if auth == "api_key":
        name = str(data.get("key_name") or "").strip()
        if not name:
            return "the API key needs the name of its header or query parameter"
        if data.get("key_in", "header") not in ("header", "query"):
            return "the API key goes in a header or query parameter"
        if data.get("key_in", "header") == "header" and not _HEADER_NAME.match(name):
            return f"invalid name for the API key header '{name}'"
    if auth == "oauth2":
        if not str(data.get("token_url") or "").strip():
            return "OAuth2 needs the token URL"
        error = _url_error(str(data["token_url"]), "token URL")
        if error:
            return error
        if data.get("client_auth", "body") not in ("body", "basic"):
            return "the OAuth2 client authentication is body or basic"
    headers = data.get("headers") or {}
    if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
        return "the default headers must be an object of names and values (text)"
    for k, v in headers.items():
        if not _HEADER_NAME.match(k.strip() or "-"):
            return f"invalid header name '{k}'"
        if "\r" in v or "\n" in v:
            return f"the header '{k}' cannot hold a line break"
    test_path = str(data.get("test_path") or "")
    if "://" in test_path or test_path.startswith("//"):
        return "the test path must be relative to the base URL (e.g. /status)"
    return None


def _check_api(host: str, username: str, extra: str | None, has_secret: bool) -> None:
    error = _url_error(host, "base URL")
    opts = api_options(extra)
    auth = opts.get("auth") or "none"
    if error is None:
        error = _api_option_error(opts)
    if error is None and auth in ("basic", "oauth2") and not (username or "").strip():
        error = "OAuth2 needs the client id" if auth == "oauth2" else "basic authentication needs the user"
    if error is None and auth != "none" and not has_secret:
        error = "enter the secret of the authentication (the API key, the token, the password or the client secret)"
    if error:
        raise HTTPException(status_code=422, detail=f"API: {error}")


def _api_origin(url: str) -> tuple[str, str, int | None]:
    p = urlsplit((url or "").strip())
    try:
        port = p.port
    except ValueError:
        port = None
    scheme = (p.scheme or "").lower()
    return scheme, (p.hostname or "").lower(), port or (443 if scheme == "https" else 80)


def _api_payload(host: str, username: str, secret_encrypted: str, extra: str | None) -> dict:
    """Only what the chosen authentication uses travels; the secret still encrypted."""
    opts = api_options(extra)
    auth = opts.get("auth") if opts.get("auth") in API_AUTH else "none"
    headers = opts.get("headers") if isinstance(opts.get("headers"), dict) else {}
    return {
        "db_type": "api",
        "base_url": (host or "").strip(),
        "auth": auth,
        "username": (username or "").strip() if auth in ("basic", "oauth2") else "",
        "secret_encrypted": secret_encrypted if auth != "none" else "",
        "key_name": str(opts.get("key_name") or "").strip(),
        "key_in": opts.get("key_in") if opts.get("key_in") in ("header", "query") else "header",
        "token_url": str(opts.get("token_url") or "").strip(),
        "scope": str(opts.get("scope") or "").strip(),
        "client_auth": opts.get("client_auth") if opts.get("client_auth") in ("body", "basic") else "body",
        "headers": {str(k).strip(): str(v) for k, v in headers.items()},
        "test_path": str(opts.get("test_path") or "").strip(),
    }


SFTP_AUTH = ("password", "key")


def sftp_auth(extra: str | None) -> str:
    """Which secret authenticates an SFTP connection (`extra.auth`; default password)."""
    import json as _json

    try:
        data = _json.loads(extra or "{}")
    except _json.JSONDecodeError:
        data = {}
    auth = data.get("auth") if isinstance(data, dict) else None
    return auth if auth in SFTP_AUTH else "password"


def _check_private_key(text: str) -> None:
    t = (text or "").strip()
    if not (t.startswith("-----BEGIN") and "PRIVATE KEY-----" in t):
        raise HTTPException(
            status_code=422,
            detail="This is not a private key: paste the whole PEM or OpenSSH private key, from -----BEGIN to -----END",
        )


def _key_blob(private_key: str, passphrase: str) -> str:
    """The private key and its passphrase, encrypted TOGETHER (one secret)."""
    import json as _json

    return encrypt_secret(_json.dumps({"private_key": private_key.strip() + "\n", "passphrase": passphrase or ""}))


def _check_sftp(host: str, port: int | None, username: str, auth: str, has_password: bool, has_key: bool) -> None:
    h = (host or "").strip()
    if not h or "://" in h or "/" in h or " " in h:
        raise HTTPException(status_code=422, detail="SFTP: enter the host name or IP address only (no sftp://, no path)")
    if port is not None and not 0 < port < 65536:
        raise HTTPException(status_code=422, detail="SFTP: the port must be between 1 and 65535")
    if not (username or "").strip():
        raise HTTPException(status_code=422, detail="SFTP: the user name is required")
    if auth == "password" and not has_password:
        raise HTTPException(status_code=422, detail="SFTP: enter the password, or choose the private key method")
    if auth == "key" and not has_key:
        raise HTTPException(status_code=422, detail="SFTP: paste the private key, or choose the password method")


def _sftp_payload(host: str, port: int | None, username: str, password_encrypted: str, private_key_encrypted: str,
                  extra: str | None, host_key_type: str = "", host_key_fingerprint: str = "") -> dict:
    """Only the secret of the chosen method travels: a password left from an
    earlier setup is not offered to the server when the key is the method."""
    auth = sftp_auth(extra)
    return {
        "db_type": "sftp",
        "host": (host or "").strip(),
        "port": port or 22,
        "username": (username or "").strip(),
        "auth": auth,
        "password_encrypted": password_encrypted if auth == "password" else "",
        "private_key_encrypted": private_key_encrypted if auth == "key" else "",
        "host_key_type": host_key_type or "",
        "host_key_fingerprint": host_key_fingerprint or "",
    }


# Host key messages for a person, in the language of the request: the engine
# refuses with a code (409) and the gateway explains it.
_HOST_KEY_CHANGED = {
    "en": "The host key of {host} has changed: accepted {expected}, offered now {offered}. Nothing was sent to the server. "
          "If the change is expected (server reinstalled, key renewed), run «Test connection» again to accept the new key; "
          "otherwise ask the server's administrator.",
    "it": "La chiave dell'host {host} è cambiata: accettata {expected}, offerta ora {offered}. Al server non è stato mandato nulla. "
          "Se il cambio è atteso (server reinstallato, chiave rinnovata), rifai «Prova connessione» per accettare la nuova chiave; "
          "altrimenti chiedi all'amministratore del server.",
    "de": "Der Host-Schlüssel von {host} hat sich geändert: akzeptiert {expected}, jetzt angeboten {offered}. An den Server wurde nichts gesendet. "
          "Ist die Änderung erwartet (Server neu installiert, Schlüssel erneuert), führen Sie «Verbindung testen» erneut aus, um den neuen Schlüssel "
          "zu akzeptieren; andernfalls fragen Sie den Administrator des Servers.",
    "fr": "La clé d'hôte de {host} a changé : acceptée {expected}, proposée maintenant {offered}. Rien n'a été envoyé au serveur. "
          "Si le changement est attendu (serveur réinstallé, clé renouvelée), relancez « Tester la connexion » pour accepter la nouvelle clé ; "
          "sinon, demandez à l'administrateur du serveur.",
    "es": "La clave de host de {host} ha cambiado: aceptada {expected}, ofrecida ahora {offered}. No se ha enviado nada al servidor. "
          "Si el cambio es esperado (servidor reinstalado, clave renovada), vuelva a ejecutar «Probar conexión» para aceptar la nueva clave; "
          "si no, pregunte al administrador del servidor.",
}
_HOST_KEY_UNKNOWN = {
    "en": "The host key of {host} has not been accepted yet: run «Test connection» on the connection to see it and accept it.",
    "it": "La chiave dell'host {host} non è ancora stata accettata: fai «Prova connessione» sulla connessione per vederla e accettarla.",
    "de": "Der Host-Schlüssel von {host} wurde noch nicht akzeptiert: Führen Sie «Verbindung testen» für die Verbindung aus, um ihn zu sehen und zu akzeptieren.",
    "fr": "La clé d'hôte de {host} n'a pas encore été acceptée : lancez « Tester la connexion » sur la connexion pour la voir et l'accepter.",
    "es": "La clave de host de {host} aún no ha sido aceptada: ejecute «Probar conexión» en la conexión para verla y aceptarla.",
}
_HOST_KEY_NOT_THE_TESTED_ONE = {
    "en": "{host} now offers the key {offered}, not the one your test showed ({expected}): it was not accepted. Run the test again.",
    "it": "{host} ora offre la chiave {offered}, non quella mostrata dalla tua prova ({expected}): non è stata accettata. Rifai la prova.",
    "de": "{host} bietet jetzt den Schlüssel {offered} an, nicht den von Ihrem Test gezeigten ({expected}): Er wurde nicht akzeptiert. Testen Sie erneut.",
    "fr": "{host} propose maintenant la clé {offered}, pas celle montrée par votre test ({expected}) : elle n'a pas été acceptée. Relancez le test.",
    "es": "{host} ofrece ahora la clave {offered}, no la que mostró su prueba ({expected}): no ha sido aceptada. Vuelva a probar.",
}


# Microsoft's SQL Server driver is not in the published images (it is under Microsoft's
# licence): the guide to adding it, linked from the message and from the form
SQLSERVER_DOCS_URL = "https://github.com/JumpAsAService/Tabularia/blob/main/docs/sqlserver.md"
_DRIVER_DOCS = {"sqlserver": SQLSERVER_DOCS_URL}
_DRIVER_LABELS = {"sqlserver": "SQL Server"}
_DRIVER_NOT_INSTALLED = {
    "en": "The {label} driver is not installed in this installation: the published images do not include it, "
          "because it is Microsoft's, under Microsoft's licence. Whoever runs Tabularia can add it: {docs}",
    "it": "Il driver {label} non è installato in questa installazione: le immagini pubblicate non lo includono, "
          "perché è di Microsoft, sotto la licenza di Microsoft. Chi gestisce Tabularia può aggiungerlo: {docs}",
    "de": "Der {label}-Treiber ist in dieser Installation nicht installiert: Die veröffentlichten Images enthalten ihn nicht, "
          "weil er von Microsoft stammt und unter Microsofts Lizenz steht. Wer Tabularia betreibt, kann ihn hinzufügen: {docs}",
    "fr": "Le pilote {label} n'est pas installé dans cette installation : les images publiées ne l'incluent pas, "
          "car il est de Microsoft, sous la licence de Microsoft. Qui gère Tabularia peut l'ajouter : {docs}",
    "es": "El controlador de {label} no está instalado en esta instalación: las imágenes publicadas no lo incluyen, "
          "porque es de Microsoft, bajo la licencia de Microsoft. Quien gestiona Tabularia puede añadirlo: {docs}",
}


def _translated(table: dict, lingua: str, **values) -> str:
    return table.get(lingua, table["en"]).format(**values)


def _s3_payload(host: str, username: str, secret_encrypted: str, database: str, db_schema: str) -> dict:
    """Mapping colonne→campi S3 (vedi docstring del modulo)."""
    return {
        "db_type": "s3",
        "endpoint_url": host or "",
        "access_key": username or "",
        "secret_key_encrypted": secret_encrypted or "",
        "bucket": database or "",
        "region": db_schema or "",
    }


async def _engine_inspect(payload: dict, action: str, path: str = "", lingua: str = "en", **extra) -> dict:
    client = get_engine_client()
    if "tenant_id" in payload:  # SharePoint ha le sue rotte sull'engine
        resp = await client.post("/sharepoint/inspect", json={"connection": payload, "action": action, "path": path, **extra})
    elif payload.get("db_type") == "s3" and action in ("files", "validate"):
        # the «S3 folder» datasource; the connection test stays on /db/inspect
        resp = await client.post("/s3/inspect", json={"connection": payload, "action": action, **extra})
    elif payload.get("db_type") == "sftp":
        resp = await client.post("/sftp/inspect", json={"connection": payload, "action": action, **extra})
    elif payload.get("db_type") == "api":
        resp = await client.post("/http-api/inspect", json={"connection": payload, "action": action, **extra})
    else:
        resp = await client.post("/db/inspect", json={"connection": payload, "action": action})
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        if isinstance(detail, dict) and detail.get("code") == "driver_not_installed":
            db_type = str(detail.get("db_type") or payload.get("db_type") or "")
            raise HTTPException(status_code=422, detail=_translated(
                _DRIVER_NOT_INSTALLED, lingua, label=_DRIVER_LABELS.get(db_type, db_type),
                docs=_DRIVER_DOCS.get(db_type, SQLSERVER_DOCS_URL)))
        if isinstance(detail, dict) and detail.get("code") in ("host_key_changed", "host_key_unknown"):
            table = _HOST_KEY_CHANGED if detail["code"] == "host_key_changed" else _HOST_KEY_UNKNOWN
            detail = _translated(table, lingua, host=detail.get("host") or payload.get("host", ""),
                                 expected=detail.get("expected") or "—", offered=detail.get("offered") or "—")
            raise HTTPException(status_code=409, detail=detail)
        raise HTTPException(status_code=resp.status_code, detail=str(detail)[:500])
    return resp.json()


# ── CRUD ──────────────────────────────────────────────────────────────────────
@router.get("/connections", response_model=list[ConnectionOut])
def list_all_connections(user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """Tutte le connessioni USABILI dall'utente (per il picker delle sorgenti DB)."""
    connectable = perm_service.connectable_project_ids(session, user)
    if not connectable:
        return []
    rows = session.exec(
        select(Connection).where(Connection.project_id.in_(connectable)).order_by(Connection.name)
    ).all()
    return [_to_out(c) for c in rows]


@router.get("/connections/search", response_model=Page[ConnectionOut])
def search_connections(
    q: str | None = Query(None, description="cerca su nome/host/database, sull'INTERO dataset"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Connessioni usabili dall'utente, PAGINATE, con ricerca server-side sul
    dataset intero. Per la pagina globale Connections."""
    connectable = perm_service.connectable_project_ids(session, user)
    if not connectable:
        return Page(items=[], total=0)
    base = select(Connection).where(Connection.project_id.in_(connectable))
    if q:
        like = f"%{q}%"
        base = base.where(
            or_(Connection.name.ilike(like), Connection.host.ilike(like), Connection.database.ilike(like))
        )
    rows, total = paginate(session, base, Connection.name, limit, offset)
    return Page(items=[_to_out(c) for c in rows], total=total)


@router.get("/connections/drivers")
async def connection_drivers(user: User = Depends(get_current_user)):
    """The optional database drivers and whether the engine has them, with the guide
    to adding one: the form marks a type whose driver is missing. An engine that does
    not say (older, or not answering) marks nothing — no warning rather than a false
    one; a test would still say it."""
    try:
        resp = await get_engine_client().get("/db/drivers", timeout=10)
        drivers = resp.json() if resp.status_code == 200 else {}
    except Exception:  # noqa: BLE001 — the engine down: the form works without the hint
        logger.warning("the engine did not report its database drivers", exc_info=True)
        drivers = {}
    if not isinstance(drivers, dict):
        return {}
    return {db_type: {"installed": bool((info or {}).get("installed")), "docs": _DRIVER_DOCS.get(db_type, "")}
            for db_type, info in drivers.items() if isinstance(info, dict)}


@router.get("/projects/{project_id}/connections", response_model=list[ConnectionOut])
def list_project_connections(
    project_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    ensure_can(session, user, project_id, Capability.CONNECT)
    rows = session.exec(
        select(Connection).where(Connection.project_id == project_id).order_by(Connection.name)
    ).all()
    return [_to_out(c) for c in rows]


@router.post(
    "/projects/{project_id}/connections",
    response_model=ConnectionOut,
    status_code=status.HTTP_201_CREATED,
)
def create_connection(
    project_id: int,
    body: ConnectionCreate,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    ensure_can(session, user, project_id, Capability.CONNECT)
    if body.db_type not in SUPPORTED_DB_TYPES:
        raise HTTPException(status_code=422, detail=f"Unsupported db_type: use one of {sorted(SUPPORTED_DB_TYPES)}")
    if body.db_type == "sharepoint":
        _check_sharepoint(body.host, body.username, body.database)
    _valid_extra(body.extra, body.db_type)
    if body.private_key and body.db_type != "sftp":
        raise HTTPException(status_code=422, detail="Only SFTP connections take a private key")
    if body.db_type == "sftp":
        if body.private_key:
            _check_private_key(body.private_key)
        _check_sftp(body.host, body.port, body.username, sftp_auth(body.extra), bool(body.password), bool(body.private_key))
    if body.db_type == "api":
        _check_api(body.host, body.username, body.extra, bool(body.password))
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="The connection needs a name")
    if _name_taken(session, project_id, name):
        raise HTTPException(status_code=409, detail=f"There is already a connection '{name}' in this folder")

    conn = Connection(
        name=name,
        description=body.description,
        project_id=project_id,
        owner_id=user.id,
        db_type=body.db_type,
        host=body.host,
        port=body.port,
        username=body.username,
        password_encrypted=encrypt_secret(body.password) if body.password else "",
        database=body.database,
        db_schema=body.db_schema,
        extra=_valid_extra(body.extra, body.db_type),
        private_key_encrypted=_key_blob(body.private_key, body.private_key_passphrase) if body.private_key else "",
    )
    session.add(conn)
    session.commit()
    session.refresh(conn)
    audit.record_audit(
        session, actor=user, action=audit.CONN_CREATE, target_type="connection",
        target_id=conn.id, target_label=conn.name,
        detail={"project_id": project_id, "db_type": conn.db_type, "host": conn.host}, request=request,
    )
    return _to_out(conn)


@router.post("/connections/{conn_id}/duplicate", response_model=ConnectionOut, status_code=status.HTTP_201_CREATED)
def duplicate_connection(
    conn_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """A copy of the connection in its folder, as «name (i)», password included: it
    stays encrypted as it is, and CONNECT on the folder is what lets anyone use it
    in the first place."""
    src = _get_connection(session, conn_id)
    ensure_can(session, user, src.project_id, Capability.CONNECT)
    conn = Connection(
        name=free_copy_name(session, Connection, src.project_id, src.name),
        description=src.description,
        project_id=src.project_id,
        owner_id=user.id,
        db_type=src.db_type,
        host=src.host,
        port=src.port,
        username=src.username,
        password_encrypted=src.password_encrypted,
        database=src.database,
        db_schema=src.db_schema,
        extra=src.extra,
        # same server, same credentials: the accepted host key comes along
        private_key_encrypted=src.private_key_encrypted,
        host_key_type=src.host_key_type,
        host_key_fingerprint=src.host_key_fingerprint,
    )
    session.add(conn)
    commit_copy(session)
    session.refresh(conn)
    audit.record_audit(
        session, actor=user, action=audit.CONN_CREATE, target_type="connection",
        target_id=conn.id, target_label=conn.name,
        detail={"project_id": conn.project_id, "db_type": conn.db_type, "host": conn.host, "duplicate_of": src.id},
        request=request,
    )
    return _to_out(conn)


@router.patch("/connections/{conn_id}", response_model=ConnectionOut)
def update_connection(
    conn_id: int,
    body: ConnectionUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    conn = _get_connection(session, conn_id)
    ensure_can(session, user, conn.project_id, Capability.CONNECT)

    target_project = conn.project_id
    if body.project_id is not None and body.project_id != conn.project_id:
        if session.get(Project, body.project_id) is None:
            raise HTTPException(status_code=404, detail="Destination folder not found")
        ensure_can(session, user, body.project_id, Capability.CONNECT)
        target_project = body.project_id

    new_name = body.name.strip() if body.name is not None else conn.name
    if not new_name:
        raise HTTPException(status_code=422, detail="The connection needs a name")
    if _name_taken(session, target_project, new_name, exclude_id=conn.id):
        raise HTTPException(status_code=409, detail=f"There is already a connection '{new_name}' in this folder")

    if (body.private_key or body.private_key_passphrase) and conn.db_type != "sftp":
        raise HTTPException(status_code=422, detail="Only SFTP connections take a private key")
    if body.private_key:
        _check_private_key(body.private_key)
    address_before = (conn.host, conn.port)
    moved = False
    if conn.db_type == "sftp":
        moved = any(getattr(body, f) is not None and getattr(body, f) != getattr(conn, f) for f in ("host", "port", "username"))
        if moved and not (body.password or body.private_key):
            # the stored secret must not follow the connection to another server or account
            raise HTTPException(
                status_code=422,
                detail="SFTP: a new host, port or user needs the password (or the private key) typed again",
            )

    api_before = (conn.host, api_options(conn.extra).get("token_url") or "") if conn.db_type == "api" else None
    if api_before is not None:
        new_host = body.host if body.host is not None else conn.host
        new_opts = api_options(body.extra) if body.extra is not None else api_options(conn.extra)
        new_token_url = str(new_opts.get("token_url") or "")
        api_moved = _api_origin(new_host) != _api_origin(api_before[0]) or (
            bool(new_token_url) and new_token_url.strip() != str(api_before[1]).strip())
        if api_moved and (new_opts.get("auth") or "none") == "none":
            # no authentication any more: the stored secret is dropped, not carried along
            api_moved = False
            if body.password is None:
                body.password = ""
        if api_moved and not body.password and conn.password_encrypted:
            # the stored secret must not follow the connection to another server
            raise HTTPException(
                status_code=422,
                detail="API: a new host (or OAuth2 token URL) needs the secret typed again",
            )

    conn.name = new_name
    conn.project_id = target_project
    for field in ("description", "host", "port", "username", "database", "db_schema"):
        value = getattr(body, field)
        if value is not None:
            setattr(conn, field, value)
    if body.extra is not None:
        conn.extra = _valid_extra(body.extra, conn.db_type)
    if body.password is not None:
        # stringa vuota = rimuovi la password; valorizzata = sostituisci
        conn.password_encrypted = encrypt_secret(body.password) if body.password else ""
    if body.private_key is not None:
        conn.private_key_encrypted = _key_blob(body.private_key, body.private_key_passphrase or "") if body.private_key else ""
    elif body.private_key_passphrase is not None and conn.private_key_encrypted:
        # a passphrase alone: the stored key stays, with the new passphrase
        import json as _json

        stored = _json.loads(decrypt_secret(conn.private_key_encrypted))
        conn.private_key_encrypted = _key_blob(stored.get("private_key", ""), body.private_key_passphrase)
    if moved:
        # every secret not typed again in this edit stays behind: a key sent with the move must not
        # carry the stored password of the other method to the new server (nor the other way round)
        if not body.password:
            conn.password_encrypted = ""
        if not body.private_key:
            conn.private_key_encrypted = ""
    if conn.db_type == "api":
        try:
            _check_api(conn.host, conn.username, conn.extra, bool(conn.password_encrypted))
        except HTTPException:
            session.rollback()  # nothing of this edit stays in the session
            raise
    if conn.db_type == "sftp":
        try:
            _check_sftp(conn.host, conn.port, conn.username, sftp_auth(conn.extra),
                        bool(conn.password_encrypted), bool(conn.private_key_encrypted))
        except HTTPException:
            session.rollback()  # nothing of this edit stays in the session
            raise
        if (conn.host, conn.port) != address_before:
            # another address is another server until a test says which key it has
            conn.host_key_type = conn.host_key_fingerprint = ""
    conn.updated_at = datetime.now(timezone.utc)
    session.add(conn)
    session.commit()
    session.refresh(conn)
    audit.record_audit(
        session, actor=user, action=audit.CONN_UPDATE, target_type="connection",
        target_id=conn.id, target_label=conn.name, request=request,
    )
    return _to_out(conn)


@router.delete("/connections/{conn_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_connection(
    conn_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    conn = _get_connection(session, conn_id)
    ensure_can(session, user, conn.project_id, Capability.CONNECT)
    used_by = session.exec(select(Datasource).where(Datasource.connection_id == conn_id)).first()
    if used_by:
        raise HTTPException(
            status_code=409,
            detail="The connection is used by one or more datasources: delete or move them first",
        )
    notifying = session.exec(select(Flow.name).where(Flow.notify_connection_id == conn_id).order_by(Flow.name).limit(6)).all()
    if notifying:
        names = ", ".join(f"'{n}'" for n in notifying[:5]) + (", …" if len(notifying) > 5 else "")
        raise HTTPException(
            status_code=409,
            detail=f"The connection sends the failure notices of flows ({names}): change their schedule first",
        )
    conn_name, conn_type = conn.name, conn.db_type
    session.delete(conn)
    session.commit()
    audit.record_audit(
        session, actor=user, action=audit.CONN_DELETE, target_type="connection",
        target_id=conn_id, target_label=conn_name, detail={"db_type": conn_type}, request=request,
    )


# ── Ispezione (via engine) ────────────────────────────────────────────────────
@router.post("/projects/{project_id}/connections/test")
async def test_draft_connection(
    project_id: int,
    body: ConnectionTest,
    request: Request = None,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Prova una connessione PRIMA di salvarla (il form della UI), col solo segreto
    scritto nel form. For SFTP in two steps (see `test_connection`): it shows the
    server's host key without sending credentials; with `expected_fingerprint` it
    tests the credentials against exactly that key. It stores nothing."""
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    ensure_can(session, user, project_id, Capability.CONNECT)
    if body.db_type not in SUPPORTED_DB_TYPES:
        raise HTTPException(status_code=422, detail=f"Unsupported db_type: use one of {sorted(SUPPORTED_DB_TYPES)}")
    secret = encrypt_secret(body.password) if body.password else ""
    if body.db_type == "sftp":
        if body.private_key:
            _check_private_key(body.private_key)
        _check_sftp(body.host, body.port, body.username, sftp_auth(body.extra), bool(body.password), bool(body.private_key))
        key_blob = _key_blob(body.private_key, body.private_key_passphrase) if body.private_key else ""
        payload = _sftp_payload(body.host, body.port, body.username, secret, key_blob, body.extra)
        lingua = lingua_della_richiesta(request)
        result = await _engine_inspect(payload, "test", lingua=lingua, expected_fingerprint=body.expected_fingerprint.strip(),
                                       expected_type=body.expected_type.strip())
        _refuse_unoffered_key(result, body.expected_fingerprint.strip(), f"{body.host.strip()}:{body.port or 22}", lingua)
        return result
    if body.db_type == "api":
        _valid_extra(body.extra, "api")
        _check_api(body.host, body.username, body.extra, bool(body.password))
        return await _engine_inspect(_api_payload(body.host, body.username, secret, body.extra), "test")
    if body.db_type == "sharepoint":
        _check_sharepoint(body.host, body.username, body.database)
        payload = _sharepoint_payload(body.host, body.username, secret, body.database, body.extra)
    elif body.db_type == "s3":
        payload = _s3_payload(body.host, body.username, secret, body.database, body.db_schema)
    else:
        payload = {
            "db_type": body.db_type,
            "host": body.host,
            "port": body.port,
            "username": body.username,
            "password_encrypted": secret,
            "database": body.database,
            "db_schema": body.db_schema,
            "extra": _db_options(body.db_type, body.extra),
        }
    return await _engine_inspect(payload, "test", lingua=lingua_della_richiesta(request))


class HostKeyAcceptance(BaseModel):
    """SFTP: the key the user accepts — the one a previous test showed."""
    expected_fingerprint: str = ""
    expected_type: str = ""


def _refuse_unoffered_key(result: dict, expected: str, address: str, lingua: str) -> None:
    """The user accepted a key the server does not offer (any more): nothing was
    sent to it, and nothing is accepted."""
    if expected and result.get("needs_acceptance"):
        offered = str((result.get("host_key") or {}).get("fingerprint") or "—")
        raise HTTPException(status_code=409, detail=_translated(
            _HOST_KEY_NOT_THE_TESTED_ONE, lingua, host=address, offered=offered, expected=expected))


@router.post("/connections/{conn_id}/test")
async def test_connection(
    conn_id: int,
    body: HostKeyAcceptance | None = None,
    request: Request = None,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Tests a saved connection. For SFTP this is trust on first use, in two steps,
    and no credential ever goes to a key that is not accepted:

    1. without `expected_fingerprint`: the engine reads the server's key; if it is
       not the accepted one (never accepted, or changed) it closes WITHOUT
       authenticating and the answer says `needs_acceptance`, with the key and the
       previous one. Nothing is stored. With the accepted key it tests the
       credentials as for any connection;
    2. with `expected_fingerprint` (the key shown in step 1, which the user
       compared and accepts): the engine sends the credentials only if the server
       offers exactly that key. After a success the key is stored, with an audit
       line naming the previous one."""
    conn = _get_connection(session, conn_id)
    ensure_can(session, user, conn.project_id, Capability.CONNECT)
    lingua = lingua_della_richiesta(request)
    if conn.db_type != "sftp":
        return await _engine_inspect(engine_connection_payload(conn), "test", lingua=lingua)
    expected = (body.expected_fingerprint if body else "").strip()
    address = f"{conn.host}:{conn.port or 22}"
    result = await _engine_inspect(engine_connection_payload(conn), "test", lingua=lingua, expected_fingerprint=expected,
                                   expected_type=(body.expected_type if body else "").strip())
    _refuse_unoffered_key(result, expected, address, lingua)
    if result.get("needs_acceptance") or not result.get("ok"):
        return result  # shown, not accepted: no credential was sent
    seen = result.get("host_key") or {}
    offered, key_type = str(seen.get("fingerprint") or ""), str(seen.get("type") or "")
    if not offered:
        raise HTTPException(status_code=502, detail="SFTP: the engine did not report the server's host key")
    previous = conn.host_key_fingerprint
    if (conn.host_key_type, previous) != (key_type, offered):
        conn.host_key_type, conn.host_key_fingerprint = key_type, offered
        conn.updated_at = datetime.now(timezone.utc)
        session.add(conn)
        session.commit()
        audit.record_audit(
            session, actor=user, action=audit.CONN_HOST_KEY_ACCEPT, target_type="connection",
            target_id=conn.id, target_label=conn.name,
            detail={"type": key_type, "fingerprint": offered, "previous": previous, "host": address},
            request=request,
        )
    return {**result, "changed": bool(previous) and previous != offered, "previous": previous, "accepted": True}


@router.post("/connections/{conn_id}/sftp/files")
async def list_sftp_files(
    conn_id: int,
    body: SftpFilesQuery,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """The files a folder + glob takes on this server: the feedback while the
    datasource is being set up, BEFORE importing."""
    conn = _get_connection(session, conn_id)
    ensure_can(session, user, conn.project_id, Capability.CONNECT)
    if conn.db_type != "sftp":
        raise HTTPException(status_code=422, detail="This is not an SFTP connection")
    extra: dict = {"folder": body.folder, "pattern": body.pattern}
    fields = body.template_fields()
    if fields or any(c in body.folder + body.pattern for c in "{}"):
        # placeholders or variables: the engine expands them and lists the values without a file
        extra["source"] = {"folder": body.folder, "pattern": body.pattern, **fields}
    return await _engine_inspect(engine_connection_payload(conn), "files", lingua=lingua_della_richiesta(request),
                                 **extra)


@router.post("/connections/{conn_id}/api/try")
async def try_api_call(
    conn_id: int,
    body: ApiCallSpec,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """«Try the call» of the datasource dialog, BEFORE saving: the first page of the
    first call with the connection's credentials — the request as sent (secrets
    masked), the status, an excerpt of the answer and the first rows. CONNECT on
    the connection's folder: it is a call made with its credentials."""
    conn = _get_connection(session, conn_id)
    ensure_can(session, user, conn.project_id, Capability.CONNECT)
    if conn.db_type != "api":
        raise HTTPException(status_code=422, detail="This is not an API connection")
    return await _engine_inspect(engine_connection_payload(conn), "try", source=body.model_dump())


@router.post("/connections/{conn_id}/sharepoint/files")
async def list_sharepoint_files(
    conn_id: int,
    body: SharePointFilesQuery,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """I file (Excel, CSV) che un percorso (anche con glob) prende su questo sito: il
    riscontro mentre si configura la datasource, PRIMA di importare."""
    conn = _get_connection(session, conn_id)
    ensure_can(session, user, conn.project_id, Capability.CONNECT)
    if conn.db_type != "sharepoint":
        raise HTTPException(status_code=422, detail="This is not a SharePoint connection")
    # the format says which files the path takes (Excel, CSV or both)
    return await _engine_inspect(engine_connection_payload(conn), "files", path=body.path,
                                 source={"path": body.path, "format": body.format, **body.template_fields()})


@router.post("/connections/{conn_id}/s3/files")
async def list_s3_files(
    conn_id: int,
    body: S3FilesQuery,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """The objects a folder + glob takes in the bucket (every expansion of its
    placeholders, and the values without a file): «Check the files» of the «S3
    folder» datasource, BEFORE importing. CONNECT on the connection's folder."""
    conn = _get_connection(session, conn_id)
    ensure_can(session, user, conn.project_id, Capability.CONNECT)
    if conn.db_type != "s3":
        raise HTTPException(status_code=422, detail="This is not an S3 connection")
    if not body.bucket.strip() and not (conn.database or "").strip():
        raise HTTPException(status_code=422, detail="No bucket: set one on the datasource or as the connection's default")
    source = {"bucket": body.bucket.strip(), "folder": body.folder.strip().strip("/"), "pattern": body.pattern.strip(),
              **body.template_fields()}
    return await _engine_inspect(engine_connection_payload(conn), "files", source=source)


@router.post("/placeholders/preview")
async def preview_placeholders(
    body: PlaceholderPreviewQuery,
    user: User = Depends(get_current_user),
):
    """The live preview of the placeholders of a form (a datasource's folder and
    glob, an Output's file name, folder, key or subject): each template's value
    now, in its time zone, or the message of what is wrong — the engine's
    checker, the same that validates at save time and before a run. Any signed-in
    user: nothing is read, it is arithmetic on dates."""
    payload = body.model_dump()
    resp = await get_engine_client().post("/placeholders/preview", json=payload)
    if resp.status_code >= 400:
        raise HTTPException(status_code=502 if resp.status_code >= 500 else 422, detail=resp.text[:500])
    return resp.json()


@router.get("/connections/{conn_id}/tables")
async def list_connection_tables(
    conn_id: int,
    request: Request = None,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    conn = _get_connection(session, conn_id)
    ensure_can(session, user, conn.project_id, Capability.CONNECT)
    return await _engine_inspect(engine_connection_payload(conn), "tables", lingua=lingua_della_richiesta(request))

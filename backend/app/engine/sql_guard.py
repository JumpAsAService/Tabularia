"""Il nodo `sql` legge SOLO il proprio input: lista BIANCA delle tabelle.

Sui motori che eseguono la query su un server vero (ClickHouse esterno, chDB,
BigQuery) una lista nera di funzioni pericolose non basta, ed è stato dimostrato
dal vivo (2026-09-19): `merge('system', '^query_log$')` non era nell'elenco e ha
letto un'altra tabella del server passando dal nodo; `information_schema` elencava
le tabelle. Ogni versione del server aggiunge table function, quindi l'elenco dei
divieti è sempre in ritardo. Qui si rovescia la domanda: non «cosa è vietato» ma
«cosa è ammesso», cioè `self` e `input` — e nient'altro.

Perché conta per l'RBAC: l'engine gira con credenziali che leggono tutto. Il
gateway decide QUALE datasource un utente può aprire, ma la query libera gira poi
sul server con quelle credenziali: se potesse nominare un'altra tabella, il
permesso sulla datasource sarebbe decorativo. Vale per l'editor dei flussi e, dal
2026-09-19, per l'assistente AI, che mette una query libera in mano a chiunque
abbia VIEW.

DuckDB e Polars non passano da qui: DuckDB esegue il nodo in una connessione in
memoria con l'accesso esterno spento e bloccato, Polars conosce solo il frame
registrato. Lì non c'è un server da cui leggere altro.
"""
from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.scope import traverse_scope

from app.engine.exceptions import EngineError

INPUT_NAMES = frozenset({"self", "input"})

# table function che generano righe dal nulla: non leggono niente, restano ammesse
HARMLESS_TABLE_FUNCTIONS = frozenset({
    "numbers", "numbers_mt", "zeros", "zeros_mt", "generateseries", "generate_series",
    "values", "unnest", "generate_array", "generate_date_array", "generate_timestamp_array",
})

# funzioni SCALARI che leggono altro dal server (dizionari, tabelle Join,
# impostazioni e macro): non compaiono come tabelle, vanno nominate
_FORBIDDEN_SCALARS = (
    "dictget", "dicthas", "dictisin", "joinget", "getsetting", "getserversetting",
    "getmacro", "hascolumnintable", "getclienthttpheader",
)

_REFUSAL = (
    "sql: la query può leggere solo l'input del nodo (`self` o `input`). "
    "{what} non è ammesso: per unire altri dati usa un nodo Join o Union."
)


def _function_name(node: exp.Expression) -> str:
    name = getattr(node, "name", "") or ""
    return (name or node.key or "").lower()


def ensure_reads_only_input(query: str, dialect: str) -> None:
    """Solleva EngineError se la query nomina una tabella che non è il suo input.

    Una query che sqlglot non sa analizzare viene RIFIUTATA: far passare ciò che
    non si è capito riaprirebbe il buco che questo modulo chiude."""
    try:
        tree = sqlglot.parse_one(query, read=dialect)
    except SqlglotError as e:
        raise EngineError(
            "sql: query non analizzabile, quindi non eseguita (il nodo SQL verifica che si legga "
            f"solo l'input). Semplificala o usa i nodi dedicati. Dettaglio: {str(e)[:200]}"
        ) from e
    if tree is None:
        raise EngineError("sql: la query è vuota")

    # 1) le sorgenti VERE di ogni scope: una CTE o una sottoquery sono uno Scope,
    #    una tabella del server è una exp.Table. Guardare lo scope (e non solo i
    #    nomi delle CTE) impedisce di definire una CTE `x` in una sottoquery e poi
    #    leggere la tabella vera `x` fuori da lì.
    real: set[int] = set()
    try:
        for scope in traverse_scope(tree):
            for source in scope.sources.values():
                if isinstance(source, exp.Table):
                    real.add(id(source))
                    _check_table(source)
    except SqlglotError as e:
        raise EngineError(f"sql: query non analizzabile, quindi non eseguita. Dettaglio: {str(e)[:200]}") from e

    # nomi delle CTE: sono sorgenti legittime ovunque nella query
    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}

    # 2) rete di sicurezza: ogni exp.Table che l'analisi degli scope non avesse
    #    visitato (costrutti del dialetto che non conosce) passa lo stesso
    #    controllo, con l'unica tolleranza dei nomi di CTE
    for table in tree.find_all(exp.Table):
        if id(table) in real:
            continue
        if isinstance(table.this, exp.Identifier) and not table.db and not table.catalog \
                and table.name.lower() in cte_names:
            continue
        _check_table(table)

    # 3) Tutto ciò che sta in posizione FROM/JOIN deve essere una sorgente che
    #    l'analisi ha riconosciuto. NON ci si può fidare del TIPO di nodo: nel
    #    dialetto ClickHouse sqlglot analizza `db.tabella` dentro la sottoquery
    #    di un ARRAY JOIN come exp.Column, non exp.Table — quindi né
    #    `traverse_scope` né `find_all(exp.Table)` la vedono, e la lista bianca
    #    guardava nel posto sbagliato. Trovato dall'audit 2026-09-19 (A9), dopo
    #    che la stessa classe di problema era gia' costata l'incidente merge().
    #    Qui si guarda la POSIZIONE, che il dialetto non può falsare.
    for ramo in tree.find_all(exp.From, exp.Join):
        _check_source(ramo.this, cte_names)

    # 4) ClickHouse: `x IN nome_tabella` legge una tabella senza un FROM
    for node in tree.find_all(exp.In):
        if node.args.get("field") is not None:
            raise EngineError(_REFUSAL.format(what=f"`IN {node.args['field'].sql()}`"))

    # 5) funzioni scalari che leggono dal server
    for func in tree.find_all(exp.Func):
        name = _function_name(func)
        if name.startswith(_FORBIDDEN_SCALARS):
            raise EngineError(_REFUSAL.format(what=f"la funzione `{name}`"))


def _check_source(nodo: exp.Expression | None, cte_names: set[str]) -> None:
    """Valida ciò che compare come sorgente di un FROM o di un JOIN.

    Si guarda la POSIZIONE e non il tipo di nodo, perché il tipo non è
    affidabile: dentro la sottoquery di un ARRAY JOIN sqlglot classifica come
    `exp.Column` sia `self` sia `information_schema.tables`. Il discrimine vero
    è il QUALIFICATORE — un nome puntato in posizione di sorgente è sempre la
    tabella di qualcun altro."""
    if nodo is None:
        return
    if isinstance(nodo, exp.Table):
        if isinstance(nodo.this, exp.Identifier) and not nodo.db and not nodo.catalog \
                and nodo.name.lower() in cte_names:
            return  # una CTE è una sorgente legittima
        _check_table(nodo)
        return
    if isinstance(nodo, (exp.Subquery, exp.Unnest, exp.Values, exp.Lateral)):
        return  # il loro contenuto è già passato dagli scope
    if isinstance(nodo, exp.Column):
        # `self`/`input` e le CTE sono nomi NUDI; tutto ciò che è qualificato
        # (`db.tabella`) in posizione di sorgente è una tabella estranea
        if not nodo.table and nodo.name.lower() in (INPUT_NAMES | cte_names):
            return
        raise EngineError(_REFUSAL.format(what=f"la tabella `{nodo.sql()[:80]}`"))
    if isinstance(nodo, exp.Dot):
        raise EngineError(_REFUSAL.format(what=f"il riferimento `{nodo.sql()[:80]}`"))


def _check_table(table: exp.Table) -> None:
    if not isinstance(table.this, exp.Identifier):
        name = _function_name(table.this)
        if name in HARMLESS_TABLE_FUNCTIONS:
            return
        raise EngineError(_REFUSAL.format(what=f"la table function `{name or table.this.sql()[:40]}`"))
    if table.db or table.catalog or table.name.lower() not in INPUT_NAMES:
        raise EngineError(_REFUSAL.format(what=f"la tabella `{table.sql()[:80]}`"))

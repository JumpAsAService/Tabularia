"""Valutatore dei data contracts: un parquet e un contratto in ingresso, un
referto regola per regola in uscita.

UNO solo, su Polars, qualunque motore abbia prodotto i dati: l'output di ogni
motore è un parquet nello storage, e valutarlo lì tiene i cinque motori fuori da
questa storia (e il risultato non dipende da quale si è usato).

Come lavora:
  1. ogni regola diventa un'espressione che CONTA le righe che la violano;
  2. ogni espressione viene prima provata sullo schema, senza leggere dati: una
     regola che non si può valutare (colonna che non c'è, tipo che non c'entra,
     SQL che non compila) NON regge, e dice perché — un contratto non passa mai
     perché non lo si è potuto controllare;
  3. tutti i conteggi si calcolano in UNA passata sul parquet;
  4. solo per le regole violate si torna a leggere, per portare qualche valore
     di esempio.

Il referto è fatto di DATI (quante violazioni, che cosa si è osservato), non di
frasi: la frase la costruisce l'interfaccia, nella lingua di chi guarda.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

import polars as pl

SEVERITIES = ("warning", "error")
MAX_SAMPLES = 5          # valori di esempio per regola violata
MAX_SAMPLE_CHARS = 80    # ognuno tagliato qui
MAX_SAMPLED_RULES = 25   # ogni esempio è un'altra lettura: oltre, si riportano solo i conteggi
ENUM_MAX_DISTINCT = 12   # la proposta suggerisce «valori ammessi» fino a tanti valori distinti


# ── tipi ─────────────────────────────────────────────────────────────────────
def family(dtype: pl.DataType) -> str:
    """La famiglia di un tipo, cioè ciò che un contratto promette: non «Int32»
    (un dettaglio del motore, che cambia da un motore all'altro) ma «intero»."""
    if dtype.is_integer():
        return "integer"
    if dtype.is_numeric():
        return "number"
    if dtype == pl.Boolean:
        return "boolean"
    if dtype == pl.Date:
        return "date"
    if isinstance(dtype, pl.Datetime):
        return "datetime"
    if dtype in (pl.String, pl.Categorical, pl.Enum):
        return "string"
    return "other"


def _fits(actual: str, wanted: str) -> bool:
    # un intero è anche un numero: chi promette «numero» accetta entrambi
    return actual == wanted or (wanted == "number" and actual == "integer")


# ── una regola pronta da valutare ────────────────────────────────────────────
@dataclass
class Check:
    """Come si valuta una regola. O è già decisa (`fixed`), o è un conteggio da
    fare sui dati (`violations`), con la maschera delle righe che violano e la
    colonna da cui prendere gli esempi."""
    fixed: dict | None = None            # {passed, ...} deciso senza contare
    violations: pl.Expr | None = None    # quante righe violano
    offending: pl.Expr | None = None     # quali (per gli esempi)
    sample_of: pl.Expr | None = None     # che cosa mostrare di quelle righe
    observe: pl.Expr | None = None       # un valore osservato da riportare (es. il più recente)
    decide: Callable[[Any], dict] | None = None  # da `observe` all'esito


class RuleError(ValueError):
    """La regola non si può valutare su questi dati (o è scritta male)."""


_BUILDERS: dict[str, Callable[[dict, "Ctx"], Check]] = {}


def rule(kind: str):
    def deco(fn):
        _BUILDERS[kind] = fn
        return fn
    return deco


@dataclass
class Ctx:
    schema: dict[str, pl.DataType]
    now: datetime
    snapshot_at: datetime | None

    def col(self, r: dict, key: str = "column") -> str:
        name = r.get(key)
        if not name or not isinstance(name, str):
            raise RuleError("the rule names no column")
        if name not in self.schema:
            raise RuleError(f"column '{name}' does not exist")
        return name


def _literal(value: Any, dtype: pl.DataType) -> Any:
    """Un estremo di intervallo scritto nel contratto (JSON) nel tipo della colonna."""
    fam = family(dtype)
    try:
        if fam == "date":
            return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
        if fam == "datetime":
            v = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return v.astimezone(timezone.utc).replace(tzinfo=None) if v.tzinfo else v
        if fam in ("integer", "number"):
            return float(value) if not isinstance(value, (int, float)) else value
    except (TypeError, ValueError) as e:
        raise RuleError(f"'{value}' is not a valid {fam}") from e
    return value


@rule("column")
def _column(r: dict, ctx: Ctx) -> Check:
    """La colonna esiste e, se è detto, è della famiglia promessa."""
    name = r.get("column")
    if not name or name not in ctx.schema:
        return Check(fixed={"passed": False, "observed": None, "expected": r.get("dtype") or "present"})
    actual = family(ctx.schema[name])
    wanted = r.get("dtype")
    return Check(fixed={"passed": not wanted or _fits(actual, wanted), "observed": actual, "expected": wanted or "present"})


@rule("not_null")
def _not_null(r: dict, ctx: Ctx) -> Check:
    c = pl.col(ctx.col(r))
    return Check(violations=c.is_null().sum(), offending=c.is_null())


@rule("unique")
def _unique(r: dict, ctx: Ctx) -> Check:
    names = r.get("columns") or ([r["column"]] if r.get("column") else [])
    if not names:
        raise RuleError("the rule names no column")
    for n in names:
        ctx.col({"column": n})
    key = pl.col(names[0]) if len(names) == 1 else pl.struct(names)
    dup = key.is_duplicated()
    return Check(violations=dup.sum(), offending=dup, sample_of=key)


@rule("accepted_values")
def _accepted_values(r: dict, ctx: Ctx) -> Check:
    name = ctx.col(r)
    values = r.get("values")
    if not isinstance(values, list) or not values:
        raise RuleError("the list of accepted values is empty")
    c = pl.col(name)
    fam = family(ctx.schema[name])
    # Il contratto è JSON e chi lo scrive non conosce il tipo fisico: «3» deve
    # combaciare con 3, e 3 con 3.0. Quindi i valori si portano nel tipo della
    # colonna dove un tipo c'è; altrimenti si confronta sulla forma scritta.
    inside = None
    if fam in ("integer", "number"):
        try:
            inside = c.cast(pl.Float64).is_in([float(v) for v in values if not isinstance(v, bool)])
        except (TypeError, ValueError):
            pass  # un valore che non è un numero: confronto sul testo
    elif fam == "boolean":
        veri = {True: True, False: False, "true": True, "false": False, "1": True, "0": False, 1: True, 0: False}
        try:
            inside = c.is_in([veri[v.strip().lower() if isinstance(v, str) else v] for v in values])
        except KeyError as e:
            raise RuleError(f"'{e.args[0]}' is not a boolean") from e
    if inside is None:
        inside = c.cast(pl.String).is_in([str(v) for v in values])
    bad = c.is_not_null() & ~inside
    return Check(violations=bad.sum(), offending=bad, sample_of=c)


@rule("range")
def _range(r: dict, ctx: Ctx) -> Check:
    name = ctx.col(r)
    dtype = ctx.schema[name]
    if family(dtype) not in ("integer", "number", "date", "datetime"):
        raise RuleError(f"a range needs a number or a date, '{name}' is {family(dtype)}")
    lo, hi = r.get("min"), r.get("max")
    if lo is None and hi is None:
        raise RuleError("the range has neither a minimum nor a maximum")
    c = pl.col(name)
    bad = pl.lit(False)
    if lo is not None:
        bad = bad | (c < _literal(lo, dtype))
    if hi is not None:
        bad = bad | (c > _literal(hi, dtype))
    bad = c.is_not_null() & bad
    return Check(violations=bad.sum(), offending=bad, sample_of=c)


@rule("pattern")
def _pattern(r: dict, ctx: Ctx) -> Check:
    name = ctx.col(r)
    regex = r.get("regex")
    if not regex or not isinstance(regex, str):
        raise RuleError("the pattern is empty")
    try:
        re.compile(regex)
    except re.error as e:
        raise RuleError(f"invalid pattern: {e}") from e
    c = pl.col(name)
    # per INTERO: «^[A-Z]+» senza ancora finale accetterebbe «ABC-qualunque-cosa»
    bad = c.is_not_null() & ~c.cast(pl.String).str.contains(f"^(?:{regex})$")
    return Check(violations=bad.sum(), offending=bad, sample_of=c)


@rule("row_count")
def _row_count(r: dict, ctx: Ctx) -> Check:
    lo, hi = r.get("min"), r.get("max")
    if lo is None and hi is None:
        raise RuleError("the row count has neither a minimum nor a maximum")

    def decide(n: Any) -> dict:
        ok = (lo is None or n >= lo) and (hi is None or n <= hi)
        return {"passed": ok, "observed": n, "expected": {"min": lo, "max": hi}}

    return Check(observe=pl.len(), decide=decide)


@rule("freshness")
def _freshness(r: dict, ctx: Ctx) -> Check:
    hours = r.get("max_age_hours")
    if not isinstance(hours, (int, float)) or isinstance(hours, bool) or hours <= 0:
        raise RuleError("the maximum age must be a positive number of hours")
    limit = ctx.now - timedelta(hours=hours)

    def esito(newest: datetime | None) -> dict:
        age = None if newest is None else round((ctx.now - newest).total_seconds() / 3600, 2)
        return {"passed": newest is not None and newest >= limit, "observed": age, "expected": hours}

    if not r.get("column"):
        # età dello snapshot: quando i dati sono stati prodotti
        return Check(fixed=esito(ctx.snapshot_at or ctx.now))
    name = ctx.col(r)
    fam = family(ctx.schema[name])
    if fam not in ("date", "datetime"):
        raise RuleError(f"freshness needs a date or a datetime, '{name}' is {fam}")

    def decide(newest: Any) -> dict:
        if isinstance(newest, date) and not isinstance(newest, datetime):
            newest = datetime(newest.year, newest.month, newest.day, 23, 59, 59)
        return esito(newest)

    return Check(observe=pl.col(name).max(), decide=decide)


@rule("expression")
def _expression(r: dict, ctx: Ctx) -> Check:
    sql = r.get("sql")
    if not sql or not isinstance(sql, str):
        raise RuleError("the expression is empty")
    try:
        expr = pl.sql_expr(sql)  # solo espressioni: niente statement, niente tabelle
    except Exception as e:
        raise RuleError(f"invalid expression: {e}") from e
    # un esito «non lo so» (null) non è un sì: la riga viola
    bad = ~expr.cast(pl.Boolean).fill_null(False)
    return Check(violations=bad.sum(), offending=bad)


# ── valutazione ──────────────────────────────────────────────────────────────
def _cut(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float)):
        return v
    s = str(v)
    return s if len(s) <= MAX_SAMPLE_CHARS else s[: MAX_SAMPLE_CHARS - 1] + "…"


def _plain(v: Any) -> Any:
    """Un valore osservato in forma JSON."""
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    return v


def _naive_utc(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d.astimezone(timezone.utc).replace(tzinfo=None) if d.tzinfo else d


def evaluate(
    lf: pl.LazyFrame, document: dict, *, now: datetime | None = None, snapshot_at: datetime | None = None,
    samples: bool = True,
) -> dict:
    """Il referto del contratto `document` sui dati `lf`.

    `snapshot_at`: quando i dati sono stati prodotti (per la freschezza dello
    snapshot); assente = adesso, cioè dati appena scritti."""
    ctx = Ctx(schema=dict(lf.collect_schema()), now=_naive_utc(now) or datetime.now(timezone.utc).replace(tzinfo=None),
              snapshot_at=_naive_utc(snapshot_at))
    rules = [r for r in (document.get("rules") or []) if isinstance(r, dict)]
    results: dict[int, dict] = {}
    checks: dict[int, Check] = {}

    for i, r in enumerate(rules):
        base = {"id": r.get("id"), "kind": r.get("kind"), "severity": r.get("severity")}
        # su che cosa era la regola: chi legge il referto non deve avere il contratto sotto mano
        base.update({k: r[k] for k in ("column", "columns", "name") if r.get(k)})
        builder = _BUILDERS.get(r.get("kind"))
        try:
            if r.get("severity") not in SEVERITIES:
                raise RuleError(f"severity must be one of {SEVERITIES}")
            if builder is None:
                raise RuleError(f"unknown rule kind '{r.get('kind')}'")
            check = builder(r, ctx)
            # la prova sullo schema: niente dati letti, ma tipi e nomi sì
            exprs = [e for e in (check.violations, check.observe) if e is not None]
            if exprs:
                lf.select(exprs).collect_schema()
        except RuleError as e:
            results[i] = {**base, "passed": False, "error": str(e)}
            continue
        except Exception as e:  # Polars: tipo che non c'entra, colonna usata dall'espressione che non c'è…
            results[i] = {**base, "passed": False, "error": str(e).splitlines()[0][:300]}
            continue
        if check.fixed is not None:
            results[i] = {**base, **check.fixed}
        else:
            checks[i] = check
            results[i] = base

    # UNA passata per tutti i conteggi
    rows = None
    aggregates = [pl.len().alias("__rows")]
    for i, c in checks.items():
        aggregates.append((c.violations if c.violations is not None else c.observe).alias(f"r{i}"))
    measured = lf.select(aggregates).collect().row(0, named=True)
    rows = int(measured["__rows"])
    for i, c in checks.items():
        value = measured[f"r{i}"]
        if c.violations is not None:
            n = int(value or 0)
            results[i].update(passed=n == 0, violations=n)
        else:
            results[i].update(c.decide(value))
            results[i]["observed"] = _plain(results[i].get("observed"))

    # qualche esempio, solo dove serve
    if samples:
        failed = [i for i, c in checks.items() if c.offending is not None and not results[i]["passed"]]
        for i in failed[:MAX_SAMPLED_RULES]:
            c = checks[i]
            what = c.sample_of if c.sample_of is not None else (pl.col(rules[i]["column"]) if rules[i].get("column") else None)
            if what is None or rules[i].get("kind") == "not_null":
                continue  # di un null non c'è niente da mostrare
            try:
                got = lf.filter(c.offending).select(what.alias("v")).unique().head(MAX_SAMPLES).collect()
                results[i]["sample"] = [_cut(_plain(v)) for v in got["v"].to_list()]
            except Exception:
                pass  # l'esempio è un di più: il conteggio c'è già

    ordered = [results[i] for i in range(len(rules))]
    broken = [x for x in ordered if not x["passed"]]
    outcome = "failed" if any(x["severity"] == "error" for x in broken) else "warning" if broken else "passed"
    # una regola scritta male (severità assente) conta come errore: non deve passare in silenzio
    if any(x["severity"] not in SEVERITIES for x in broken):
        outcome = "failed"
    return {
        "outcome": outcome,
        "version": document.get("version"),  # di quale versione del contratto è questo referto
        "rows": rows,
        "errors": sum(1 for x in broken if x["severity"] != "warning"),
        "warnings": sum(1 for x in broken if x["severity"] == "warning"),
        "rules": ordered,
    }


# ── profilo e proposta ───────────────────────────────────────────────────────
def profile(lf: pl.LazyFrame) -> dict:
    """Che cosa c'è nei dati, colonna per colonna: la base per PROPORRE un contratto."""
    schema = dict(lf.collect_schema())
    names = list(schema)
    aggs = [pl.len().alias("__rows")]
    for i, n in enumerate(names):
        c = pl.col(n)
        aggs += [c.null_count().alias(f"n{i}"), c.n_unique().alias(f"d{i}")]
        if family(schema[n]) in ("integer", "number", "date", "datetime"):
            aggs += [c.min().alias(f"lo{i}"), c.max().alias(f"hi{i}")]
    row = lf.select(aggs).collect().row(0, named=True)
    rows = int(row["__rows"])
    columns = []
    for i, n in enumerate(names):
        fam = family(schema[n])
        col = {"name": n, "dtype": str(schema[n]), "family": fam, "nulls": int(row[f"n{i}"]), "distinct": int(row[f"d{i}"])}
        if f"lo{i}" in row:
            col["min"], col["max"] = _plain(row[f"lo{i}"]), _plain(row[f"hi{i}"])
        # i pochi valori distinti di un testo sono candidati a «valori ammessi»; di
        # un booleano no: che siano vero e falso lo dice già il tipo
        if fam == "string" and 0 < col["distinct"] <= ENUM_MAX_DISTINCT and rows > col["distinct"] * 2:
            vals = lf.select(pl.col(n)).unique().drop_nulls().head(ENUM_MAX_DISTINCT).collect()[n].to_list()
            col["values"] = sorted(_plain(v) for v in vals)
        columns.append(col)
    return {"rows": rows, "columns": columns}


def propose(prof: dict) -> dict:
    """Un contratto di partenza dal profilo dei dati di OGGI, da correggere a mano.

    Prudente apposta: promette ciò che è strutturale (le colonne e i loro tipi,
    che ci sia almeno una riga) come ERRORE, e ciò che è solo vero oggi (nessun
    null, nessun doppione, pochi valori distinti) come AVVISO — una proposta
    troppo severa fermerebbe il prossimo aggiornamento per un caso mai visto."""
    rules: list[dict] = []
    rows = prof.get("rows") or 0

    def add(kind: str, severity: str, **fields):
        rules.append({"id": f"r{len(rules) + 1}", "kind": kind, "severity": severity, **fields})

    for c in prof.get("columns", []):
        fam = c.get("family")
        add("column", "error", column=c["name"], **({"dtype": fam} if fam and fam != "other" else {}))
        if rows and c.get("nulls") == 0:
            add("not_null", "warning", column=c["name"])
        if rows > 1 and c.get("distinct") == rows and c.get("nulls") == 0 and fam in ("integer", "string"):
            add("unique", "warning", columns=[c["name"]])
        if c.get("values"):
            add("accepted_values", "warning", column=c["name"], values=c["values"])
    if rows:
        add("row_count", "error", min=1)
    return {"description": "", "rules": rules}

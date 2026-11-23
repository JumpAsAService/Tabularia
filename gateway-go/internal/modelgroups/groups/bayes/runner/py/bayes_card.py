"""The card of a run (docs/specs/bayes-contract.md, «The card»): `model.json`, the common keys of `tab_card.card` and the object `bayes`
(`bayescontract.Card` is its Go twin), and `MLmodel` when the node logs to MLflow.

The card holds NO DRAW, ever (answer 14): `summary` is the scalar quantities' summary the diagnostics hand over, `units` the labels
of each plate. When the card would pass `max_card_bytes` (1 MiB) the summary is cut first, then the units' labels (their counts stay);
if it is still too large the existing rule applies: the note `note_card_too_large`, no card, no registration.
`card.schema.json` does not know the object `bayes` yet (lot K0b gives it to the group's own view of the schema).
"""
from __future__ import annotations

from typing import Any

import tab_card
from tab_protocol import artifact, write_text
from bayes_data import Facts
from bayes_task import Layout, Task
from bayes_rows import ResultsWriter

# a mapping entry's kind as the summary and the card say it («effect» for a component of correlated effects)
_CARD_KIND = {"parameter": "parameter", "transformed": "transformed", "linear_predictor": "transformed", "state": "state",
              "component": "effect", "correlation": "correlation"}


def summary_kind(mapping_kind: str) -> str:
    return _CARD_KIND.get(mapping_kind, "transformed")


def inputs(task: Task, facts: Facts) -> list[dict[str, Any]]:
    """The bound columns as `tab_card` lists them: a number, or a category with its levels (a category's reference first)."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for e in task.data:
        column = e.get("column")
        if not column or column in seen:
            continue
        seen.add(column)
        if e["kind"] in ("real", "int"):
            out.append({"name": column, "type": "number"})
        elif e["kind"] == "group_index":
            out.append({"name": column, "type": "category", "levels": list(facts.units.get(e["target"], []))})
        elif e["kind"] == "category":
            levels = list(facts.units.get("levels:" + e["node"], []))
            out.append({"name": column, "type": "category", "levels": levels, **({"reference": levels[0]} if levels else {})})
    return out


def build(task: Task, facts: Facts, layout: Layout, sampler_facts: dict[str, Any] | None = None, summary: list[dict[str, Any]] | None = None,
          program_bytes: int = 0, image: str = "") -> dict[str, Any]:
    """The card: `sampler_facts` = {"divergences", "treedepth_hits", "e_bfmi_min"} (the follower's accumulators), `summary` the scalar
    quantities' rows ({"quantity", "mean", "sd", "q5", "q50", "q95", "rhat", "ess_bulk", "ess_tail"}), `program_bytes` the program's size
    and `image` the image's digest (the Job knows them, the task does not)."""
    c = tab_card.card(task.raw, inputs(task, facts), packages=("numpy", "pyarrow"))
    m, s = task.model, task.sampler or {}
    f = sampler_facts or {}
    by_stan = {e.stan for e in layout.entries}
    mapping = []
    for e in task.mapping:
        if e.get("role") == "quantity" and e["stan"] in by_stan:
            mapping.append({"stan": e["stan"], "node_id": e.get("node_id") or "", "name": e.get("name"), "label": e.get("label"),
                            "kind": summary_kind(e.get("kind") or "")})
    data = []
    for e in task.data:
        if e.get("kind") in ("real", "int") and e.get("column"):
            std = facts.standardized.get(e["stan"])
            item = {"stan": e["stan"], "column": e["column"], "standardized": bool(std)}
            if std:
                item["mean"], item["sd"] = std["mean"], std["sd"]
            data.append(item)
    plates = {p["id"] for p in task.plates if p.get("kind") == "group"}
    c["bayes"] = {
        "model": {"id": m.get("id"), "version": m.get("version"), "semantic_hash": m.get("semantic_hash"),
                  "codegen_version": m.get("codegen_version")},
        "program": {"sha256": m.get("program_sha256"), "bytes": int(program_bytes)},
        "binary": {"sha256": m.get("binary_sha256"), "cmdstan": m.get("cmdstan"), "image": image},
        "mapping": mapping, "data": data,
        "units": {p: {"count": len(u), "labels": list(u)} for p, u in facts.units.items() if p in plates},
        "sampler": {"algorithm": task.algorithm, "chains": s.get("chains"), "warmup": s.get("warmup"), "draws": s.get("draws"),
                    "seed": s.get("seed"), "adapt_delta": s.get("adapt_delta"), "max_treedepth": s.get("max_treedepth")},
        "facts": {"divergences": f.get("divergences", 0), "treedepth_hits": f.get("treedepth_hits", 0),
                  "e_bfmi_min": f.get("e_bfmi_min")},
        "summary": list(summary or []),
    }
    return c


def fit(task: Task, c: dict[str, Any], writer: ResultsWriter | None = None) -> str | None:
    """The card's text when it fits `max_card_bytes`, after cutting the summary, then the units' labels (their counts stay); None
    otherwise, with the note `note_card_too_large` in the results (the models spec's rule: the run is complete, nothing is registered)."""
    most = task.limit("max_card_bytes")

    def size() -> int:
        return len(tab_card.card_text(c).encode("utf-8"))

    if size() > most:
        c["bayes"]["summary"] = []
    if size() > most:
        for u in c["bayes"]["units"].values():
            u["labels"] = []
    if size() <= most:
        return tab_card.card_text(c)
    if writer is not None:
        writer.note("card_too_large", tab_card.NOTE_CARD_TOO_LARGE)
    return None


def write(task: Task, c: dict[str, Any], text: str, run_id: str = "") -> None:
    """model.json, and MLmodel when the node logs to MLflow, under TABULARIA_ARTIFACTS."""
    write_text(artifact("model.json"), text)
    if task.raw.get("mlflow"):
        write_text(artifact("MLmodel"), tab_card.mlmodel(c, run_id))

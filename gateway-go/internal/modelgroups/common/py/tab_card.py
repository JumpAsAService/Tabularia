"""The model card (the twin of common/R/card.R; card.schema.json): what applying the model to new
rows needs, as data. A runner starts from card(task, inputs) and adds what its model needs (the
boosting group: the `boosting` object, with its model file's name, size and sha256); tab_main
writes model.json, and MLmodel and the model file when the node logs to MLflow, unless the card
is larger than max_card_bytes or the model file than max_model_file_bytes (card_fits,
model_file_fits: a note, and no model files). The model file is the library's own format
(XGBoost's JSON, CatBoost's cbm), data the library parses: never pickle or any format that runs
code when read.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import platform
import warnings
from importlib import metadata
from typing import Any, Iterable

from tab_protocol import limit, to_json
from tab_results import Rows, note

CARD_FORMAT = "tabularia-model"
CARD_VERSION = 1

# The notes of a fit whose card or model file is too large. Neither stops the fit: the step
# gives its results, its rows and its report; only the model is not logged to MLflow (no
# model.json, MLmodel or model file), so no Predict node can apply it. The sentences have no
# number or name in them: report_texts/common.json says them in the page's language.
NOTE_CARD_TOO_LARGE = (
    "The model card would be larger than a card can be (1 MiB): the results and the report are complete, but the model "
    "is not logged to MLflow and no Predict node can apply it.")
NOTE_MODEL_FILE_TOO_LARGE = (
    "The model file would be larger than a model file can be (64 MiB): the results and the report are complete, but the "
    "model is not logged to MLflow and no Predict node can apply it.")


def versions(packages: Iterable[str] = ()) -> dict[str, str]:
    """Python and the distributions a model used: {"Python": "3.14.0", "xgboost-cpu": "3.4.1"}."""
    v = {"Python": platform.python_version()}
    for p in packages:
        try:
            v[p] = metadata.version(p)
        except metadata.PackageNotFoundError:
            v[p] = "not installed"
    return v


def software(packages: Iterable[str] = ()) -> str:
    """«Python 3.14.0; xgboost-cpu 3.4.1; numpy 2.5.5»."""
    return "; ".join(f"{k} {v}" for k, v in versions(packages).items())


def card(task: dict[str, Any], inputs: list[dict[str, Any]] | None = None, packages: Iterable[str] = ()) -> dict[str, Any]:
    """The keys every card has. `inputs`: the columns Predict needs, in the order of the
    positional names (v1, v2…): {"name", "type": number | category | time, "levels"}."""
    gv = task.get("group_version")
    return {
        "format": CARD_FORMAT, "format_version": CARD_VERSION, "model": task.get("model"), "variant": task.get("variant"),
        "group": task.get("group"), "group_version": "dev" if gv is None else gv,
        "recipe_hash": task.get("recipe_hash") or "", "runner_hash": task.get("runner_hash") or "",
        "software": versions(packages),
        "created_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "params": dict(task.get("params") or {}), "inputs": list(inputs or []),
    }


def card_text(c: dict[str, Any]) -> str:
    return to_json(c)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def model_file_entry(name: str, fmt: str, data: bytes) -> dict[str, Any]:
    """The card's description of a model file, from its bytes: {"name", "format", "bytes",
    "sha256"} (the boosting object's `model_file`)."""
    return {"name": name, "format": fmt, "bytes": len(data), "sha256": sha256(data)}


def card_fits(task: dict[str, Any], c: dict[str, Any], rows: Rows) -> str | None:
    """The card's text when it is not larger than max_card_bytes; else None, with the note
    note_card_too_large in the rows and a warning for run.json."""
    text = card_text(c)
    size = len(text.encode("utf-8"))
    most = int(limit(task, "max_card_bytes"))
    if size <= most:
        return text
    note(rows, "card_too_large", NOTE_CARD_TOO_LARGE)
    warnings.warn(f"the model card would be {size} bytes (at most {most}): it is not written, and the model is not logged to MLflow")
    return None


def model_file_fits(task: dict[str, Any], data: bytes, rows: Rows) -> bool:
    """Whether the model file is not larger than max_model_file_bytes; else the note
    note_model_file_too_large in the rows and a warning for run.json."""
    most = int(limit(task, "max_model_file_bytes"))
    if len(data) <= most:
        return True
    note(rows, "model_file_too_large", NOTE_MODEL_FILE_TOO_LARGE)
    warnings.warn(f"the model file would be {len(data)} bytes (at most {most}): it is not written, and the model is not "
                  "logged to MLflow")
    return False


def mlmodel(c: dict[str, Any], run_id: str = "") -> str:
    """MLflow's MLmodel, written by hand (no mlflow package in the images): a `tabularia` flavor
    naming model.json (and the model file, when the card has one), and the inputs' signature in
    MLflow's format."""
    type_of = {"number": "double", "category": "string", "time": "datetime"}
    signature = to_json([{"name": i["name"], "type": type_of[i["type"]]} for i in c.get("inputs", [])])
    model_file = (c.get("boosting") or {}).get("model_file") or {}
    lines = [
        "artifact_path: model",
        "flavors:",
        "  tabularia:",
        "    card: model.json",
        f"    format: {CARD_FORMAT}",
        f"    format_version: {CARD_VERSION}",
        f"    model: {c['model']}",
        f"    group: {c['group']}",
    ]
    if model_file:
        lines += [f"    model_file: {model_file['name']}", f"    model_file_sha256: {model_file['sha256']}"]
    if run_id:
        lines.append(f"run_id: {run_id}")
    lines += [
        "signature:",
        "  inputs: '" + signature.replace("'", "''") + "'",
        "utc_time_created: '" + c["created_at"].removesuffix("Z").replace("T", " ") + "'",
    ]
    return "\n".join(lines) + "\n"

# Spec: dbt export, piece 4 — AI, optional, in the export dialog

Status: approved, 2026-10-10. Decisions marked **(owner)**; the rest are assumptions.

> **Changed after the bug hunt of 2026-10-15/16** — see the section at the end. Where this spec and
> [docs/dbt-export.md](../dbt-export.md) disagree, the docs describe the code.

## Objective

The project the data team receives should explain itself and should not stop at a
query sqlglot cannot translate. **(owner)** Two AI helps, both optional, both chosen
in the export dialog: descriptions where they are missing, and a translation proposal
where sqlglot fails. **(owner)** The checks on what the AI writes are done by
Tabularia's own code, not by the AI.

## Decisions

- **(owner) Memory per version.** What the AI writes is stored in Tabularia per flow
  version; re-exporting the same version reuses it, so the export stays deterministic.
  A new gateway table, `dbt_ai_texts` (flow, version, kind, key, text, model).
- **(owner) English**, like every other text of the project.
- **(owner) Structure and a few values.** The AI sees column names and types, the
  steps of the flow, the descriptions already written, and at most 3 distinct values
  per column (each cut at 40 characters) read from the datasource. Nothing else.
- Admins only, like the export. Shown only when the assistant is configured and a
  model is enabled: the default model if enabled, otherwise the first enabled one.
- Spending goes through the assistant's accounting (`ai_spend`) and its daily caps; a
  cap reached or a provider down → the export answers with the reason, it does not
  silently drop the AI part.

## 1. Descriptions where missing

Every flow gets a paragraph "what the flow does" (in the README and after the
description of its outputs) — it is the second of the two helps the owner approved,
not a missing description. Only where they have none: the columns of the output
models, of the source tables (`sources.yml`), of the seeds. Marked `(AI)` at the start
of the text; the README lists that the descriptions marked so were written by AI.
Texts are cut at 400 characters and lose `{{ }}`, `{% %}`, `{# #}` (dbt renders
descriptions as Jinja). One call per export, structured output; items not returned
stay without a description.

## 2. Translation proposal

When the engine cannot translate a model (`translation_failed`) or a datasource query
on ClickHouse (`query_not_translatable`), it returns the source SQL, the dialects and
the expected output columns. The gateway asks the AI for the translation (stored per
version) and sends the export again with it. The engine accepts it only if:
one `SELECT` in the target dialect; tables only among those the model may read
(the placeholders of its sources and models, its CTEs; on ClickHouse the `tab_src_*`
databases of that connection); no table function; the same output columns in the same
order. Otherwise the export refuses, saying the AI proposal did not pass the checks.
An accepted model carries `-- Translated by AI from <dialect>: review it before use.`
in its header, `ai_translated` in its meta, and a line in the README.

## Acceptance

1. Off by default: with both toggles off the export is byte-identical to today's.
2. With a fake model (tests): descriptions land where missing and only there, marked,
   sanitized; a second export of the same version makes no AI call and gives the same
   zip; a new version calls again.
3. Translation: an accepted proposal is used and marked; a proposal with another
   column, a DDL, an unknown table, a table function is refused.
4. Cap reached, model not enabled, provider error → a clear refusal, in the user's language.
5. Live, once, on the development stack with the real provider: the descriptions of a
   Sample flow, and dbt `parse` / `docs generate` on the result.
6. Dialog: the two toggles appear only when the AI is available; browser check.

## Changed after the bug hunt (2026-10-16)

- **Jinja.** Removing `{{ }}`, `{% %}`, `{# #}` from AI text is no longer the safety measure: every text
  the exporter writes (AI text included) is neutralised so dbt prints it back as it is. AI text also
  loses the characters YAML cannot carry (C0/C1 controls, DEL, U+FFFE/FFFF) before it is kept.
- **Memory per version** now includes "no description": items the AI leaves out, or answers with nothing,
  are asked once more in the same export, then kept without a description. A re-export of a stored
  version needs no provider; the README names the model(s) that wrote the stored texts. Overlapping
  exports of one version ask once (a claim row in `dbt_ai_texts`). Keys over 500 characters are stored
  as `sha256:<digest>`.
- **Limits.** One time budget for all the AI calls of an export (`AI__DBT_EXPORT_TIMEOUT_SECONDS`, 180 s),
  the assistant's per-turn limits on each call, and failed calls recorded in the spending ledger. A query
  over 20 000 characters is refused (`ai_query_too_long`) instead of being sent cut.
- **Kept texts with the AI off.** The plan reports what is kept for the current version (`ai.stored`:
  descriptions, translations, the models that wrote them); the dialog offers to reuse them with the AI
  switched off, and the hint names the models that wrote them (acceptance 6 changes accordingly). An empty
  translation is never kept; a rejected one is forgotten only if it is still the one stored.
- **The engine call is bounded** (`ENGINE__DBT_EXPORT_TIMEOUT_SECONDS`, 100 s per call: HTTP 504
  `engine_timeout`, 502 `engine_unreachable`), and the AI budget also stops the engine's check of a new
  translation once it is spent, so 180 s + 100 s stay under the ingress' 300 s.
- **Checks on a proposal** (section 2) also refuse a `SETTINGS` clause, functions that read data or
  settings outside the model (`function_not_allowed`), `IN db.table`, a qualified placeholder and a table
  hidden behind a CTE that is not visible where it is read; on the native and ClickHouse targets a
  model's proposal may read only that model's own sources.

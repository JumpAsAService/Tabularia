# Spec: the Tabularia agent, in the chat and over MCP

Status: designed 2026-10-28 from the owner's decisions (2026-10-19 and 2026-10-28).

## Objective

A person asks in plain words — «every morning send me the orders still open seven days
after they were placed» — and an agent finds the data, asks what it is not sure about,
shows a plan, and after an explicit confirmation creates and schedules the flow. The same
abilities are offered to agents outside Tabularia (Claude Desktop, an IDE, another
company agent) through an **MCP server** authenticated with Tabularia's API tokens.

## Decisions (owner)

- **One set of tools** for the chat and for MCP: the same names, arguments and checks.
- **Abilities, v1**: explore and read (folders, objects, datasources with schema and rows,
  read-only SQL on the catalog, runs); complete flows (create, change existing ones with
  a diff, every Output kind, preview, run, schedule); datasources from **existing**
  connections and SQL scripts (create, refresh, schedule, write a datasource); flow groups
  and marimo notebooks (create, change). **Never connections**: credentials never reach a
  model nor a conversation; when one is missing the agent says so and links the page.
- **Always with the rights of whoever uses it** (the person in the chat, the token's owner
  over MCP), checked when the plan is made AND again when it is applied.
- **Human in the loop**:
  - in the chat, clarifying questions are **cards with clickable choices** (2–4 options
    with a description, the recommended first, «Other» free text, several questions in one
    card); before any change, a **plan card** (what, where, when, to whom, first rows,
    the diff of a changed flow) with **Confirm / Change / Cancel**. Only the person's click
    applies a plan: the chat's agent has no tool to confirm.
  - over MCP: **plan → confirm**. A write tool never changes anything: it adds an action
    to a pending plan and returns the plan in words with its id; `confirm_plan(plan_id)`
    applies it. `confirm_plan` is annotated as destructive, so MCP clients ask their human
    before calling it; every plan also has a link to review and confirm it in Tabularia.
    An administrator may require that plans made over MCP be confirmed **only in
    Tabularia** (`MCP__CONFIRM_IN_TABULARIA_ONLY`).
  - (MCP elicitation is not used in v1: it needs a server session pinned to one replica,
    and the gateway runs several replicas without session affinity.)
- **Visible work** (2026-10-19): in the chat every tool call shows as a step — what in
  words, arguments on demand, status, duration, result; retries grouped under the same
  step with the reason; the final outcome stated clearly.
- **MCP access**: personal and service-account API tokens; a `read` token sees and calls
  only the reading tools, a `write` token all of them. An administrator's switch turns the
  external MCP off (on by default, and only when AI is not required: MCP works without an
  AI model, the model is the client's). Every applied action is audited with
  `via: "mcp"` (and the token's name) or `via: "chat"`.

## Model

- `AgentDraft`: a server-side work area for a flow being built or changed — owner,
  origin (`chat`|`mcp`), `chat_id` or token, the flow it changes (or none), name, folder,
  engine, the definition, `updated_at`; expires after 24 h unused.
- `AgentPlan`: owner, origin, `chat_id`/token name, `actions` (JSON list, each with a
  `kind` and its validated arguments), `summary` (words), status
  `pending|applied|failed|cancelled|expired`, `result` (per action: ok, the object made,
  its link, or the error), `created_at`, `expires_at` (30 min), `applied_at`.
  Postgres only, so any gateway replica serves any call.

## Tools

Read (all tokens, the chat):
`search(query, kinds?)`, `list_folders()`, `list_datasources(search?)`,
`describe_datasource(id)`, `query_datasource(id, sql, …)` (the existing tool),
`list_connections(kind?)` (name, type, folder — no secret ever), `list_environments()`,
`get_flow(id)`, `get_sql_script(id)`, `get_flow_group(id)`, `get_notebook(id)` (never
someone's unsaved draft: only that there is one, and whose),
`run_sql_preview(sql, engine | connection)` (a SQL script's run without saving, with its
rules: the datasources the user reads, or CONNECT), `list_tables(connection)`,
`list_columns(connection, table)`, `list_files(connection, …)` (what an existing
connection offers, as the datasource dialogs show it),
`list_runs(object?, status?)`, `get_run(id)`, `get_plan(id)`.

Drafts of flows (no change to any saved object):
`flow_draft_new(name, folder_id, engine?)`, `flow_draft_open(flow_id)`,
`flow_add_source(draft, datasource_id)`, `flow_add_step(draft, after, operation, params, right?)`,
`flow_change_step(draft, node, params, operation?)`, `flow_connect(draft, from, to, input)`,
`flow_remove_node(draft, node)`, `flow_add_output(draft, after, kind, …)` (datasource,
database, s3, sftp, email — with the existing checks: allowed domains, CONNECT…),
`flow_preview(draft, node, rows)`, `flow_draft_show(draft)`.

Plan building (each adds an action and returns the plan so far):
`plan_save_flow(draft)`, `plan_run_flow(flow_or_draft, mode)`,
`plan_schedule(object, cron | null)` (flows, datasources, SQL scripts, flow groups),
`plan_create_datasource(folder, connection, source)` (table, SQL, S3, SFTP, SharePoint, API),
`plan_refresh_datasource(id)`, `plan_sql_script(folder | id, sql, engine | connection, output?)`,
`plan_run_sql_script(id)`, `plan_flow_group(folder | id, name, steps)`, `plan_run_flow_group(id)`,
`plan_notebook(folder | id, name, environment, source)`, `cancel_plan(id)`.
An object the plan creates has no id yet: running or scheduling it names the action that
creates it (`planned_action`, its number in the plan), as a flow's run names its draft.
Planning a change of an object the plan changes already goes on from that change, in its
place.

Chat only: `ask_user(questions)` — ends the turn and shows the card.
MCP only: `confirm_plan(plan_id)`.

## Applying a plan

`POST /agent/plans/{id}/confirm` (the chat's button, a person's session) and
`confirm_plan` (MCP) run the same code: the plan must be the caller's, pending and not
expired; each action is validated again and applied **in order** through the same service
functions and routes the interface uses (so every rule holds: permissions, schedules'
authority, contracts, allowed domains, audit), stopping at the first failure; the result
of each action is stored and returned with links. A plan applied is never applied twice.

What the person confirms is what they saw (2026-10-28): the page and the chat's card send
back the plan's `revision`, and a plan the agent changed since is refused until it is
looked at again; an action is never added to a plan already being applied. The plan shows
everything that will run or be sent — an Output's post-SQL, an email's copies and message,
an API call's headers and body, a container's command, a script's or notebook's whole
code. A plan made in the chat is confirmed by a person signed in, never with an API token.
Deleting a conversation cancels its pending plans and drops its drafts.

## Pieces

1. The core: tool registry, drafts, plans, read tools, flow tools, applying, the MCP
   endpoint (`/mcp`, Streamable HTTP, stateless, tokens with read/write levels, the
   administrator's switch, audit), the page to review and confirm a plan in Tabularia.
2. The chat: the Assistant page becomes the agent — question cards, plan cards, grouped
   retries, reopening a conversation with its cards.
3. Datasources, SQL scripts, flow groups, notebooks.
4. Documentation, live trials (an MCP client from outside; the chat in Chrome and Firefox).

# MCP: Tabularia for agents outside it

Tabularia serves the [Model Context Protocol](https://modelcontextprotocol.io) at `/mcp`,
so an agent that runs somewhere else (a desktop assistant, an IDE, another company agent)
can read the catalogue, query datasources and build flows with the same tools the
**Assistant** page uses. It signs in with an [API token](../api.md#signing-in-api-tokens)
and acts with the rights of the token's account. Nothing it builds changes anything until
a person confirms a **plan**. It needs no AI model on Tabularia's side: the model is the
client's.

## At a glance

| | |
|---|---|
| Address | `https://<your host>/api/mcp` (`http://localhost:8000/mcp` in development): Streamable HTTP, JSON answers, no session, so any gateway replica answers any call |
| Sign-in | `Authorization: Bearer tab_…`: a personal token or a service account's |
| Tools | 18 that read for a *read only* token; 40 for a *read and write* token |
| Changes | built in drafts and gathered in a plan; applied only by `confirm_plan` or by a person on the plan's page in Tabularia |
| Rights | the token's account, checked when a plan is made and again when it is applied |
| Audit | every action applied is in the audit log, with `via: mcp` and the token's name |
| Switches | `MCP__ENABLED`, `MCP__CONFIRM_IN_TABULARIA_ONLY`, `MCP__METADATA_ONLY`, also in Administration → Settings |

## How it works

### The tools

A *read only* token sees the tools that read:

| Tools | What they answer |
|---|---|
| `search`, `list_folders`, `list_datasources`, `describe_datasource` | the catalogue the account may read: objects by name, folders with what the account may do there, datasources with their descriptions, a datasource's columns, field descriptions, row count and a few rows |
| `query_datasource`, `run_sql_preview` | one read-only `SELECT` on a datasource (the table is `self`), or a SQL script's query tried without saving it |
| `get_flow`, `get_sql_script`, `get_flow_group`, `get_notebook` | a saved object, its settings, schedule and last run |
| `list_runs`, `get_run`, `get_plan` | runs and their errors; a plan and its state |
| `list_connections`, `list_tables`, `list_columns`, `list_files`, `list_environments` | what a connection offers (tables, columns, the files a pattern takes) and the Python and R environments; never an address, a user or a secret |

A *read and write* token also gets the tools that build and plan:

| Tools | What they do |
|---|---|
| `flow_draft_new`, `flow_draft_open` | start a draft of a new flow, or of a saved one to change it |
| `flow_add_source`, `flow_add_step`, `flow_change_step`, `flow_connect`, `flow_remove_node`, `flow_add_output` | put datasources, steps and Outputs in the draft; each step answers with its columns and first rows |
| `flow_preview`, `flow_draft_show` | a node's first rows; the whole draft |
| `plan_save_flow`, `plan_run_flow`, `plan_schedule`, `plan_create_datasource`, `plan_refresh_datasource`, `plan_sql_script`, `plan_run_sql_script`, `plan_flow_group`, `plan_run_flow_group`, `plan_notebook` | add an action to the account's pending plan |
| `confirm_plan`, `cancel_plan` | apply a plan, or drop it |

Connections are never created or changed over MCP: credentials do not travel through an
agent. A datasource can be planned only from a connection that exists.

### Drafts, plans and confirmation

A draft is a work area on the server, private to the token's account, kept for 24 hours
unused. A plan lists actions in words («Create the flow … 3 nodes: out-1 writes the
datasource …», «Schedule the flow …: cron `0 6 * * *` in UTC»), with a `review_link` to its
page in Tabularia, and expires after 30 minutes. Until it is confirmed nothing exists: no
flow, no schedule, no run.

Two ways to confirm:

- **The agent calls `confirm_plan`.** The tool is marked *destructive*, so MCP clients ask
  their person before calling it. Every action is validated again, applied in order through
  the same code the interface uses, and the first failure stops the rest.
- **A person confirms it on the plan's page** in Tabularia (`/agent-plan?id=…`), signed in.
  With `MCP__CONFIRM_IN_TABULARIA_ONLY=true` this is the only way: `confirm_plan` refuses
  with the plan's link.

### Metadata-only mode

With `MCP__METADATA_ONLY=true` no value of a row leaves through `/mcp`: the tools that
return data (`query_datasource`, `run_sql_preview`, `flow_preview`) are neither listed nor
callable, and the others leave out sample rows and the text of errors. What it covers and
what it does not: [AI in Tabularia](ai.md#metadata-only-mode).

## Example: a flow planned by an agent

An administrator creates a service account `analytics-agent` with **EDIT** on the folder
`Sales` and a *read and write* token for it, and gives the token to the agent's MCP
configuration, never to a generic HTTP tool. Most clients that speak Streamable HTTP take
the address and a header; with a command-line client:

```bash
claude mcp add --transport http tabularia https://tabularia.example.com/api/mcp \
  --header "Authorization: Bearer $TABULARIA_TOKEN"
```

The calls the agent makes are plain JSON-RPC, so `curl` shows them. The first, `tools/list`:

```bash
curl -s https://tabularia.example.com/api/mcp \
  -H "Authorization: Bearer $TABULARIA_TOKEN" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}' | jq '.result.tools | length'
# 40
```

Asked for «orders by channel, every morning», the agent finds `orders` (`list_datasources`),
then builds and plans, one `tools/call` each:

```json
{"jsonrpc": "2.0", "id": 2, "method": "tools/call",
 "params": {"name": "flow_draft_new", "arguments": {"name": "Orders by channel", "folder_id": 7, "engine": "duckdb"}}}
```

| Call | Answer, shortened |
|---|---|
| `flow_draft_new` | `{"draft_id": 1, "folder": "Sales", "engine": "duckdb", "nodes": []}` |
| `flow_add_source` `{"draft_id": 1, "datasource_id": 45}` | `{"node": "src-1", "datasource": "orders", "rows": 12000000, "columns": […]}` |
| `flow_add_step` `{"draft_id": 1, "after": "src-1", "operation": "group_by", "params": {"by": ["canale"], "aggregations": [{"column": "id", "func": "count", "alias": "ordini"}]}}` | `{"node": "op-1", "columns": [canale, ordini], "first_rows": [{"canale": "Ristorazione", "ordini": 2430886}, …]}` |
| `flow_add_output` `{"draft_id": 1, "after": "op-1", "kind": "datasource", "datasource_name": "Orders by channel"}` | `{"node": "out-1", "writes": "writes the datasource «Orders by channel» in Sales"}` |
| `plan_save_flow` `{"draft_id": 1}` | `{"plan_id": 1, "status": "pending", "review_link": "https://tabularia.example.com/agent-plan?id=1", …}` |
| `plan_schedule` `{"kind": "flow", "draft_id": 1, "cron": "0 6 * * *"}` | the plan with its second action: «Schedule the flow «Orders by channel»: cron `0 6 * * *` in UTC (next run …)» |

The agent shows the plan to its person in its own words; once they agree it calls
`confirm_plan` with `{"plan_id": 1}`, or the person opens the review link and presses
**Confirm**. The flow is created, scheduled, and both actions are in the audit log with
`via: mcp` and the token's name. `cancel_plan` drops it instead, and the draft stays.

## Permissions

| To | You need |
|---|---|
| Use `/mcp` at all | An API token, and `MCP__ENABLED` (on by default) |
| The reading tools | A token of either level; each answer holds only what the account may read |
| Drafts and plans | A *read and write* token |
| Apply a plan | The rights each action needs, as in the interface: **EDIT** on the folder a flow is saved in, **RUN** to run or schedule, **CONNECT** on the connection a datasource is created from |

## Limits and good to know

- No sessions and no server-sent streams: `GET /mcp` and `DELETE /mcp` answer 405, every
  answer is one JSON document.
- MCP elicitation is not used: questions to the person are the client's business.
- The agent cannot create or change dashboards, reports, connections, permissions or
  users; it cannot delete anything.
- Turning `MCP__ENABLED` off makes `/mcp` answer 404 at once; the tokens keep working on
  the rest of the API.

## Related

- [AI in Tabularia](ai.md): the assistants inside Tabularia, which use the same tools
- [Tabularia API](../api.md): tokens, service accounts, the rest of the API
- [Ask your data](../capabilities/ask-your-data.md#asking-it-to-build): the same plans, made in the chat
- [Configuration: Agents over MCP](../configuration.md#agents-over-mcp)

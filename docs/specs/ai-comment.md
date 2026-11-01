# Spec: an AI comment on the numbers, in dashboards and reports

Status: decided with the owner on 2026-11-01, not built yet.

## Objective

An author puts a new element on a dashboard or a report: a short text written by the configured
AI model about the numbers beside it, such as what moved and what stands out. A reader sees it
next to the numbers, in the viewer, in the PDF and in a scheduled email.

The numbers never depend on the model. A provider that is down, a budget that is spent or a mode
that forbids it costs the comment only, and the page says so.

## Decisions

| Question | Decision |
|---|---|
| What the model reads | The aggregated numbers already computed for the elements the author picks, as the screen shows them, plus the author's instruction. Never rows, and no tool to query with |
| When it is written | By itself on opening and in a document, shared by whoever sees the same numbers. After a click on a tile the old comment goes, and a button **Comment this view** writes a new one |
| When it fails in a document | The document goes out, with one line saying the comment is not available |
| Metadata-only mode | With `AI__METADATA_ONLY` on nothing is sent: a total is a value (`services/metadata_only.py`). The element says it is off |
| Labelling | Always marked as written by AI, with the model and the time of the numbers, on screen and on paper. Not removable |

Out of this version: choosing a model per comment, editing the text by hand, a chart drawn by the
model, comments in the chat or over MCP.

## The element

A new item type `comment` in a report's definition (`backend/app/reports/definition.py`,
`gateway/app/services/report_definition.py`) and a tile type `comment` on a dashboard
(`MIN_TILE` 4 × 3):

```json
{"id": "story", "type": "comment", "title": "What changed", "grid": {"x": 0, "y": 0, "w": 6, "h": 4},
 "instruction": "Say what moved most against last year, for the sales director.",
 "reads": ["revenue", "by_area"]}
```

- `instruction`: at most 1,000 characters. It is the author's, like a saved SQL, and is checked for
  length only.
- `reads`: the ids of the elements it comments on, at least 1 and at most 10, each a KPI, a chart or
  a table of the same definition. An id that does not exist is refused on save, as a wrong field is.
- A report without this item renders byte for byte as before: `GOLDEN_PDF_SHA256` does not move.

## How it is written

The provider's keys and the budgets live in the gateway, so the gateway writes the comment
(`gateway/app/services/ai_comment.py`), on the code of the other agents (`ai_models`, `ai_pricing`,
`ai_prompts`, `_check_spending`, `record_spend`):

1. The numbers of `reads` come from the same path as the screen's (`numbers` for a dashboard,
   `tiles.compute` for a report), with **the reader's rights** and from the cache when they are there.
2. **Any element of `reads` the reader may not read, or that failed, stops the comment**: it says
   what a tile with no access says, and nothing about why. A text written from fewer numbers would
   mislead, and one written from more would leak.
3. What goes to the model: the instruction, each element's title, and its numbers as written on the
   screen (a chart's categories and series, a KPI's value and change, a table's page). At most 20,000
   characters: past it the comment asks to pick fewer elements, and no call is made.
4. One call, no tools, at most 500 tokens of answer, in the language of the definition. The system
   prompt is a seventh entry of the editable prompts (`ai_prompts`), and says that what comes between
   the markers is data and never instructions.
5. The answer is plain text, at most 1,500 characters. Nothing in it is rendered as markup or as a
   link: a text node on screen, escaped in Typst, escaped in the email.

### What is kept

A table `ai_comments` (the next free migration): key, text, model, numbers' time, cost, written at,
expiring with `AI__COMMENT_KEEP_SECONDS` (one day by default).

- The key is the hash of the instruction, the language, the model and **the numbers themselves**.
  Whoever computes the key has already read those numbers with their own rights: nobody finds a
  comment on numbers they could not read.
- A hundred people on one dashboard make **one call**: one writes, under a lock, and the others wait
  for it or show a placeholder without paying.
- A failure is kept for 60 seconds, so that a provider that is down is not asked at every opening.

### When a call is made

| Moment | Call |
|---|---|
| Opening, or Apply on a parameter, with no click active | Yes, if those numbers have no comment |
| A click on a tile | No: the old comment goes, with **Comment this view** in its place |
| **Comment this view** | Yes, if those numbers have no comment |
| Automatic refresh with new numbers | At most every `AI__COMMENT_MIN_SECONDS` per element (one hour by default): until then the old text stays, saying the time of the numbers it speaks of. A wall screen must not make 1,440 calls a day |
| Refresh by hand | As opening |
| PDF, PNG, scheduled send | Yes, always on the numbers of that document, if they have no comment: paper never speaks of other numbers |
| The designer | Only on **Try it**, pressed by the author; never at a keystroke |

### Who may cause a call, and who pays

- Spending goes on the person who caused the call, under the existing daily ceilings; a scheduled send
  goes on its author. With a ceiling reached the comment is not available, and a kept one is still shown.
- An observer causes no call and reads only what is kept, like the shared demo account.
- Every call is audited as `ai.comment` with the element, the model and the cost, never the numbers.
- With no provider configured the element says so to whoever may configure one, and is empty for the others.

## On the screen and on paper

- **Dashboard:** `/data` does not wait for the model. The tile shows a skeleton, then asks
  `POST /dashboards/{id}/comment` with the tile, the parameters and the clicks.
- **Report viewer, PDF, email:** the gateway gets the text first and passes it to the render as
  `comments`, waiting at most `AI__COMMENT_WAIT_SECONDS` (30 by default). Past it, or on any failure, the
  line of the decision above takes its place.
- **Both designers:** a new element **AI comment**: the instruction, the elements it reads, **Try it**.
  With metadata-only on or no provider, the panel says why it will not write.
- Every comment carries `Written by AI · <model> · numbers of <time>`, and the reader is told it may be
  wrong.

## Settings

`AI__COMMENT_KEEP_SECONDS`, `AI__COMMENT_MIN_SECONDS`, `AI__COMMENT_WAIT_SECONDS`, `AI__COMMENT_MODEL`
(empty: the default model). All in the panel and in Helm, and generated into `docs/configuration.md`.

## Testing

No paid call in any test or check, with the user's key or anyone's: a fake provider, as
`~/tabularia-carico/kube/prove-ui-2026-10-24/fake_openai.py`, recording what it receives. A real model
is left to the owner, and the final report says so.

| Level | What |
|---|---|
| Gateway | Metadata-only sends **nothing**; a reader with no rights on one element gets no text and no hint, with the kept comment there and with none; 100 at once make one call; a click makes none until asked; refresh within the interval makes none; a ceiling reached stops the call and leaves the document going; an observer causes none; what reaches the fake provider holds no row and no more than the screen shows |
| Engine | The item renders in PDF and PNG with hostile text (Typst syntax, a very long word); without it reports are byte-identical |
| Browser, Chrome and Firefox | Built by hand in both designers; five themes; a text holding markup stays text; click, **Comment this view**, Back |
| Whole | A scheduled send with a comment, opened in Mailpit, and the same with the provider down: the email goes, with the line |

## Pieces

Each ends with its tests and is a commit.

1. **The writing:** definition, gateway service, table, keys, lock, ceilings, modes, settings.
2. **Dashboards:** the tile, its route, the viewer's button, the designer's panel.
3. **Reports:** the designer's panel, viewer, PDF, PNG, email, Pushmail, the guides.

## Success criteria

1. In metadata-only mode a recording provider receives nothing, in every path.
2. No reader obtains a comment on numbers they may not read, cached or not.
3. 100 readers of one dashboard for a day of unchanged numbers cost one call.
4. A dashboard with a 60-second refresh and numbers changing each time costs at most 24 calls a day per
   element at the default interval.
5. With the provider down a dashboard, a PDF and a scheduled email all come out with their numbers.
6. Reports without the element are byte-identical (golden PDF), and all suites are green.

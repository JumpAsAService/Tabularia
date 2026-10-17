<!-- {% raw %} GitHub Pages: no Liquid in this page -->
# Revisione completa — 14 ottobre 2026

Revisione dell'intera applicazione (non solo delle modifiche recenti), con la performance per **decine di utenti** come priorità, poi correttezza, qualità e sicurezza. Referto per il proprietario del progetto; non committato.

## In breve

- **Metodo:** 16 corsie di analisi in parallelo (una era una prova di carico dal vivo sul prodtest), poi ogni rilievo verificato da due agenti indipendenti che cercavano di smentirlo (uno ripercorre il codice, uno giudica l'impatto reale), con un terzo nei casi di disaccordo; infine un giro di completezza sulle aree che nessuno aveva coperto.
- **Numeri:** 110 segnalazioni iniziali, 86 dopo l'unione dei doppioni, più 32 dal giro di completezza. **113 confermate** (12 alte, 43 medie, 58 basse), 5 smentite.
- **Per tipo:** 47 di correttezza, 37 di performance, 17 di sicurezza, 12 di qualità, struttura e test.
- **Il verdetto:** nell'uso normale l'app regge già 30 utenti con margine (sotto). I problemi veri sono nei casi limite: scritture nelle tabelle ClickHouse dei clienti, dati grandi (export, anteprime, lineage con lo storico), memoria dei processi del motore, e quattro scalate di privilegi.

## La misura sotto carico

Stack prodtest (configurazione di produzione: 3 gateway, 2 orchestratori, worker, proxy Caddy), motore duckdb, salita a 1, 10, 20 e 30 utenti (27 utenti dati + 3 sessioni admin) che rifanno le chiamate vere dell'interfaccia, compresi i polling.

| Cosa | Risultato a 30 utenti |
|---|---|
| Richieste | 5.139, zero errori 5xx, zero timeout (i 403 erano prove volute di permessi) |
| Liste (flussi, datasource, ricerca…) | p95 entro 60 ms |
| Anteprima di un nodo / colonne / viewer | p95 177 / 197 / 134 ms |
| `/queue` | 1,06 s fisso (quattro domande ai worker che aspettano tutto il timeout) |
| `/system/memory` (indicatore in alto) | p95 423 ms, la richiesta più frequente (12,7% del traffico) |
| CPU dell'intero stack | media 1,4 core, picco 2,3 su 8; il più impegnato è il worker delle anteprime (40% di un core) |
| Memoria | processi delle anteprime ≤ 235 MB con duckdb; macchina sempre sopra 8 GB disponibili |
| Postgres | al massimo 23 connessioni, 1 attiva |

Nessuna risorsa si è saturata. Limiti della misura: dataset di esempio piccoli (il più grande ha 105 mila righe); oltre i 30 utenti la prova non è andata; polars misurato solo un'anteprima alla volta.

## I 12 rilievi alti

- **Append su ClickHouse per posizione.** Se l'ordine delle colonne del flusso non coincide con quello della tabella del cliente, i valori finiscono nelle colonne sbagliate senza alcun errore (cliente e regione scambiati, importi troncati in colonne intere). Provato su un ClickHouse vero. Correzione: `INSERT` con l'elenco delle colonne per nome e controllo preventivo dello schema. *(P1, sforzo S, `gap-1-2-1`)*
- **Replace su ClickHouse ricostruisce la tabella dallo schema del flusso.** Si perdono engine, ORDER BY, partizioni, TTL, DEFAULT e commenti; le materialized view sulla tabella non vedono i dati nuovi. Il run dice «riuscito». Correzione: tabella di appoggio `CREATE TABLE … AS target`, inserimento per nome, poi `EXCHANGE`. *(P1, sforzo M, `gap-1-2-2`)*
- **Un output che dopo 10 minuti è ancora in coda o in esecuzione non ferma il flusso.** I nodi a valle leggono dati non ancora scritti, il run viene chiuso come fallito mentre l'output poi pubblica, e lo slot successivo può far partire una seconda esecuzione sovrapposta. Con 20 flussi alle :00 e 2 worker capita davvero. Correzione: fermare la sequenza, contare il tempo dall'avvio reale, non liberare il flusso finché un figlio è vivo. *(P2, sforzo M, `background-loops-2`)*
- **Export Excel ed elaborazione degli upload girano dentro il processo dell'API del motore.** Circa 800 mila righe × 20 colonne superano i 4 GB: il kernel uccide l'API e cadono anteprime, export e upload di tutti. Correzione: export come task su una coda propria, oppure tetto più basso, scrittura a memoria costante e un export pesante alla volta. *(P4, sforzo M, `backend-api-tasks-2`)*
- **DuckDB non ha un budget di memoria per processo:** ogni connessione crede di avere l'80% del container, quindi 2–4 anteprime pesanti insieme sforano il limite e i processi vengono uccisi. È lo stesso crash di ieri, sul motore che vuoi usare. Correzione: `memory_limit` e `threads` divisi per la concorrenza dei worker. *(P4, sforzo S, `engine-dataplane-1`)*
- **Il nodo SQL su DuckDB copia tutto il dato a monte in memoria** (circa 8 volte il parquet) anche per un'anteprima di 100 righe. Correzione: sandbox su file con limite di memoria e spill su disco. *(P4, sforzo S, `engine-dataplane-3`)*
- **Ogni anteprima riscarica da S3 l'intero parquet sorgente.** Su un dataset da 2,4 GB sono 6–14 secondi per anteprima: con 30 utenti la coda cresce senza limite. Correzione: riuso nella stessa richiesta, cache su disco per worker, letture parziali (solo le parti del file che servono). *(P5, sforzo M, `engine-dataplane-2`)*
- **La pagina Lineage carica tutti i run di tutti i flussi.** Con un anno di storico (488 mila run) sono 5,5–7 secondi e +700 MB per chiamata: due chiamate insieme fanno cadere un gateway da 1 GB. Correzione: una riga per flusso con un indice (0,6 ms misurati). *(P6, sforzo S, `gateway-queries-1`)*
- **Le pianificazioni girano con i permessi di chi le ha impostate:** chi modifica un gruppo o un flusso altrui (con solo EDIT, aggiungendo celle che può solo leggere) li fa eseguire con l'autorità dell'autore. Già noto in parte. Correzione: una regola sola che spegne la pianificazione altrui quando si modifica o si sposta l'oggetto, e RUN richiesto sulle celle nuove. *(P3, sforzo M, `quality-architecture-2`)*
- **Salvataggio di flussi e viste senza limite di dimensione:** un utente con EDIT, mandando 100–150 MB, fa cadere un gateway; ripetendolo, tutti e tre. Correzione: limite sui campi e sul corpo della richiesta al proxy. *(P3, sforzo S, `sec-injection-1`)*
- **Il blocco delle funzioni di lettura file nel nodo SQL di Polars si aggira:** un autore di flussi legge i file del worker, compresi i dati temporanei di altri utenti. Correzione: validare l'albero della query, non il testo con un'espressione regolare (come già fa DuckDB a livello di motore). *(P3, sforzo M, `sec-secrets-infra-1`)*
- **Un utente con solo CONNECT può puntare una connessione salvata verso un suo server e premere «Prova»:** riceve la password memorizzata dall'admin. Correzione: chiedere di nuovo la password (o un permesso più alto) quando cambiano host, porta o utente; registrarlo nell'audit. *(P3, sforzo M, `sec-secrets-infra-2`)*

## Interventi proposti

Ordine consigliato: **P1 e i quattro alti di P3** subito (dati dei clienti e scalate di privilegi), poi **P4 e P2** (stabilità), poi **P6 e P5** (velocità con molti dati e molto storico), infine **P7, P8, P9**. Lo sforzo è indicato come S (ore), M (circa un giorno), L (più giorni), come lo hanno stimato le corsie.

### P1 — Scritture nei database dei clienti (ClickHouse prima di tutto)

Il 99% dei clienti scrive su ClickHouse esterno: append e replace oggi possono corrompere o snaturare le tabelle senza errori. Si aggiungono le pulizie (tabelle di appoggio lasciate, nomi da quotare, fusi orari, post-SQL) e un test contro database veri.

11 rilievi: 2 alti, 1 medi, 8 bassi. Sforzo: 8 S, 3 M, 0 L.

| Gravità | Sforzo | Rilievo | ID |
|---|---|---|---|
| alta | M | ClickHouse 'Replace' swaps in a new table: engine, ORDER BY, partitions, TTL, types, DEFAULT columns and comments are lost, and materialized views never see the new rows | `gap-1-2-2` |
| alta | S | ClickHouse append matches columns by position: reordered or renamed outputs land silently in the wrong columns of the customer's table | `gap-1-2-1` |
| media | S | On ClickHouse a failing post-SQL marks the run failed after the data is already committed; re-running an append duplicates every row | `gap-1-2-3` |
| bassa | M | Postgres 'Replace' holds an ACCESS EXCLUSIVE lock on the customer table for the whole load: every reader blocks, and a long reader in front makes them queue | `gap-1-2-4` |
| bassa | M | The DB destination writer has no test against a real database; the mocked unit tests could not have caught any of the defects above | `gap-1-2-11` |
| bassa | S | A ClickHouse write killed by revoke, stale timeout, OOM or the hard time limit leaves a staging table full of customer data that nothing removes | `gap-1-2-5` |
| bassa | S | ClickHouse destination fails on table or database names that need quoting, after it has already created an empty target | `gap-1-2-6` |
| bassa | S | Naive-UTC timestamps are not pinned to UTC at the destination: ClickHouse shows them in server-local time, and a MySQL TIMESTAMP column stores a wrong instant | `gap-1-2-8` |
| bassa | S | Oracle and SQL Server connections are offered as Output destinations, but the writer has no code for them and the run fails after the transform with the bare error 'oracle' | `gap-1-2-9` |
| bassa | S | post_sql is split on every ';', which breaks statements containing ';' in string literals or procedural blocks | `gap-1-2-10` |
| bassa | S | Destinations need DDL rights even to append to an existing table: Postgres needs CREATE on the schema and TRUNCATE for replace, ClickHouse needs CREATE and DROP TABLE on the database | `gap-1-2-7` |

### P2 — Esecuzione dei flussi e dei gruppi: niente sovrapposizioni, stessi risultati da ogni pulsante

Nessuna esecuzione sovrapposta dello stesso flusso, gli stessi risultati da qualunque pulsante (il Run dell'editor non fa la copia su S3 esterno e ignora le modifiche non salvate se ci sono Refresh o Run-flow), la cancellazione di flussi/datasource/connessioni che oggi dà 500 su Postgres, e la compatibilità DuckDB: 4 degli 11 flussi di esempio falliscono su duckdb per `EXTRACT(… FROM …)`.

12 rilievi: 1 alti, 6 medi, 5 bassi. Sforzo: 9 S, 3 M, 0 L.

| Gravità | Sforzo | Rilievo | ID |
|---|---|---|---|
| alta | M | An output still queued or running after the 10-minute wait cap does not stop the flow; the orchestration closes and frees the flow for a second, overlapping run | `background-loops-2` |
| media | M | A Run from the editor never makes the S3 copy: the flow-to-run translation is written twice and the two copies already disagree | `quality-architecture-1` |
| media | M | Deleting a flow, a datasource or an SMTP connection fails with a 500 because foreign keys are off in every gateway test | `quality-architecture-3` |
| media | S | Flow-group cells ignore running orchestrations when counting MAX_CONCURRENT, and the orchestrator's pool is smaller than that budget; when the pool runs out, the whole event loop freezes for 30 s | `background-loops-3` |
| media | S | Editor Run ignores unsaved edits when the flow has Refresh or Run-flow nodes | `frontend-correctness-1` |
| media | S | DuckDB and BigQuery compute still reject EXTRACT(... FROM ...), so 4 of the 11 Sample flows cannot preview or run on duckdb | `measure-1` |
| media | S | A refresh cell in a later step accepts an import that started before the previous step ended (stale data, group shows success) | `flow-groups-3` |
| bassa | S | The run poll returns 50 runs with full cell grids every 2.5 s while any run is live | `flow-groups-4` |
| bassa | S | Deleting a group and the scheduler take the same two locks in opposite order (Postgres deadlock) | `flow-groups-5` |
| bassa | S | The admin Performance tab lumps every flow-group run into one 'deleted flow' row | `flow-groups-6` |
| bassa | S | Two overlapping run closes on one datasource can let the older snapshot win and leave the newer parquet orphaned | `correctness-core-6` |
| bassa | S | Schedule load heatmap ignores flow-group schedules | `gateway-queries-11` |

### P3 — Sicurezza

Furto di credenziali, lettura di file, gateway abbattibili da un utente, permessi presi in prestito dalle pianificazioni; poi fughe di nomi (OpenLineage, audit agli osservatori), token SSO che sopravvivono alla disattivazione, TLS SMTP non verificato, voci mancanti nell'audit. Grafana 11.2.0 nel compose di produzione resta aperto (già nel referto dell'8 ottobre, B5).

16 rilievi: 4 alti, 8 medi, 4 bassi. Sforzo: 9 S, 7 M, 0 L.

| Gravità | Sforzo | Rilievo | ID |
|---|---|---|---|
| alta | M | A CONNECT-only user can retarget a saved connection and exfiltrate its stored password | `sec-secrets-infra-2` |
| alta | M | Polars SQL-node file-function blocklist is bypassable (arbitrary file read on the worker, cross-tenant data) | `sec-secrets-infra-1` |
| alta | M | Schedules run with their author's rights, but only moving a datasource or group switches someone else's schedule off; editing a group borrows the author's RUN *(noto)* | `quality-architecture-2` |
| alta | S | Flow and saved-view writes accept an unbounded JSON body, letting any EDIT user OOM-kill gateway replicas | `sec-injection-1` |
| media | M | Per-flow OpenLineage endpoint leaks names, schema, storage keys and DB host of objects in folders the caller cannot read | `sec-authz-1` |
| media | M | SSO users keep API access after they are disabled or demoted at the IdP: personal tokens last up to 365 days and SSO roles are only re-synced at the next SSO login | `sec-authn-1` |
| media | M | GET /tasks/{task_id} proxy performs no per-object authorization (documented debt) | `sec-authz-3` |
| media | S | Observer role sees unmasked email recipients, DB/webhook hostnames and other personal data in audit-log `detail` | `sec-authz-2` |
| media | S | Login response time tells which emails have a password account (about 35 ms vs about 300 ms), and unknown emails are not rate-limited | `sec-authn-3` |
| media | S | SMTP credentials sent over unverified TLS (no certificate validation) | `sec-secrets-infra-3` |
| media | S | No audit entry for 21 state-changing routes, including folder create/move/delete, user deletion and datasource moves | `quality-architecture-6` |
| media | S | Helm: by default the engine and workers have unrestricted egress; the production example's 0.0.0.0/0 includes the cloud metadata address, and the template cannot exclude it | `gap-1-5-4` |
| bassa | M | Users cannot change their own password, and the only rule is 6 characters minimum | `sec-authn-4` |
| bassa | S | /api/docs and /api/redoc load floating-version CDN scripts with no SRI and no CSP, on the app's own origin, and the API tokens dialog links there | `sec-authn-2` |
| bassa | S | An IdP group whose name matches a Tabularia admin or observer group still grants that role (AUDIT-2026-09-19 M2 is still open), and personal tokens now make the grant persistent *(noto)* | `sec-authn-5` |
| bassa | S | Production .env secret files are protected only by a local, uncommitted git exclude | `sec-secrets-infra-5` |

### P4 — Memoria del motore: nessun processo che fa cadere gli altri

Che nessun processo pesante faccia cadere gli altri: budget di memoria per DuckDB, export e upload fuori dall'API, spill separati, pulizia di /tmp, tetti di memoria coerenti con la RAM della macchina.

12 rilievi: 3 alti, 4 medi, 5 bassi. Sforzo: 8 S, 4 M, 0 L.

| Gravità | Sforzo | Rilievo | ID |
|---|---|---|---|
| alta | M | Any user's Excel export of a big view can OOM-kill the whole engine API, which runs exports and upload conversions in-process | `backend-api-tasks-2` |
| alta | S | DuckDB runs with no per-process memory or thread budget: every connection assumes it has 80% of the container's memory | `engine-dataplane-1` |
| alta | S | DuckDB `sql` node copies the entire upstream into an in-memory table in an unbounded sandbox, even for a 100-row preview | `engine-dataplane-3` |
| media | M | Synchronous in-process xlsx/csv ingest (<=50MB) can drive the shared backend data-plane API to multi-GB RSS | `sec-injection-3` |
| media | M | foreach re-downloads its nested source once per iteration and keeps every temp copy until run end, filling worker /tmp | `sec-injection-2` |
| media | S | All DuckDB connections in a container share one spill directory, so concurrent spills fail or read each other's data | `backend-api-tasks-3` |
| media | S | Helm: memory requests are 1/4 to 1/8 of the limits the application sizes itself to, so the scheduler reserves 5-7x less memory than the pods are built to use | `gap-1-5-2` |
| bassa | M | Helm: with no CPU limits, DuckDB and Polars use one thread per node vCPU in every Celery child, and DuckDB's spill ceiling ignores the emptyDir sizeLimit, so a large spill evicts the whole pod | `gap-1-5-3` |
| bassa | S | Polars foreach collects the entire driver before checking the 1,000-iteration cap | `engine-dataplane-7` |
| bassa | S | OOM-killed or hard-time-limited tasks leave full source copies in /tmp, and nothing sweeps them | `engine-dataplane-8` |
| bassa | S | Default memory ceilings add up to more than the RAM the guide asks for (17 GiB vs 8 GB; prodtest 20 GiB on a 15.4 GiB host), so no ceiling protects the host or Postgres | `infra-perf-2` |
| bassa | S | Previews inherit the 1-hour run time limit and rely on the API to revoke them | `backend-api-tasks-7` |

### P5 — Anteprime veloci anche sui dati grandi

Con i dati di esempio le anteprime sono già veloci (p95 177 ms a 30 utenti); sui dataset grandi il collo di bottiglia è riscaricare tutto il file a ogni clic, più le ricerche delle colonne e le copie della step-cache che occupano i posti delle anteprime.

8 rilievi: 1 alti, 5 medi, 2 bassi. Sforzo: 5 S, 3 M, 0 L.

| Gravità | Sforzo | Rilievo | ID |
|---|---|---|---|
| alta | M | Every preview downloads the whole source parquet (or the whole cached parent) from S3: no reuse across previews, none within one preview | `engine-dataplane-2` |
| media | M | Data-contract profiling and checks scan the whole dataset in memory with Polars on the preview queue | `backend-api-tasks-4` |
| media | S | The editor's column lookups run the full chain (limit 1) and queue a materialization just to read column names | `engine-dataplane-4` |
| media | S | Every editor parameter change wipes all cached node columns, so each preview waits for 1-2 extra serial preview tasks; Output-node text fields start previews on every keystroke | `frontend-perf-1` |
| media | S | Background step-cache copies run on the 4 interactive preview slots: 39% of the preview worker's measured busy time | `infra-perf-3` |
| media | S | The Viewer's hidden chart runs a full-dataset aggregation on every datasource open, and the chart never re-queries after Apply, a datasource switch or a node change | `frontend-perf-2` |
| bassa | M | A run re-downloads its own output once per post-step: contract check, DB destination, S3 copy, email | `engine-dataplane-9` |
| bassa | S | Step cache grows about 60 MB per minute under 20-30 editing users and is kept 24 h; DuckDB writes it as SNAPPY | `measure-5` |

### P6 — Gateway e database: query, indici, permessi, compressione

Lineage, indici mancanti su `runs` (datasource_id, started_at, flow_id), permessi ricalcolati più volte per richiesta (fino a 0,25 s a 2.000 cartelle), pagina Flussi da 2 s di CPU, `/queue` da 1 s (si scende a 14 ms indicando quanti worker aspettare), compressione delle risposte JSON assente.

15 rilievi: 1 alti, 6 medi, 8 bassi. Sforzo: 10 S, 5 M, 0 L.

| Gravità | Sforzo | Rilievo | ID |
|---|---|---|---|
| alta | S | /lineage loads every run of every readable flow as full ORM rows: 5.5-7 s and about +700 MB per call, enough to OOM-kill a 1 GiB gateway replica | `gateway-queries-1` |
| media | M | Every permission check reloads the whole projects and permissions tables as ORM objects, several times per request, with no per-request reuse | `gateway-request-path-1` |
| media | M | Flows page open costs about 2 s of GIL-bound gateway CPU: /runs/activity pulls 175K rows into Python and /flows/search pulls every run of the page's 50 flows | `gateway-queries-3` |
| media | M | /schedule/load expands every cron firing in Python on every Flows page open: 0.5 s for 7 days, 2.1 s for 30 days, uncached and GIL-bound | `gateway-queries-5` |
| media | M | Run and ingest pollers are async routes that do SQL on the event loop and keep a pooled connection while awaiting the engine (the stall fixed in proxy.py, still open here) | `gateway-request-path-2` |
| media | S | Import polling and the refreshing flag scan all ingest history: runs.datasource_id has no index (36-53 ms per call, polled every 2.5 s per page) | `gateway-queries-2` |
| media | S | Run history and performance pages sort or scan the whole runs table (no index on started_at) and load error tracebacks they do not show | `gateway-queries-4` |
| bassa | M | Key authorization fallback scans runs by output_key/input_key (no index), loads full rows and calls session.get(Flow) per match | `gateway-queries-10` |
| bassa | S | `.first()` on ORDER BY queries without LIMIT transfers the whole history (7 sites: refresh, scheduler, orchestrator, flow save, failure notice) | `gateway-queries-6` |
| bassa | S | GET /queue never answers in under 1 s: four Celery inspect broadcasts each wait their full 1.0 s timeout | `measure-3` |
| bassa | S | Memory badge: the most frequent request scrapes 86 KB from node-exporter inline, under a lock, with a 3 s per-replica cache | `measure-4` |
| bassa | S | API responses are never compressed: Caddy compresses only the frontend handler, so JSON travels 5-13x larger | `infra-perf-4` |
| bassa | S | List endpoints load Flow.definition, Datasource.columns and the contract report for every row only to return names | `gateway-queries-8` |
| bassa | S | Engine client has no timeouts on several control calls, and its default 100-connection pool has no wait limit | `gateway-request-path-4` |
| bassa | S | The webhook reclaim UPDATE scans the whole delivery log on every dispatch round | `background-loops-4` |

### P7 — Data contract

Una regola che fallisce a runtime azzera il report e blocca ogni refresh; la freschezza conta dal momento sbagliato o non invecchia mai.

7 rilievi: 0 alti, 3 medi, 4 bassi. Sforzo: 6 S, 1 M, 0 L.

| Gravità | Sforzo | Rilievo | ID |
|---|---|---|---|
| media | S | One rule that fails at run time (strict CAST, regex syntax Polars rejects) wipes out the whole contract report and refuses every refresh, even when that rule is only a warning | `gap-1-3-1` |
| media | S | Snapshot freshness on flow-published datasources counts from creation or the last metadata edit, not the last publish: the contract turns red about 60 s after every publish and sends 2 emails per publish | `gap-1-3-2` |
| media | S | Freshness rules on a column never age: the sweep skips them, so a datasource whose updates stop keeps a green state forever | `gap-1-3-4` |
| bassa | M | The worker checks the contract version from launch time, but the gateway records the report as the status of whatever version is current at reconcile: tightened rules are skipped and the new data shows as 'passed' | `gap-1-3-3` |
| bassa | S | accepted_values on datetime columns matches only Polars' cast text, which changes with the time unit, so ISO values (even the value shown in the report's sample) never match | `gap-1-3-5` |
| bassa | S | Two orchestrators can record the same freshness transition twice, and each copy sends its own email | `gap-1-3-6` |
| bassa | S | ODCS export drops the full-match meaning of pattern rules: logicalTypeOptions.pattern is the raw, unanchored regex | `gap-1-3-7` |

### P8 — Editor e interfaccia

Errori di logica nei form dei parametri e nei dialoghi (connessioni che si svuotano, valori dei filtri letti come JSON, cron a 6 campi descritto male), ricerca globale che non porta alla pagina già aperta, polling da rivedere, lingue fr/de/es incomplete (98 chiavi), tastiera nell'editor.

25 rilievi: 0 alti, 9 medi, 16 bassi. Sforzo: 17 S, 7 M, 1 L.

| Gravità | Sforzo | Rilievo | ID |
|---|---|---|---|
| media | M | The parameter form rebuilds only when the node id changes: Undo, Redo and a change of operation type leave stale values that the next edit writes back | `gap-1-4-2` |
| media | M | Filter and fill values are parsed as JSON whatever the column type: text codes become numbers, and an empty value becomes null and keeps zero rows | `gap-1-4-3` |
| media | M | Stale column names stay in a node's params but are hidden in the form, so a renamed or removed upstream column breaks the node and cannot be unticked | `gap-1-4-5` |
| media | M | Page loads chain independent requests one after another; Explore reloads everything with a skeleton on every tab focus and has no guard against stale responses | `frontend-perf-4` |
| media | S | Global search hits do nothing when they point to the page already open (Explore folder, Datasources, Connections, Viewer) | `frontend-correctness-3` |
| media | S | Delete, copy and cut in the editor act on the panel's node, not the node selected on the canvas after a drag or keyboard selection | `frontend-correctness-4` |
| media | S | Editing a connection after another type was open blanks its database and schema (and a SharePoint tenant and library), and Save stores the blanks | `gap-1-4-1` |
| media | S | Schedule dialog: 6-field crons are described with seconds first but run by croniter with seconds last, and the description is always in Italian | `gap-1-4-4` |
| media | S | Saving after a failed flow load overwrites the stored flow with an empty canvas | `frontend-correctness-2` |
| bassa | L | Italian error messages in non-Italian UIs: worse than recorded, the gateway also sends them *(noto)* | `frontend-correctness-14` |
| bassa | M | Status polling loops ignore hidden tabs, fetch more than they read, and one of them (/queue) costs 1 s of Celery broadcasts per call | `frontend-perf-6` |
| bassa | M | Core editor paths are mouse-only: palette nodes cannot be added by keyboard, and keyboard selection does not open the node panel | `frontend-correctness-6` |
| bassa | M | French, German and Spanish are missing 98 keys: Explore, the top search and admin tables appear in English | `frontend-correctness-7` |
| bassa | S | 422 validation errors show as raw JSON or '[object Object]' | `frontend-correctness-8` |
| bassa | S | After the session ends, the editor re-prompts every 15 s, and login loses the page the user was on | `frontend-correctness-11` |
| bassa | S | Editor pollers outlive the page or die silently | `frontend-correctness-12` |
| bassa | S | Datasource documentation drafts are lost on navigation with no warning | `frontend-correctness-13` |
| bassa | S | Connection 'Test' in edit mode tests the saved connection, not the edited host, database or user, and reports OK | `gap-1-4-6` |
| bassa | S | The palette and the Type select offer 'Sample rows', which has no parameter form, so every such node fails | `gap-1-4-7` |
| bassa | S | A typo in the foreach static items JSON silently deletes the saved items from the node | `gap-1-4-8` |
| bassa | S | The 'is one of' picker reuses another node's distinct values for a column with the same name | `gap-1-4-9` |
| bassa | S | Import sort keys are split on spaces, so a column name with a space cannot be a sort key | `gap-1-4-10` |
| bassa | S | Large flows: the editor re-serializes the whole canvas through reactive proxies on every drag frame, and all edges animate forever | `frontend-perf-5` |
| bassa | S | The entry bundle ships all five UI languages; the editor preloads ECharts for a tab that is hidden by default; cronstrue ships about 30 locales | `frontend-perf-7` |
| bassa | S | Search boxes can show results for an older query: Audit sends a request on every keystroke and the paged lists have no sequence guard | `frontend-perf-8` |

### P9 — Infrastruttura, Helm, test e struttura del codice

Rollback Helm che non riesce, connessioni Postgres con l'autoscaling, risorse del ClickHouse di esempio, nessun test eseguito dalla CI prima di pubblicare le immagini, file troppo grandi.

7 rilievi: 0 alti, 1 medi, 6 bassi. Sforzo: 4 S, 2 M, 1 L.

| Gravità | Sforzo | Rilievo | ID |
|---|---|---|---|
| media | S | Helm rollback cannot complete: the previous version's migrate init container fails on a database that a newer release has migrated | `gap-1-5-1` |
| bassa | L | The largest files keep growing, and the Explore and Datasources pages each carry their own copy of the import watcher *(noto)* | `quality-architecture-8` |
| bassa | M | No automated test gate: CI publishes images without running the 1,505 tests, and the 24k-line frontend has no typecheck or tests | `quality-architecture-7` |
| bassa | M | Run launching and reconciliation still live in routes/runs.py (1,257 lines), and services import from routes *(noto)* | `quality-architecture-4` |
| bassa | S | Helm: with the shipped gateway HPA, Postgres connections can reach 208-234 against the 100 the chart documents; the per-pod figure is 26, not 25 | `infra-perf-6` |
| bassa | S | At rest, the sample ClickHouse and cAdvisor cost more CPU than three gateways serving 30 users; the sample ClickHouse holds 1.1 GB with no memory limit | `measure-6` |
| bassa | S | storage-stats lists the whole bucket every 60 s on the run queue, with no expiry, so stale copies pile up in front of runs | `infra-perf-5` |

## Segnalazioni smentite

- `correctness-core-3` — Flow Output runs only finish (publish) when someone reads them; after 24 h a successful run is recorded as a Timeout failure and its data is lost. *Motivo:* Refuted. The finding says nothing reconciles flow publish runs in the background, but gateway/app/services/scheduler.py:242-264 (_tick, every TICK_SECONDS=60, in every orchestrator / role=all process) selects ALL runs with status not terminal and task_id != '' and calls _reconcile on each. Its comment says this exists so that a run finished while nobody is polling gets applied (swap/publish) withi…
- `gateway-request-path-5` — In role all, the scheduler tick and the audit purge run synchronous SQL on the request event loop. *Motivo:* The code is as described: _tick (scheduler.py:242-347) and retention_loop (audit_retention.py:60-77) run synchronous SQL in a coroutine. But this only affects APP__ROLE=all, and the cost there is negligible. The documented deployments run the gateway as api plus orchestrator: the production compose (docker-compose.yml:266, :289), Helm (templates/gateway.yaml:112), docs/deploy/docker-compose.md:173…
- `quality-architecture-9` — The three SQL engines still re-implement the same 29 operations, and each engine imports private names from the others. *Motivo:* This repeats a known item with no new evidence. The finding says itself that packages D1-D3 of the 2026-10-05 simplification report are still accurate, and it reports one improvement since then (NULLS LAST in the dbt export). Nothing is worse than recorded and no new user-facing defect is shown. The duplication is a maintainability cost that the owner has already seen and scheduled. The cross-engi…
- `sec-secrets-infra-4` — Celery logs full task results, putting preview data rows into container logs. *Motivo:* Refuted in its main claim. Celery 5.6.3 logs the return value as saferepr(R, resultrepr_maxsize), and saferepr defaults to maxlevels=3. The preview payload is {'ok', 'result': {'columns': [ {...} ], 'rows': [ {...} ]}}, so each row dict sits at nesting level 4 and is printed as the literal `{...}`; column dicts are cut the same way. I parsed all 1910 'succeeded in' lines in tabularia-preview-worke…
- `sec-secrets-infra-6` — Grafana pinned to 11.2.0 (CVE-2024-9264) in the production compose. *Motivo:* This is a known item repeated with no new evidence. The finding says the earlier audit framed it as development-only, but docs/audit/PRONTI-PER-PROD-2026-10-08.md row B5 already records 'Grafana è fermo a 11.2.0' against the production compose (docker-compose.yml:526,556). It is also not exploitable as configured. Caddy lets only an authenticated admin or observer reach /grafana (forward_auth to /…

## Limiti di questa revisione

- **Assistente AI:** la corsia che lo copriva (tetti di spesa, connessione al database tenuta durante lo streaming, chi raggiunge il nodo SQL) ha lavorato, ma il suo rapporto è stato bloccato da un filtro di sicurezza e non contiene rilievi. L'area resta da rivedere.
- **Secondo passaggio del critico di completezza** non eseguito (errore di rete); il primo aveva aperto 5 aree, tutte coperte tranne l'assistente AI.
- **Misura:** niente oltre 30 utenti, nessun dataset da milioni di righe misurato (stimato dal codice), polars solo un'anteprima alla volta; non chiamati gli endpoint verso servizi esterni (ClickHouse esterno, modello AI, export dbt).
- **68 file non dichiarati letti da nessuna corsia**, quasi tutti piccoli componenti e composable del frontend, modelli del gateway, le migrazioni 0001–0005 e i moduli dell'export dbt: `backend/app/api/routes/dbt.py`, `backend/app/engine/bigquery_engine.py`, `backend/app/engine/dtypes.py`, `backend/app/engine/matview.py`, `backend/app/engine/query_tag.py`, `backend/app/ingest/models.py`, `frontend/components/admin/EngineSettingsSection.vue`, `frontend/components/admin/ServiceAccountsSection.vue`, `frontend/components/admin/SiemSection.vue`, `frontend/components/admin/WebhooksSection.vue`, `frontend/components/ui/AiDisclosure.vue`, `frontend/components/ui/ApiTokensDialog.vue`, `frontend/components/ui/BannerBar.vue`, `frontend/components/ui/BrandMark.vue`, `frontend/components/ui/ContractBadge.vue`, `frontend/components/ui/ContractDialog.vue`, `frontend/components/ui/DbtExportDialog.vue`, `frontend/components/ui/OrderingField.vue`, `frontend/components/ui/Pager.vue`, `frontend/components/ui/PrivacyNotice.vue`, `frontend/components/ui/SecretOnce.vue`, `frontend/components/ui/SessionsDialog.vue`, `frontend/components/ui/SkeletonRows.vue`, `frontend/components/ui/TokenForm.vue`, `frontend/composables/opSummary.ts`, `frontend/composables/sessionDevice.ts`, `frontend/composables/useAiDisclosure.ts`, `frontend/composables/useChartTheme.ts`, `frontend/composables/useContracts.ts`, `frontend/composables/useEnginePolicy.ts`, `frontend/composables/useEngineSettings.ts`, `frontend/composables/useFlowClipboard.ts`, `frontend/composables/useFlowLayout.ts`, `frontend/composables/useIntegrations.ts`, `frontend/composables/useNodeStats.ts`, `frontend/composables/useOpIcons.ts`, `frontend/composables/usePopover.ts`, `frontend/composables/usePrivacy.ts`, `frontend/composables/useRunProgress.ts`, `frontend/composables/useSiem.ts`, `frontend/composables/useSkeleton.ts`, `frontend/composables/useTheme.ts`, `frontend/pages/admin.vue`, `frontend/pages/monitoring.vue`, `gateway/app/core/lingua.py`, `gateway/app/db/legacy.py`, `gateway/app/db/migrations/versions/0001_baseline.py`, `gateway/app/db/migrations/versions/0002_sessioni.py`, `gateway/app/db/migrations/versions/0003_token_e_webhook.py`, `gateway/app/db/migrations/versions/0004_impostazioni_del_motore.py`, `gateway/app/db/migrations/versions/0005_inoltro_dell_audit_al_siem.py`, `gateway/app/models/ai_model.py`, `gateway/app/models/api_token.py`, `gateway/app/models/banner.py`, `gateway/app/models/blob_deletion.py`, `gateway/app/models/dbt_ai_text.py`, `gateway/app/models/engine_policy.py`, `gateway/app/models/engine_setting.py`, `gateway/app/models/flow_version.py`, `gateway/app/models/privacy.py`, `gateway/app/models/shared_state.py`, `gateway/app/models/siem.py`, `gateway/app/services/dbt_ai.py`, `gateway/app/services/dbt_export.py`, `gateway/app/services/documented.py`, `gateway/app/services/messaggi_dbt.py`, `.github/scripts/test_image_secret_scan.py`, `.github/scripts/test_push_guard.py`.
- I test sono stati eseguiti solo in container usa e getta; il typecheck del frontend non era disponibile (stack di sviluppo fermo).

## Appendice — dettaglio dei rilievi

Testo originale delle corsie e dei verificatori (in inglese), in ordine di intervento.

### P1 — Scritture nei database dei clienti (ClickHouse prima di tutto)

#### `gap-1-2-1` — ClickHouse append matches columns by position: reordered or renamed outputs land silently in the wrong columns of the customer's table

Gravità **alta** (proposta: alta), sforzo S. Corsia `gap-1-2`.

Dove: `backend/app/ingest/db_destination.py:184`, `backend/app/ingest/db_destination.py:175-176`

**Description.** For ClickHouse, append writes the parquet into a staging table created from the output schema. It then runs `INSERT INTO target SELECT * FROM staging` (db_destination.py:184). ClickHouse maps INSERT ... SELECT * by position, not by name. So whenever the flow's output columns are in a different order from the existing table, values of compatible types (String/String, Int/Float) go into the wrong columns with no error. Floats are also silently truncated into Int columns. This happens on a later run after someone edits the flow, and also on the very first write into a table the customer created with its own column order. A renamed column lands in whatever column sits at that position. A table with an extra column (for example `loaded_at DateTime DEFAULT now()`) cannot be appended to at all. Postgres (ADBC COPY with a column list) and MySQL (explicit column list) match by name, verified below, so ClickHouse alone has this behaviour, and it is the dialect most of the owner's clients use.

**Impact.** Silent data corruption in customer ClickHouse tables, for example customer and region swapped, quantity written into amount, amount truncated into quantity. Nothing in the run result shows it. Downstream materialized views aggregate the corrupted values (the MV got region='carol' in the replace test). With append, every later scheduled run keeps adding shifted rows.

**Evidence.** Real ClickHouse 24.8 server in a throwaway container, write_parquet_to_db called directly. Output A (customer, region, amount, qty) creates `drift` with rows ('alice','north',10.5,1) and ('bob','south',20.25,2). Appending output B with the same columns reordered (region, customer, qty, amount = 'east','carol',3,30.75) reports OK and stores ('east','carol',3.0,30): customer='east', region='carol', amount=3.0, qty=30. Output C, with customer renamed to client, reports OK and stores ('dave','west',40.0,4) under customer. Output D, with one more column: NUMBER_OF_COLUMNS_DOESNT_MATCH (source 5, result 4). Pre-existing customer table `cust (region String, customer String, amount Float64, qty Int64)` plus output A reports OK and stores region='alice', customer='north'. Customer table with an extra `loaded_at DateTime DEFAULT now()`: append fails with 'Number of columns doesn't match (source: 4 and result: 5)'. Same drift sequence on Postgres 16: B lands correctly ('carol','east',30.75,3); C and D fail with 'column "client"/"note" of relation "drift" does not exist'. MySQL 8.4: B correct; C and D fail with 'Unknown column'.

**Fix.** Insert by name: build the column list from the parquet schema, quoted with backticks, and run `INSERT INTO target (c1, c2, ...) SELECT c1, c2, ... FROM staging`. Before streaming, read system.columns for the target and fail fast with a clear message when an output column is missing from the target. Columns the output lacks then take their DEFAULT, as on Postgres and MySQL. Add a real-server test for reorder, rename and add.

*Verification:* confirmed (alta); confirmed (alta).

#### `gap-1-2-2` — ClickHouse 'Replace' swaps in a new table: engine, ORDER BY, partitions, TTL, types, DEFAULT columns and comments are lost, and materialized views never see the new rows

Gravità **alta** (proposta: alta), sforzo M. Corsia `gap-1-2`.

Dove: `backend/app/ingest/db_destination.py:170-182`, `backend/app/ingest/db_destination.py:104-105`, `backend/app/ingest/db_destination.py:4-7`, `frontend/i18n/locales/en.gen.ts:815`

**Description.** The module contract (db_destination.py:6) says replace means TRUNCATE: the table definition stays and only the data goes. The UI labels the option 'Replace (TRUNCATE + INSERT)'. On ClickHouse, the writer creates the staging table from the parquet schema: all columns Nullable, Float64 for decimals, `ENGINE = MergeTree ORDER BY tuple()`. It then runs `EXCHANGE TABLES target AND staging` and drops the staging table, which now holds the customer's original table. The customer's table definition is gone. Every ReplacingMergeTree or SummingMergeTree engine, sorting and partition key, TTL, codec, LowCardinality or Decimal type, DEFAULT or MATERIALIZED column not present in the output, and table comment is replaced by a generic heap-like MergeTree. A Replicated*MergeTree target would become a local non-replicated table; this is inferred from the same SHOW CREATE result and was not tested (no Keeper). Rows are written into the staging table, so materialized views attached to the target never receive the replaced data, and their target tables keep stale aggregates. On a database that is not Atomic (Ordinary, Memory), replace fails only after the whole dataset has been streamed into staging.

**Impact.** One scheduled replace run destroys the physical design of a customer's production table. Queries lose their primary-key and partition pruning, deduplication engines stop deduplicating, TTL retention stops. Columns not produced by the flow disappear, so views and queries that reference them break. Aggregates fed by materialized views drift from the base table without any error. The run reports success.

**Evidence.** Before: CREATE TABLE default.sales (customer LowCardinality(String), region String, amount Decimal(12, 2) COMMENT 'EUR', qty Int64 DEFAULT 1, loaded_at DateTime DEFAULT now()) ENGINE = ReplacingMergeTree PARTITION BY region ORDER BY (region, customer) TTL loaded_at + toIntervalYear(1) SETTINGS index_granularity = 4096 COMMENT 'customer fact table'. write_parquet_to_db(mode='replace') reports OK {'rows_written': 2}. After: CREATE TABLE default.sales (customer Nullable(String), region Nullable(String), amount Nullable(Float64), qty Nullable(Int64)) ENGINE = MergeTree ORDER BY tuple() SETTINGS index_granularity = 8192. MV `sales_mv TO sales_by_region`: before replace [('north', 1.00)]; after replace still [('north', 1.00)] although the table now holds alice/north 10.5 and bob/south 20.25. Ordinary database: replace fails with 'Tables can be exchanged only in Atomic databases' after streaming; Memory database: 'renameTable() is not supported'.

**Fix.** When the target exists, create the staging table with `CREATE TABLE stg AS target`, which copies engine, keys, TTL, types and defaults. Insert into it by column name (see the append finding), then EXCHANGE. Use the generated DDL only when the target does not exist. Check `system.databases.engine` before streaming and fail fast on databases that are not Atomic. Document that materialized views on the target do not see replace data, or fall back to TRUNCATE plus INSERT by name when system.tables shows dependencies. Change the UI label and docstring so they describe what actually happens.

*Verification:* confirmed (alta); confirmed (alta).

#### `gap-1-2-3` — On ClickHouse a failing post-SQL marks the run failed after the data is already committed; re-running an append duplicates every row

Gravità **media** (proposta: media), sforzo S. Corsia `gap-1-2`.

Dove: `backend/app/ingest/db_destination.py:180-186`, `gateway/app/routes/runs.py:801-805`

**Description.** On ClickHouse the EXCHANGE (replace) or the INSERT ... SELECT (append) is final before post_sql runs (lines 180-186), and ClickHouse has no transaction to undo it. If any post-SQL statement fails (syntax, permissions, a broken ALTER, an OPTIMIZE that times out), write_parquet_to_db raises and the whole run is recorded as FAILURE, although the rows are already in the customer's table. The obvious reaction to a failed append run is to run it again, which appends the same rows a second time. The gateway's own stale-timeout comment (runs.py:801-804) names exactly this double-append risk. Postgres behaves correctly: post_sql runs inside the same transaction, so a failure rolls back the data and even the table creation (verified).

**Impact.** Duplicate rows in customer ClickHouse tables after a rerun. A run history that says 'failed' while the replace already happened misleads whoever investigates. Flow groups stop on this failed step even though its data was delivered.

**Evidence.** ClickHouse 24.8, append of 2 rows with post_sql "ALTER TABLE post_t UPDATE region = 'north;east' WHERE customer = 'alice'". The run fails with 'Single quoted string is not closed', and the table holds 2 rows. Retry: fails again, and the table holds 4 rows. Replace with post_sql 'OPTIMIZE TABLE post_t FINAL; SELECT throwIf(1)': the run fails with FUNCTION_THROW_IF_VALUE_IS_NON_ZERO while the table already holds the new 2 rows. Postgres 16 with the same broken post_sql: the run fails and to_regclass('post_t') is NULL (everything rolled back).

**Fix.** On ClickHouse, report post-SQL failures separately from the write: return rows_written plus a post_sql error, and have the gateway show a 'data written, post-SQL failed' outcome instead of a plain failure. Alternatively, for replace, run post-SQL against the staging table before the EXCHANGE where that makes sense. Document that post-SQL on ClickHouse is not atomic with the load.

*Verification:* confirmed (media); confirmed (media).

#### `gap-1-2-5` — A ClickHouse write killed by revoke, stale timeout, OOM or the hard time limit leaves a staging table full of customer data that nothing removes

Gravità **bassa** (proposta: bassa), sforzo S. Corsia `gap-1-2`.

Dove: `backend/app/ingest/db_destination.py:170-191`, `backend/app/api/routes/tasks.py:349-353`, `gateway/app/routes/runs.py:801-805`

**Description.** Staging cleanup relies only on the `finally` at lines 187-191. Several normal paths kill the Celery child without running it. The Queue panel 'stop' and the gateway's stale-run timeout call DELETE /tasks/{id} → `revoke(terminate=True)` with the default SIGTERM. Celery's prefork pool resets SIGTERM to its default action in the child (celery/concurrency/prefork.py WORKER_SIGRESET), so the process dies without unwinding. The OOM killer and the hard task_time_limit send SIGKILL. In every case a `<table>__tab_stg_<8 hex>` table with the rows streamed so far stays in the customer's database. After an interrupted replace that already got past the EXCHANGE, it holds the customer's previous data. There is no sweep anywhere: `__tab_stg` appears only at db_destination.py:170.

**Impact.** Each interrupted ClickHouse destination write leaks a partial copy of the output, up to the full dataset, into the customer's database. It uses their storage and stays visible to anyone with SELECT on that database. Since the stale timeout revokes long runs automatically, slow outputs leak one table per attempt.

**Evidence.** ClickHouse 24.8, 120,000-row output (three 50k batches), writer in a child process. SIGTERM after the first batch: exit code -15, target unchanged (120000 rows), system.tables shows ('default', 'mid__tab_stg_b82755e2', 50000). SIGKILL: exit code -9, ('default', 'mid__tab_stg_106bf6b6', 50000) left behind. A Python-level failure on the second insert drops the staging table correctly (staging left: []).

**Fix.** At the start of each ClickHouse write, drop `<table>__tab_stg_%` tables in the same database whose `metadata_modification_time` is older than the run's hard time limit. Optionally have the gateway revoke with signal SIGUSR1, as previews already do (tasks.py:137), so the task's finally blocks run.

*Verification:* confirmed (bassa); confirmed (bassa).

#### `gap-1-2-6` — ClickHouse destination fails on table or database names that need quoting, after it has already created an empty target

Gravità **bassa** (proposta: bassa), sforzo S. Corsia `gap-1-2`.

Dove: `backend/app/ingest/db_destination.py:170-178`

**Description.** CREATE TABLE uses backtick-quoted names, but the staging insert passes `stg_name` (line 172: the bare name or 'db.table' joined without quotes) to `client.insert_arrow`. clickhouse-connect then builds `INSERT INTO {table} (...) FORMAT Arrow` without quoting the table name (httpclient.py raw_insert). Any name that is not a plain identifier (a hyphen, non-ASCII letters, a leading digit, spaces) is accepted by the UI and the gateway, gets created, and then makes the insert fail with a syntax error. The target table created at line 175 stays behind, empty. Schema-qualified plain names work: 'analytics.sales' written from a connection whose database is 'default' landed in analytics.sales for both append and replace. Postgres accepts 'sales-2024' fine.

**Impact.** Valid ClickHouse names such as `sales-2024`, `team-a.sales` or `ventes_é` cannot be targets. Each attempt fails after the full transform has run and leaves an empty table in the customer database.

**Evidence.** 'sales-2024' → 'Syntax error: failed at position 18 ('-'): -2024__tab_stg_d5713cd9 (`customer`, ...) FORMAT Arrow', after which system.tables has ('sales-2024', 0 rows). 'team-a.sales' → 'Syntax error ... ('-'): -a.sales__tab_stg_2712d458'. 'ventes_é' → 'Syntax error ... ('\xc3')'. 'analytics.sales' → OK, 2 rows in analytics.sales, no stray table in default. Postgres 'sales-2024' → OK, 2 rows.

**Fix.** Pass the backtick-quoted `stg_qualified` to insert_arrow. Because it contains a '.' when qualified, and no database argument is given otherwise, clickhouse-connect uses it verbatim.

*Verification:* confirmed (bassa); confirmed (bassa).

#### `gap-1-2-8` — Naive-UTC timestamps are not pinned to UTC at the destination: ClickHouse shows them in server-local time, and a MySQL TIMESTAMP column stores a wrong instant

Gravità **bassa** (proposta: bassa), sforzo S. Corsia `gap-1-2`.

Dove: `backend/app/ingest/db_destination.py:82-89`, `backend/app/ingest/db_destination.py:202-210`

**Description.** The project standard says naive datetimes are UTC. Postgres stores the parquet wall clock in TIMESTAMP, and a customer TIMESTAMPTZ column gets the correct instant. MySQL DATETIME(6) keeps the wall clock. ClickHouse columns are created as DateTime64(6) without a timezone (line 87). The stored instant is right, but the column renders and parses literals in the server's timezone. The same output therefore reads 00:00 in Postgres and MySQL and 02:00 in a ClickHouse server set to Europe/Rome, and filtering with the parquet's literal value finds nothing. Tabularia's own ClickHouse reader round-trips correctly. The MySQL writer never sets the session time_zone (lines 202-210). When the customer's column is TIMESTAMP, the most common MySQL type for instants, MySQL converts the naive value from the server's local zone, so the stored instant is off by the UTC offset (1 h in winter, 2 h in summer for Europe/Rome).

**Impact.** Business users and SQL in a ClickHouse server set to a non-UTC zone see shifted times and miss rows when they filter with the values Tabularia shows. Customer MySQL TIMESTAMP columns get instants that are 1-2 h wrong.

**Evidence.** ClickHouse 24.8 with server timezone Europe/Rome; parquet epoch µs [1705322096123456, 1719792000000000, 1711848600000000]. Stored toUnixTimestamp64Micro values are identical (instant OK). toString(ts) gives '2024-01-15 13:34:56.123456', '2024-07-01 02:00:00.000000', '2024-03-31 03:30:00.000000'. `WHERE ts = '2024-07-01 00:00:00'` → 0 rows. Round trip through db_source._batches_clickhouse returns the original naive values. Postgres TIMESTAMP: '2024-07-01 00:00:00', epoch 1719792000. MySQL 8.4 (TZ Europe/Rome) DATETIME(6): '2024-07-01 00:00:00.000000'. A customer TIMESTAMP(6) column: UNIX_TIMESTAMP 1705318496.123456 (parquet 1705322096 → -3600 s) and 1719784800 (parquet 1719792000 → -7200 s).

**Fix.** Create ClickHouse columns as DateTime64(6, 'UTC') (Nullable). Open the MySQL connection with init_command="SET time_zone = '+00:00'" so naive UTC values are interpreted as UTC.

*Verification:* confirmed (bassa); confirmed (bassa).

#### `gap-1-2-9` — Oracle and SQL Server connections are offered as Output destinations, but the writer has no code for them and the run fails after the transform with the bare error 'oracle'

Gravità **bassa** (proposta: bassa), sforzo S. Corsia `gap-1-2`.

Dove: `frontend/components/ui/NodePanel.vue:545`, `gateway/app/routes/runs.py:278-287`, `backend/app/ingest/db_destination.py:292-298`, `backend/app/ingest/db_destination.py:334-335`

**Description.** The README (lines 81-82) says Oracle and SQL Server are sources only. However, the Output node's connection picker filters out only s3, smtp and sharepoint (NodePanel.vue:545), and the gateway rejects only s3, smtp and sharepoint (runs.py:278-287). The backend then raises a raw KeyError, outside the try that turns driver errors into readable messages: `_IDENT_QUOTE['sqlserver']` at line 334 via _qualified_table, or `_WRITERS['oracle']` at line 335. This happens after the full transform has already run.

**Impact.** Users can build and schedule a flow that can never succeed. Every run spends the full transform and then fails with the message "'oracle'" or "'sqlserver'", which explains nothing.

**Evidence.** write_parquet_to_db with db_type='oracle' → KeyError "'oracle'". With db_type='sqlserver' → KeyError "'sqlserver'". The backend's /tasks status returns str(result.info) as the run error (tasks.py:328), so the run history shows just the quoted dialect name.

**Fix.** Filter the connection list to the dialects in _WRITERS in NodePanel and FlowEditor validation. Reject other db_types with a 422 in runs.py. In the backend, raise DbDestinationError for an unsupported db_type before the transform starts.

*Verification:* confirmed (bassa); confirmed (bassa).

#### `gap-1-2-10` — post_sql is split on every ';', which breaks statements containing ';' in string literals or procedural blocks

Gravità **bassa** (proposta: bassa), sforzo S. Corsia `gap-1-2`.

Dove: `backend/app/ingest/db_destination.py:64-65`, `frontend/i18n/locales/en.gen.ts:835`

**Description.** `_post_statements` runs `post_sql.split(';')` (line 64). Any ';' inside a quoted string, a comment, a dollar-quoted DO block or a function body cuts the statement. The UI tells users to separate statements with ';', with no warning about this. On Postgres the failure rolls everything back. On ClickHouse it fails after the data is committed (see the separate post-SQL finding).

**Impact.** Legitimate post-load statements, such as an UPDATE that sets a text containing ';' or a Postgres DO block, cannot be used. The error the user gets ('unterminated quoted string') points at their SQL, not at the splitter.

**Evidence.** _post_statements("UPDATE post_t SET region = 'north;east' WHERE customer = 'alice'") → ["UPDATE post_t SET region = 'north", "east' WHERE customer = 'alice'"]. Postgres: 'unterminated quoted string at or near "'north" (SQLSTATE 42601)'. 'DO $$ BEGIN UPDATE ...; UPDATE ...; END $$' → three fragments, giving 'unterminated dollar-quoted string'. ClickHouse ALTER ... UPDATE with 'north;east' → 'Single quoted string is not closed'.

**Fix.** Split with a SQL-aware tokenizer. sqlglot, already used by sql_guard, can split per dialect (sqlglot.parse(..., read=dialect)), with a fallback to sending the whole text as one statement when parsing fails. Or let users enter one statement per line or list item.

*Verification:* confirmed (bassa); confirmed (bassa).

#### `gap-1-2-4` — Postgres 'Replace' holds an ACCESS EXCLUSIVE lock on the customer table for the whole load: every reader blocks, and a long reader in front makes them queue

Gravità **bassa** (proposta: media), sforzo M. Corsia `gap-1-2`.

Dove: `backend/app/ingest/db_destination.py:131-143`

**Description.** Replace runs TRUNCATE, then the full adbc_ingest COPY, then post_sql, all in one transaction (lines 133-143). TRUNCATE takes an ACCESS EXCLUSIVE lock that is held until commit. Every SELECT on the target from any other session (BI dashboards, reports, other ETL) waits for the entire load. If a reader is already running when the replace starts, TRUNCATE waits behind it, and every new reader queues behind the TRUNCATE (Postgres lock queue). The transaction does protect the old data on failure, which is good. The cost is a full read outage of the table that grows linearly with output size and network distance to the customer's database.

**Impact.** Each scheduled replace into Postgres makes the customer's table unreadable for the whole load: measured 0.93 s for 1M rows x 4 columns and 6.4 s for 3M rows x 8 columns (313 MB) on a local Docker network. A remote customer database over a WAN is several times slower. Dashboards hitting the table hang for that long. A slow query already running extends the outage for everyone behind it.

**Evidence.** Throwaway postgres:16-alpine. With 1M rows x 4 columns, pg_locks during replace shows ('AccessExclusiveLock', granted, 'TRUNCATE TABLE "big"'); `SELECT count(*)` issued 0.06 s after the start returned after 0.93 s, replace total 0.95 s. With 3M rows x 8 columns, a point query `WHERE id = 42` issued 0.16 s after the start returned after 6.41 s, replace total 6.17 s. With a 2 s reader already running, pg_locks shows the AccessShareLock granted and the AccessExclusiveLock waiting; a point query issued 0.43 s after the start returned after 8.37 s.

**Fix.** Load into a staging table outside the lock (`CREATE TABLE stg (LIKE target INCLUDING ALL)`, COPY into stg), then swap in a short transaction: `LOCK target; TRUNCATE target; INSERT INTO target SELECT * FROM stg` server-side, or a rename swap where no views or FKs depend on the table. Alternatively replace TRUNCATE with `DELETE FROM target`, as the MySQL path already does (db_destination.py:217-223): readers keep seeing the old rows until commit, at the price of bloat that a post-ANALYZE/VACUUM handles. Setting a lock_timeout on the TRUNCATE would avoid queueing behind long readers.

*Verification:* confirmed (media); confirmed (bassa).

#### `gap-1-2-7` — Destinations need DDL rights even to append to an existing table: Postgres needs CREATE on the schema and TRUNCATE for replace, ClickHouse needs CREATE and DROP TABLE on the database

Gravità **bassa** (proposta: bassa), sforzo S. Corsia `gap-1-2`.

Dove: `backend/app/ingest/db_destination.py:133-135`, `backend/app/ingest/db_destination.py:175-176`

**Description.** Every write starts with `CREATE TABLE IF NOT EXISTS`, even when the table exists. Postgres checks CREATE on the schema before it checks whether the table exists, so an account holding only INSERT on the target table cannot append. Replace uses TRUNCATE, a separate privilege from DELETE. ClickHouse checks the CREATE TABLE grant for the IF NOT EXISTS on the target, and append also creates and drops a staging table, which needs CREATE TABLE and DROP TABLE on the whole database. The customer's DBA therefore has to grant schema- or database-wide DDL to the Tabularia account just to insert rows. Nothing in the UI hint ('The table is created if it doesn't exist') or the docs says so.

**Impact.** Least-privilege service accounts, the norm for writing into customer databases, fail with permission errors after the full transform has run. The workaround widens the account to create and drop any table in the schema or database.

**Evidence.** Postgres 16, role writer with USAGE on schema public and SELECT, INSERT on lp: append fails with 'permission denied for schema public (SQLSTATE 42501)'. After GRANT CREATE ON SCHEMA public, append works (2 rows). Replace with INSERT, SELECT, DELETE fails with 'permission denied for table lp' (TRUNCATE). ClickHouse 24.8, user with SELECT, INSERT ON default.lp: 'necessary to have the grant CREATE TABLE ON default.lp'. After GRANT CREATE TABLE ON default.lp: 'necessary to have the grant CREATE TABLE ON default.lp__tab_stg_c172c2d1'.

**Fix.** Check existence first (to_regclass on Postgres, system.tables on ClickHouse) and run CREATE only when the table is missing. Document the grants each dialect and mode needs. On Postgres replace, consider DELETE instead of TRUNCATE (see the lock finding).

*Verification:* confirmed (bassa); confirmed (bassa).

#### `gap-1-2-11` — The DB destination writer has no test against a real database; the mocked unit tests could not have caught any of the defects above

Gravità **bassa** (proposta: bassa), sforzo M. Corsia `gap-1-2`.

Dove: `backend/tests/test_db_destination.py:1-178`

**Description.** test_db_destination.py covers only pure helpers (name quoting, DDL strings, type mapping, splitting, chunking) plus two MySQL tests with a recording fake connection. No test writes to a real Postgres, ClickHouse or MySQL. Column matching on append, the ClickHouse EXCHANGE semantics, staging cleanup, name quoting through clickhouse-connect, timestamp semantics and privileges were never exercised. The project already runs throwaway containers for source tests (oracletest, mssqltest, chtest).

**Impact.** Data-correctness regressions in writes to customer databases ship unnoticed. The two high-severity ClickHouse defects above show the gap.

**Evidence.** All 15 tests in the file use either no connection or monkeypatched pymysql.connect. Every scenario in this review needed a real server to show the behaviour (for example, ADBC append matches by name while ClickHouse INSERT ... SELECT * matches by position).

**Fix.** Add an integration test module that is skipped unless a test DSN is set, run against throwaway postgres:16-alpine and clickhouse-server containers. Cover append with reorder, rename and added columns, replace preserving the definition, mid-stream failure leaving no staging table, post-SQL failure, names that need quoting, and the timestamp round trip.

*Verification:* confirmed (bassa); confirmed (bassa).

### P2 — Esecuzione dei flussi e dei gruppi: niente sovrapposizioni, stessi risultati da ogni pulsante

#### `background-loops-2` — An output still queued or running after the 10-minute wait cap does not stop the flow; the orchestration closes and frees the flow for a second, overlapping run

Gravità **alta** (proposta: alta), sforzo M. Corsia `background-loops`, segnalato anche da `correctness-core`.

Dove: `gateway/app/services/orchestrator.py:165-177`, `gateway/app/services/orchestrator.py:219-247`, `gateway/app/services/orchestrator.py:76-79`, `gateway/app/core/config.py:445-455`, `gateway/app/services/orchestrator.py:349-355`, `gateway/app/services/scheduler.py:170-178`, `gateway/app/routes/runs.py:797-801`

**Description.** `_wait_run` polls an output's run at most OUTPUT_WAIT_TICKS times: 200 polls × 3 s, about 600 s. It then returns the run still PENDING or STARTED. `orchestrate` handles that exactly like a failed output: it appends 'output: run N PENDING' to the error list and moves on to the next node in sequence. The config comment and module docstring promise the opposite ("oltre il tetto il flusso aborta invece di leggere dati stantii"; without the wait "un output 'a valle' leggerebbe dati non ancora prodotti").
The cap counts from launch, so time spent waiting in the Celery queue is included. The run's own stale timer deliberately starts at engine_started_at (runs.py:797-801) and allows 3900 s, while the Celery hard limit is 3600 s.
When the orchestration then closes as FAILURE, the partial unique index (live = STARTED orchestration) and `flusso_in_corso` (which only looks at orchestration rows) both let the next scheduled slot or a 'Run now' queue the same flow again. Meanwhile the abandoned output task is still queued or writing.

**Impact.** Who sees this: outputs that run longer than 10 minutes, or that wait more than 10 minutes for a worker. prodtest runs CELERY__WORKER_CONCURRENCY=2 with one worker replica. If 20 flows fire at :00 with 1-minute outputs, about the 20th output starts after roughly 10 minutes.
In those cases:
- Downstream nodes in the sequence (outputs, runflow) read data not yet written.
- The run history and webhooks report flow.run.failed, and a failure email goes out, while the output later succeeds and publishes.
- The next schedule slot can start a second execution of the same flow while the first one's output is still writing. For an append or replace into a database destination, that means duplicated or racing writes. Flow-group cells inherit the same behaviour.

**Evidence.** A scratch test copied to the scratchpad (tests/test_zz_review_wait_cap.py, run with the gateway dev image) uses two sequenced outputs, `_reconcile` faked to keep returning PENDING and OUTPUT_WAIT_TICKS=2. It printed `launched: ['first', 'reads_first'] errors: ['output: run 1 PENDING —', 'output: run 2 PENDING —']` and `first output status when the orchestration returns: PENDING`, and passed. Live settings in tabularia-orchestrator-1: OUTPUT_WAIT_TICKS 200, interval 3.0 s, STALE_AFTER_SECONDS 3900.

**Fix.** 1. When an output is still non-terminal after the cap, stop the sequence the way a refresh does: raise OrchestrationError.
2. Count the cap from engine_started_at, not from launch, and default it to at least the run stale timeout.
3. Do not close the orchestration, and so free the one-live-run guard, while a child run it launched is still non-terminal. Either keep waiting until the child is terminal or revoke it (`_revoke_task`) before closing.

*Verification:* confirmed (alta); confirmed (alta).

#### `background-loops-3` — Flow-group cells ignore running orchestrations when counting MAX_CONCURRENT, and the orchestrator's pool is smaller than that budget; when the pool runs out, the whole event loop freezes for 30 s

Gravità **media** (proposta: media), sforzo S. Corsia `background-loops`, segnalato anche da `flow-groups`, `quality-architecture`.

Dove: `gateway/app/services/flow_group_runner.py:159-161`, `gateway/app/services/orchestrator.py:676`, `gateway/app/core/config.py:461-466`, `gateway/app/db/session.py:53-62`, `infrastructure/production/docker-compose.yml:283-290`, `gateway/app/services/orchestrator.py:450-484`, `gateway/app/services/orchestrator.py:370`, `gateway/app/services/flow_group_runner.py:225-226`

**Description.** Owner decision: cells count in ORCHESTRATOR__MAX_CONCURRENT. `coda_loop` honours it: it claims MAX_CONCURRENT - len(_in_corso) - celle_in_corso. But `_run_cell` only waits while `celle_in_corso >= MAX_CONCURRENT`, so a group step starts up to 20 cells even when the process already runs 19 orchestrations. That is 39 concurrent DB users.
Each orchestration or cell keeps a pooled connection checked out during every engine call (30 s timeout). Some paths briefly need a second connection while the first is still held:
- `esegui` calls `_finalize_orch_run`, which opens its own Session and, for failed scheduled runs, awaits a 60 s /db/notify call holding both.
- `_flow_cell` reads child.id and then cells.set → write.
The orchestrator pool, as configured by the production compose (APP__GATEWAY_THREADS=12, no DB__POOL_SIZE), is 12 + 10 overflow = 22 with a 30 s timeout. That is already close to MAX_CONCURRENT=20, even though config.py:461-465 says the latter must stay below the pool size.
All these checkouts are synchronous calls on the asyncio loop. Once the pool is empty, the next checkout blocks the loop itself, so the coroutines holding connections can never release them. Everything stalls for pool_timeout and then that coroutine gets TimeoutError.

**Impact.** When the pool runs out (a wide group step plus a busy orchestrator, or a slow engine, for example a backend under preview load):
- The orchestrator stops for 30 s at a time.
- The coroutine that times out fails. A cell becomes 'internal error (TimeoutError)' and the group stops. If `_finalize_orch_run` times out twice, the task dies without closing its run, and the orphan sweep later records 'Orchestrazione interrotta'. A flow whose outputs succeeded is then reported FAILURE, with webhook and email.
The heartbeat thread has its own engine, so the stuck runs keep looking alive meanwhile.

**Evidence.** - A scratch script with the real `_run_cell`, MAX_CONCURRENT=3 and 3 orchestrations already in `_in_corso` printed `queue loop would claim: 0` and `cells started while full: 3 peak concurrent (orchestrations + cells): 6`.
- Live tabularia-orchestrator-1: `pool 12 overflow 10 timeout 30.0 read pool 3 MAX_CONCURRENT 20`.
- A generic check against a scratch Postgres (pool_size=2, max_overflow=0, pool_timeout=3) had two coroutines hold a Session across `await asyncio.sleep(1)`. A third checkout on the same loop `failed after 3.00s: TimeoutError`, and another coroutine on the loop saw a 3.10 s gap.

**Fix.** 1. In `_run_cell`, wait while `len(orch._in_corso) - 1 + orch.celle_in_corso >= orch.MAX_CONCURRENT` (the group's own run excluded), or share one asyncio.Semaphore between `_avvia` and the cells.
2. For APP__ROLE=orchestrator, size the pool from MAX_CONCURRENT, for example pool_size >= 2 × MAX_CONCURRENT + 4, or refuse to start when it is smaller.
3. Release the outer session (commit/close) before `_finalize_orch_run`, and send the failure notification after both sessions are closed.

*Verification:* confirmed (media); confirmed (media).

#### `quality-architecture-1` — A Run from the editor never makes the S3 copy: the flow-to-run translation is written twice and the two copies already disagree

Gravità **media** (proposta: alta), sforzo M. Corsia `quality-architecture`.

Dove: `frontend/components/FlowEditor.vue:1624-1694`, `frontend/components/FlowEditor.vue:1662-1668`, `frontend/composables/useFlowModel.ts:127-169`, `gateway/app/services/flow_resolver.py:324-384`, `gateway/app/services/flow_resolver.py:378-384`, `gateway/app/routes/runs.py:315-357`

**Description.** Two separate pieces of code turn a flow on the canvas into a run request. The browser does it in TypeScript (resolveChain/operationFor/containerBody, then describeOutput/emailSpecOf/executeOutputRuns in FlowEditor). The gateway does it in Python (flow_resolver._Resolver and _output_body), and scheduled runs, Run now, flow groups and the dbt export all use the Python one. The only thing keeping them aligned is a set of comments ('Speculare a…', 'tenerli allineati…'). They have already drifted. _output_body adds `mirror` (copy to external S3) when data.mirrorEnabled && mirrorConnectionId. The editor's executeOutputRuns builds `publish` without `mirror`, and no frontend file except NodePanel mentions mirror. Commit 6c045d9, which added the mirror, says manual runs go 'through the same resolve_output_request', but the editor's Run button does not. node.data is `any`, and the frontend has no tests, so nothing catches this kind of drift.

**Impact.** Take an Output node that publishes a datasource with 'Also copy the file to external S3' turned on. Clicking Run in the editor (the most common manual action) publishes the datasource and the run shows SUCCESS with mirror=null. The external copy at the agreed path is not refreshed, and the panel's own hint promises 'Overwritten at every run'. The same flow does make the copy when run from the schedule, from Run now in production (pages/flows.vue:84) or from a flow group. Anyone downstream reading the agreed path gets stale data and no warning. Every future field on the Output node has the same chance of drifting.

**Evidence.** FlowEditor.vue:1662-1668: `publish = { name, project_id, description, overwrite, sort_keys }`, with no mirror. flow_resolver.py:378-384: `if d.get("mirrorEnabled") and d.get("mirrorConnectionId"): body["mirror"] = {...}`. `grep -rn mirror frontend --include=*.vue --include=*.ts` matches only NodePanel.vue:363-397 (the form). `git show 6c045d9 -- frontend/components/FlowEditor.vue` is empty: the commit never touched the editor. The gateway accepts the field (schemas/models.py:524 RunCreate.mirror), so the editor never sending it is the whole bug.

**Fix.** Now (S): in executeOutputRuns, publish branch, add `mirror: d.mirrorEnabled && d.mirrorConnectionId ? { connection_id: d.mirrorConnectionId, bucket: (d.mirrorBucket ?? '').trim(), key: (d.mirrorKey ?? '').trim() } : null` and pass it to runsApi.launch. Add `mirror` to the RunBody type in useRuns.ts.
Structural (M): run saved flows from the server's copy. Add `POST /flows/{id}/outputs/run` (or a flag on run-now) that calls build_output_run_requests(definition, …, engine_mode="development") and _launch_flow_run for each Output. In the editor, save the flow if it has unsaved changes (the Run button already requires a saved flow), call that endpoint, and delete the payload building in executeOutputRuns plus emailSpecOf. The TypeScript resolveChain then serves previews only. Pin the remaining shared rules with golden fixtures (flow definition JSON → expected RunCreate bodies) checked by pytest now and by a TS test once one exists.

*Verification:* confirmed (media); confirmed (media).

#### `frontend-correctness-1` — Editor Run ignores unsaved edits when the flow has Refresh or Run-flow nodes

Gravità **media** (proposta: media), sforzo S. Corsia `frontend-correctness`.

Dove: `frontend/components/FlowEditor.vue:1449`, `frontend/components/FlowEditor.vue:1533`, `frontend/components/FlowEditor.vue:1537`, `gateway/app/routes/flows.py:632`, `gateway/app/services/orchestrator.py:202`, `frontend/pages/flow-group.vue:470`

**Description.** When the canvas has a control node, run() goes straight to executeOrchestration(), which calls POST /flows/{id}/run-now. The orchestrator then reads flow.definition from Postgres (orchestrator.py:202), so it runs the last saved version. Nothing checks `dirty` first. Flows that have only Output nodes behave differently: executeOutputRuns sends the live canvas operations. So whether Run uses what is on screen depends on which node types the flow contains. The flow-group page gets this right: its Run button is disabled while dirty, with a 'save first' title.

**Impact.** A user edits a filter or adds an Output in a flow that also has a Refresh node, then clicks Run without saving. The server refreshes and publishes using the old definition, and the editor still reports 'orchestration completed'. Wrong data gets published with no sign that anything went wrong.

**Evidence.** FlowEditor.vue:1449-1460: `if (hasControlNodes()) { if (flowId.value === null) {...} executeOrchestration(); return }` has no dirty check. FlowEditor.vue:1537: `await flowsApi.runNow(flowId.value!)`. orchestrator.py:202: `definition = json.loads(flow.definition or "{}")`. flow-group.vue:470-476 disables Run with `dirty || running || starting`.

**Fix.** In run(), when dirty is true and the flow has control nodes, either save first ('Save and run') or disable Run with the saveFirst title, as flow-group.vue does. Ctrl+S already shares the same saveFlow().

*Verification:* confirmed (media); confirmed (media).

#### `measure-1` — DuckDB and BigQuery compute still reject EXTRACT(... FROM ...), so 4 of the 11 Sample flows cannot preview or run on duckdb

Gravità **media** (proposta: media), sforzo S. Corsia `measure`.

Dove: `backend/app/engine/duckdb_ops.py:277-298`, `backend/app/engine/bigquery_ops.py:321-355`, `backend/app/engine/espressioni.py:53`, `backend/app/engine/chdb_ops.py:312`

**Description.** op_compute in duckdb_ops (and in bigquery_ops) still uses the old word regex `\b(?:select|from|...)\b` (_FORBIDDEN_IN_EXPR). The sqlglot-based guard `espressione_vietata`, introduced on 2026-10-10 because the word regex rejected `EXTRACT(YEAR FROM data)`, is wired only into chdb_ops and clickhouse. So any compute that contains `FROM` inside a scalar function is refused on duckdb and bigquery, for previews and for runs. This covers EXTRACT(YEAR FROM d), TRIM(BOTH ' ' FROM x) and SUBSTRING(x FROM 1 FOR 3).

**Impact.** duckdb is the engine the owner now wants to use because it is lighter on memory. On duckdb, 4 of the 11 Sample flows break from their compute node downstream: Margin by Category and Month, Quantities by Channel and Depot, Order Fulfilment by Month, and Customer Acquisition by Region and Year (36% of the samples). The same flows work on polars. This breaks the cross-engine data standard.

**Evidence.** I copied the 11 Sample flows with engine=duckdb and sent POST /api/tasks/preview for every operation node, one at a time. 16 nodes returned 400 "compute: espressione di 'anno_mese' non consentita (niente sottoquery, FROM o funzioni di lettura file …)": flow Margin… calc/agg/pct/ord, Quantities… mese/piv/ord, Order Fulfilment… all 4 nodes, Customer Acquisition… all 5 nodes. The same Order Fulfilment chain on polars returned 200 in 49-136 ms. Inside the backend container, espressione_vietata('EXTRACT(YEAR FROM data_ordine) * 100 + EXTRACT(MONTH FROM data_ordine)', 'duckdb') returns False, so the precise guard would accept the expression.

**Fix.** In duckdb_ops.op_compute replace the regex with espressioni.espressione_vietata(expr, "duckdb"); its function blacklist already covers read_*, parquet_scan and scan_*. In bigquery_ops use espressione_vietata(expr, "bigquery"), adding external_query, ml.* and ai.* to the blacklist if needed. Add an oracle case with EXTRACT … FROM and TRIM … FROM on every engine.

*Verification:* confirmed (media); confirmed (media).

#### `quality-architecture-3` — Deleting a flow, a datasource or an SMTP connection fails with a 500 because foreign keys are off in every gateway test

Gravità **media** (proposta: media), sforzo M. Corsia `quality-architecture`, segnalato anche da `correctness-core`, `correctness-core`.

Dove: `gateway/tests/conftest.py:43-52`, `gateway/tests/conftest.py:165`, `gateway/app/routes/flows.py:595-619`, `gateway/app/routes/datasources.py:533-566`, `gateway/app/models/saved_view.py:43-46`, `gateway/app/routes/connections.py:402-422`, `gateway/app/models/flow.py:45`, `gateway/app/models/run.py:87`

**Description.** The gateway test database is SQLite with foreign keys off ('FK non forzate da SQLite' in conftest), and the factories depend on that. On Postgres every foreign key is NO ACTION, and three delete routes hit one:
(1) delete_flow deletes the flow's runs, but its orchestration run is the parent_run_id of ingest runs started by Refresh nodes (orchestrator.py:148) and of sub-flow output runs (runflow). Those runs have a different flow_id or none, so a flow that has ever run a Refresh or Run-flow node can never be deleted. delete_flow_group already detaches parent_run_id for this reason.
(2) delete_datasource detaches runs but not saved_views.datasource_id, which is NOT NULL with an FK. Its model comment says the view should survive the datasource and say so; instead the datasource cannot be deleted.
(3) delete_connection checks only datasources, not flows.notify_connection_id, which flows.py:688-692 sets for failure notices.
There is no IntegrityError handler, so each case is a 500.

**Impact.** Users cannot delete these objects from the UI and get an opaque server error. Today 3 of the 22 datasources in prodtest have saved views and cannot be deleted. Any flow using a Refresh or Run-flow node becomes undeletable after its first run, with no API to clear the blocking rows. More generally, no gateway test can catch a missing cleanup on a delete path.

**Evidence.** Postgres catalog (pg_constraint, SELECT only): runs_parent_run_id_fkey, saved_views_datasource_id_fkey and flows_notify_connection_id_fkey all have confdeltype='a' (NO ACTION). Scratch tests with PRAGMA foreign_keys=ON in a throwaway gateway:dev container: delete_flow with an ingest child run → IntegrityError FOREIGN KEY constraint failed; delete_datasource with a SavedView → IntegrityError; delete_connection referenced by Flow.notify_connection_id → IntegrityError. A flow without such children deletes fine (control case passed). prodtest: SELECT count(DISTINCT datasource_id) FROM saved_views = 3. Turning on FKs in a copy of conftest makes tests/test_run_integrity.py fail 13 of 17, because make_run inserts flow_id=1 with no flow (tests/test_flow_versioning.py passes 7 of 7).

**Fix.** delete_flow: before deleting, `UPDATE runs SET parent_run_id=NULL WHERE parent_run_id IN (SELECT id FROM runs WHERE flow_id=:id)`, as flow_groups.py:247-250 does.
delete_datasource: either migration 0007 making saved_views.datasource_id nullable and setting it NULL (the behaviour the model comment describes), or a 409 that names the views.
delete_connection: a 409 (or clear the notice) when flows.notify_connection_id points at it.
Then turn on `PRAGMA foreign_keys=ON` in the db_engine fixture, make the factories create missing parents (make_run/make_flow/make_datasource), and add a generic test like test_delete_user_references: for each FK into flows, datasources, connections and runs, the matching delete route either clears it or refuses with a 4xx.

*Verification:* confirmed (media); confirmed (media).

#### `flow-groups-3` — A refresh cell in a later step accepts an import that started before the previous step ended (stale data, group shows success)

Gravità **media** (proposta: media), sforzo S. Corsia `flow-groups`.

Dove: `gateway/app/services/flow_group_runner.py:272`, `gateway/app/services/orchestrator.py:141`, `gateway/app/services/orchestrator.py:148`

**Description.** Fix #2 (stale or development runs must not count) covers flow cells only. `_refresh_cell` calls `orch._refresh_and_wait`. If the datasource's latest ingest run is not terminal, that function waits for it whatever its start time (orchestrator.py:141-148), and `_refresh_cell` reports success with that run. Take a common ELT layout: step 1 runs a flow whose Output writes a DB table, and step 2 refreshes a datasource that imports that table. An import launched by the datasource's own schedule, a user, or another group while step 1 was still writing ends up standing in for step 2. It may have read the table before step 1 finished, yet the group ends SUCCESS. The cell also records `waited: False` although it waited, so the editor does not show the 'waited' marker the composable documents.

**Impact.** Silent stale or partial data downstream of a green group run, in exactly the steps a group exists to order.

**Evidence.** Scratch test: group [[flow A], [refresh D]] with an ingest of D in STARTED status that began 10 minutes before the group. Result: group status SUCCESS, refresh cell {'state': 'succeeded', 'run_id': <the old import>, 'waited': False}, and launch_ingest_run was never called.

**Fix.** Apply the flow-cell rule to refresh cells. When step n>0 finds an import in progress whose started_at (or engine_started_at) is before `since`, wait for it to end and then launch a fresh import. For the first step, keep the current behaviour. Set `waited=True` when the cell takes the outcome of an import it did not launch.

*Verification:* confirmed (media); confirmed (media).

#### `flow-groups-4` — The run poll returns 50 runs with full cell grids every 2.5 s while any run is live

Gravità **bassa** (proposta: bassa), sforzo S. Corsia `flow-groups`.

Dove: `gateway/app/routes/flow_groups.py:378`, `gateway/app/routes/flow_groups.py:342`, `frontend/pages/flow-group.vue:355`, `frontend/pages/flow-group.vue:377`, `frontend/pages/flow-group.vue:260`

**Description.** While a run is queued or running, the editor polls GET /flow-groups/{id}/runs every 2.5 s. Each answer has the last 50 runs, each with its complete group_cells grid. Per poll the server also runs about 12 queries: the group, ensure_can (about 4 queries), the runs, 2 IN lookups, and readable_project_ids, which loads every Permission row. The page only needs the cells of the shown run (usually the live one) and a short history header. Also, every open of the page, including read-only viewers who never see the catalog, downloads the whole readable catalog: GET /flows and GET /datasources, unpaginated.

**Impact.** Measured in-process (SQLite, no network) for one poll of 50 runs: 3x3 grid 47 KB and 5.0 ms median; 10x4 grid 185 KB and 11.5 ms; 20x10 grid 898 KB and 38.9 ms. With 30 viewers on groups with a live run, that is 12 polls/s: about 2.2 MB/s and 0.14 CPU-s/s for 10x4 groups, or 10.8 MB/s and 0.47 CPU-s/s for 20x10 groups, across the gateways. The browser also re-parses and re-renders up to 10k cell objects every 2.5 s. On prodtest, an empty runs poll takes 8.1 ms median, /datasources 10.6 ms for 24 KB with 22 datasources.

**Evidence.** Scratch benchmark test_zz_review_runs_poll_cost.py (list_flow_group_runs + TypeAdapter dump_json): body sizes 47,392 / 184,952 / 897,501 bytes; median 5.0 / 11.5 / 38.9 ms. On prodtest, 5 requests each: list-by-folder 7.4 ms, detail 7.4 ms, runs 8.1 ms, /flows 8.9 ms (5.7 KB), /datasources 10.6 ms (24.4 KB).

**Fix.** Poll only the live run (GET /flow-groups/{id}/runs/{run_id} already exists). Have the list endpoint return run headers without `cells`, and fetch a past run's cells when the user selects it. Load the catalog only when can_edit is true.

*Verification:* confirmed (bassa); refuted (bassa); confirmed (bassa, tie-break).

#### `flow-groups-5` — Deleting a group and the scheduler take the same two locks in opposite order (Postgres deadlock)

Gravità **bassa** (proposta: bassa), sforzo S. Corsia `flow-groups`.

Dove: `gateway/app/routes/flow_groups.py:253`, `gateway/app/routes/flow_groups.py:265`, `gateway/app/services/scheduler.py:222`, `gateway/app/services/scheduler.py:230`, `gateway/app/services/scheduler.py:311`

**Description.** DELETE /flow-groups/{id} first takes the group's advisory lock (`in_line_for_the_group`), then deletes the flow_groups row, which needs a FOR UPDATE row lock. `_fire_group` first UPDATEs the same row's next_run_at, which takes a FOR NO KEY UPDATE row lock, and only then asks for the advisory lock. If a delete and the due slot meet, Postgres detects a deadlock and aborts one transaction. If the delete loses, the user gets a 500. If the scheduler loses, the `_tick` handler calls `_advance_group` on the same session without a rollback, whose Postgres transaction is aborted. That call likely fails too, so the exception leaves `_tick` and that tick skips the remaining due groups, the contract sweep and notifications, the shared-state cleanup and the blob sweep. Run-now does not deadlock with the scheduler, because the FK check's KEY SHARE is compatible with NO KEY UPDATE. Flows do not have this cycle, because flow delete takes no advisory lock.

**Impact.** Rare: it needs a delete to coincide with the group's due slot. It recovers on the next tick or retry, but the user sees a 500 or one scheduler tick is lost.

**Evidence.** Throwaway postgres:16 container, no network, replaying both lock orders: the scheduler transaction got 'ERROR: deadlock detected ... waits for ExclusiveLock on advisory lock [..,74610002,1,2]; blocked by process 72', and the delete committed.

**Fix.** In `_fire_group`, call `orchestrator.in_line_for_the_group` before the conditional UPDATE, in the same order as delete and run-now. Roll back the session in the `_tick` except path before `_advance_group`.

*Verification:* confirmed (bassa); confirmed (bassa).

#### `flow-groups-6` — The admin Performance tab lumps every flow-group run into one 'deleted flow' row

Gravità **bassa** (proposta: bassa), sforzo S. Corsia `flow-groups`.

Dove: `gateway/app/routes/performance.py:50`, `gateway/app/routes/performance.py:52`, `frontend/pages/performance.vue:120`

**Description.** runs_performance buckets every non-ingest run by ("flow", run.flow_id). A group run is kind=orchestration with flow_id None, so all group runs from every group fall into one bucket with name None, which the page labels 'deleted'. Whole-group durations go into that bucket's total_s, the sort key, so it can top the list. The query also loads entire Run rows, now including the group_cells text, for up to 20,000 runs.

**Impact.** Admins watching performance see a misleading 'deleted flow' entry that hides per-group timings and can rank first. Minor extra memory per Performance request.

**Evidence.** In performance.py:50-57, `kind = "ingest" if r.kind == "ingest" else "flow"; ident = r.flow_id` gives ("flow", None) for group runs, and the name is looked up in the flows map, so it is None. performance.vue:120 shows `i.name ?? $t('performance.deleted')`.

**Fix.** Add a 'group' kind bucketed by flow_group_id with names from FlowGroup, or exclude group runs. Select only the needed columns instead of whole Run rows.

*Verification:* confirmed (bassa); confirmed (bassa).

#### `correctness-core-6` — Two overlapping run closes on one datasource can let the older snapshot win and leave the newer parquet orphaned

Gravità **bassa** (proposta: bassa), sforzo S. Corsia `correctness-core`.

Dove: `gateway/app/routes/runs.py:918-963`, `gateway/app/routes/runs.py:966-1016`, `gateway/app/routes/runs.py:711-719`, `gateway/app/routes/datasources.py:392-402`

**Description.** `_finalize_ingest` and the overwrite branch of `_publish_datasource` read `ds.snapshot_run_id` with a plain SELECT, run `_is_stale_swap`, then UPDATE the row through the ORM. The datasource row is not locked and the UPDATE has no condition on snapshot_run_id. If two reconciles of two different runs for the same datasource overlap (three gateway replicas plus the orchestrators all reconcile), the older run can read the pre-swap state, block on the newer run's row lock, then overwrite it after the newer run commits. Two refreshes of one datasource can exist at once because `refresh_datasource`, the scheduler and `_refresh_and_wait` all check-then-launch without a lock.

**Impact.** This is rare: the transactions must overlap by a few milliseconds. When it happens, the datasource serves the older data even though `_is_stale_swap` exists to prevent exactly that. The newer parquet is never scheduled for deletion, and the superseded one is scheduled twice.

**Evidence.** Scratch test test_swap_race.py on throwaway Postgres with the real `_finalize_ingest` in two threads: run 11 flushes its swap, run 10 reads and then blocks, run 11 commits, then run 10 commits. Final state: `key=datasets/1/k10.parquet snapshot_run_id=1` (the older run), and pending deletions are `['datasets/1/k9.parquet','datasets/1/k9.parquet']`, so k11 is orphaned.

**Fix.** In `_finalize_ingest` and in the overwrite branch, load the datasource with `with_for_update()` (SELECT … FOR UPDATE) before the stale check. Alternatively, do the swap as a conditional UPDATE: `WHERE id = :ds AND (snapshot_run_id IS NULL OR snapshot_run_id < :run)`, and fall back to scheduling deletion of the run's own output when no row matches.

*Verification:* confirmed (bassa); confirmed (bassa).

#### `gateway-queries-11` — Schedule load heatmap ignores flow-group schedules

Gravità **bassa** (proposta: bassa), sforzo S. Corsia `gateway-queries`.

Dove: `gateway/app/services/schedule_load.py:102-111`, `gateway/app/models/flow_group.py:42-44`

**Description.** compute_schedule_load collects schedules only from Flow.run_schedule and Datasource.refresh_schedule. FlowGroup.run_schedule, from the new flow groups, is never projected, even though one group fire launches several flows and refreshes at once and counts in ORCHESTRATOR__MAX_CONCURRENT.

**Impact.** The Flows page heatmap and the collision list under-report load at exactly the times when grouped runs pile up, so an admin cannot see crowded time slots caused by flow groups.

**Evidence.** schedule_load.py:104-111 iterates select(Flow)... and select(Datasource)... only; the FlowGroup model has run_schedule and next_run_at, and scheduler.py:299-311 fires them.

**Fix.** Add the readable flow groups with run_schedule set as kind 'group', with the same cron expansion, and update the frontend legend.

*Verification:* confirmed (bassa); confirmed (bassa).

### P3 — Sicurezza

#### `sec-secrets-infra-2` — A CONNECT-only user can retarget a saved connection and exfiltrate its stored password

Gravità **alta** (proposta: alta), sforzo M. Corsia `sec-secrets-infra`.

Dove: `gateway/app/routes/connections.py:355`, `gateway/app/routes/connections.py:461`, `backend/app/ingest/db_source.py:221`

**Description.** update_connection lets anyone with the CONNECT capability on the folder change host/port/username without re-supplying the password; the stored Fernet-encrypted password is kept. test_connection (also only CONNECT) then has the engine open a connection to the new host, decrypting and sending that password. The UI only exposes has_password, so the user never 'sees' the secret, but they can make the engine hand it to a server they control.

**Impact.** A low-privilege data analyst (CONNECT is commonly granted to use DB sources) steals the production database credentials set by an admin on a shared connection, by pointing the connection at an attacker-controlled host and clicking Test. Privilege escalation / credential theft of the most sensitive secrets in the system.

**Evidence.** Gateway test (tabularia-gateway:dev, 1 passed): admin creates a Postgres connection with password 'Prod-Secret-Value-123'; an analyst holding only Capability.CONNECT calls update_connection(host='attacker.example') and the decrypted engine payload still carries the original password. A fake Postgres server (AuthenticationCleartextPassword) then captured that exact password sent by the engine's _batches_postgresql connect path.

**Fix.** When host/port/username change on a connection that has a stored secret, require the password to be re-entered (or clear it), or require a higher capability (EDIT/MANAGE) to retarget a connection with a stored secret. Record the retarget in the audit log with old/new host.

*Verification:* confirmed (alta); confirmed (alta).

#### `sec-secrets-infra-1` — Polars SQL-node file-function blocklist is bypassable (arbitrary file read on the worker, cross-tenant data)

Gravità **alta** (proposta: alta), sforzo M. Corsia `sec-secrets-infra`.

Dove: `backend/app/engine/operations.py:554`, `backend/app/engine/operations.py:571`

**Description.** op_sql blocks file-reading table functions with a single regex `\b(?:read|scan)_(?:csv|parquet|ipc|ndjson|json)\s*\(`. Polars SQL accepts the same call written so the regex no longer matches: a block comment between name and paren `read_csv/**/('...')`, a quoted identifier `"read_csv"('...')`, or a line comment `read_csv --\n('...')`. All three parse and execute. The documented sandbox claim (AUDIT-2026-09-19: "Polars knows only the registered frame") no longer holds. Because the known-open issue "engine policy is not applied to preview/export" lets a user pick the polars engine even when an admin disabled it, any user with EDIT on a folder can run such a preview.

**Impact.** A flow author reads any file the worker process (uid 10001) can read. The worker /tmp is shared by all preview/run tasks in the same container: source parquet downloads, CSV/XLSX exports and DuckDB spill of OTHER users land there, so this is cross-tenant data disclosure. Process environment (/proc/self/environ) and outbound HTTP were NOT reachable in testing, so env-held secrets are not directly exposed, but glob reads of other tenants' in-flight datasets are.

**Evidence.** Measured in a throwaway tabularia-backend:dev container (polars 1.41.2): `SELECT * FROM read_csv('/tmp/x.csv')` is blocked, but `read_csv/**/(...)`, `"read_csv"(...)` and `read_csv --\n(...)` all returned the file contents; `SELECT * FROM read_parquet/**/('/tmp/**/*.parquet')` returned rows from a parquet written by a different 'user'. /proc/self/environ -> os error 19; HTTP URL -> 'Range request not supported'.

**Fix.** Do not rely on a textual regex against a SQL string. Validate the parsed query: reject any function/table reference that is not a column or the registered `self`/`input` frame (e.g. walk the Polars/sqlglot AST), or strip comments and normalise identifiers before matching. The DuckDB node's engine-level lockdown (enable_external_access=false + lock_configuration=true) is the right model; Polars has no equivalent, so AST-level validation is required.

*Verification:* confirmed (alta); confirmed (alta).

#### `sec-injection-1` — Flow and saved-view writes accept an unbounded JSON body, letting any EDIT user OOM-kill gateway replicas

Gravità **alta** (proposta: alta), sforzo S. Corsia `sec-injection`.

Dove: `gateway/app/routes/flows.py:50`, `gateway/app/routes/flows.py:98`, `gateway/app/routes/flows.py:232`, `gateway/app/schemas/models.py:350`, `gateway/app/schemas/models.py:186`, `gateway/app/routes/proxy.py:148`

**Description.** The engine proxy routes carefully cap request bodies at 8MB (proxy.py:148 MAX_JSON_BODY_BYTES, with a streamed re-check), but the flow and saved-view write routes do not. FlowCreate.definition / FlowUpdate.definition (schemas/models.py:350,370) and SavedViewCreate.spec (schemas/models.py:186) are plain str with no max_length, bound through a normal pydantic body parameter (flows.py:50 create_flow / update_flow). Starlette buffers the whole body into memory for .json(); pydantic keeps definition as a second copy; _authorize_definition_keys runs json.loads(definition) producing a third in-memory tree (flows.py:98-110); and on update _snapshot_version writes a full extra copy into flow_versions (flows.py:232). No upstream cap exists either: the production Caddyfile declares no request_body max_size and uvicorn imposes none (docker-compose.yml:146). Any user with EDIT on a single folder can send this.

**Impact.** A single request with a ~100-150MB definition exceeds the 1g gateway replica mem_limit (docker-compose.yml:150) and the uvicorn worker is OOM-killed, dropping every in-flight request on that replica; a few concurrent large saves cycle all 3 replicas, taking login/catalog/runs/queue down intermittently. This is the exact crash class the owner just hit, triggerable on purpose by one low-privilege user, and it defeats the 'fast at tens of users' target.

**Evidence.** Measured in the gateway:dev image (mem 1200m): a 26.2MB flow body parsed via json.loads(body)->FlowUpdate->json.loads(definition) peaked at 248MB RSS (0.57s); a 78.5MB body peaked at 620MB RSS (1.79s). Confirmed no max_length on definition/spec and no Caddy/uvicorn body cap; contrast proxy.py:148 which does cap at 8MB.

**Fix.** Apply a Field(max_length=...) to definition and spec (a few MB is generous for a DAG), and/or add a request-body size limit at Caddy (request_body max_size) and a Starlette middleware that rejects oversized Content-Length before buffering. Reuse the same 8MB bound the proxy already enforces.

*Verification:* confirmed (alta); confirmed (alta).

#### `quality-architecture-2` — Schedules run with their author's rights, but only moving a datasource or group switches someone else's schedule off; editing a group borrows the author's RUN

Gravità **alta** (proposta: alta), sforzo M, già noto. Corsia `quality-architecture`, segnalato anche da `flow-groups`, `correctness-core`.

Dove: `gateway/app/routes/flow_groups.py:79-104`, `gateway/app/routes/flow_groups.py:206-232`, `gateway/app/routes/flows.py:449-475`, `gateway/app/routes/datasources.py:488-497`, `gateway/app/services/scheduler.py:150-182`, `gateway/app/services/scheduler.py:92-128`, `gateway/app/services/scheduler.py:204-239`, `gateway/app/services/flow_group_runner.py:95-104`

**Description.** A schedule runs with the rights of whoever set it. The rule 'someone else's change voids that authority' is coded by hand in each route, three different ways.
- Datasources: moving one drops someone else's schedule, and every fire re-checks RUN and CONNECT.
- Flow groups: moving drops the schedule, but editing the grid does not. `_checked` lets the editor add any cell whose folder they can only VIEW (readable_project_ids).
- Flows: neither moving nor editing the definition drops the schedule, and `_fire_flow` (unlike `_fire_ds` and `_fire_group`) never re-checks RUN, so a schedule whose author lost RUN keeps queueing failing runs.
So someone with EDIT on a scheduled group's folder and only VIEW on a flow's folder can put that flow into the group. At the next fire it runs with the author's RUN, on production engines, with its real outputs: appends, emails, overwrites. The flow variant is A11, recorded as open in docs/audit/AUDIT-2026-09-13-BIS.md and AUDIT-2026-09-19.md. Flow groups (uncommitted) add a new path to it: from VIEW to running a flow.

**Impact.** A step up in privilege: a VIEW-only user gets a flow run, or a database datasource refreshed (refresh cells check the author's RUN and CONNECT), at the author's next slot, every day. The audit log shows a flow_group.update by the analyst and then runs launched by the author. A flow schedule whose author lost RUN produces a FAILURE run plus failure notices at every slot, while a datasource schedule in the same situation switches itself off.

**Evidence.** Scratch pytest with the repository's own fixtures (gateway/tests copy under scratchpad, app mounted read-only): the author has RUN on Ops and Prod; the analyst has EDIT on Ops and VIEW on Prod. update_flow_group(group, definition adds payroll from Prod, user=analyst) is accepted. The output was 'schedule kept: 0 2 * * * author: True'. Then scheduler._fire_group queues a run with launched_by == author, and has_capability(author, Prod, RUN) is True, which is the check _flow_cell makes (flow_group_runner.py:191). The analyst's own has_capability(..., RUN) is False. Code: flow_groups.py:100 `if obj is None or obj.project_id not in readable` (VIEW); flow_groups.py:228-230 rewrites the definition and leaves run_scheduled_by alone; flows.py:463-468 moves with no schedule handling, while datasources.py:495 and flow_groups.py:216 clear it.

**Fix.** Put the rule in one place: services/schedule_authority.py with `void_foreign_schedule(obj, editor, *, moved, definition_changed) -> bool`. It clears run_schedule/refresh_schedule, *_scheduled_by and next_*_at when someone other than the author moves the object or changes its definition, and it records schedule_cleared in the audit detail. Call it from the PATCH routes of flows, datasources and flow groups.
In flow_groups._checked, require RUN (not VIEW) on the folder of every newly added cell (and CONNECT for refresh cells), the same rights the runner checks.
Make _fire_flow re-check RUN like _fire_ds/_fire_group and switch itself off with a reason.
One parametrized test over (flow, datasource, group) × (move, edit by another user, author lost RUN).

*Verification:* confirmed (alta); confirmed (alta).

#### `sec-authz-1` — Per-flow OpenLineage endpoint leaks names, schema, storage keys and DB host of objects in folders the caller cannot read

Gravità **media** (proposta: media), sforzo M. Corsia `sec-authz`.

Dove: `gateway/app/routes/lineage.py:161`, `gateway/app/routes/lineage.py:170`, `gateway/app/services/openlineage.py:494`, `gateway/app/services/openlineage.py:411`, `gateway/app/services/openlineage.py:440`, `gateway/app/services/openlineage.py:303`, `gateway/app/routes/flows.py:98`

**Description.** GET /flows/{id}/openlineage requires only VIEW on the flow's OWN folder (lineage.py:169). It then calls openlineage.job_events_for_flow -> flow_inputs/flow_outputs, which do session.get(Datasource, datasourceId) and session.get(Connection, connectionId) for every source/output node in the flow definition and emit full metadata with NO check that the referenced object is readable: datasource_dataset() emits the datasource name, full folder path (_project_path walks all ancestors), the current snapshot storage key (ds.key) and every column name/dtype (schema facet); table_dataset()/object_dataset() emit the connection name and `host:port` of the destination database. The flow author can reference any datasource/connection by guessable integer id, because the save-time guard _authorize_definition_keys (flows.py:98-110) only authorizes managed-prefix storage-key STRINGS via collect_storage_keys and never checks datasourceId/connectionId references. So any user with EDIT on even one folder (e.g. their own sandbox) can create a flow pointing datasourceId/connectionId at objects in folders they cannot read, then read that metadata back through the openlineage endpoint. The aggregate /lineage graph endpoint does filter by readable projects and collapses hidden objects to 'Datasource #id' (lineage.py:190, services/lineage.py:92), so this per-flow endpoint is the gap. Data CONTENT stays protected (run/preview/export all go through ensure_can_read_keys + ensure_reads_pinned, which block reads of the leaked key), so the exposure is metadata only.

**Impact.** A low-privileged user enumerates datasource/connection ids (1,2,3...) and recovers, for folders they have no access to: object names, the full folder hierarchy, column names (schema, e.g. an `iban` column on a payroll table), storage object keys, and the hostname:port and connection name of production databases. This is schema + topology + naming disclosure across the RBAC boundary, and on a public demo the low-trust guest can harvest it. No run/preview/export of the data itself is possible with the leaked key.

**Evidence.** Verified in a throwaway tabularia-gateway:dev container (scratch test_zz_review_authz.py::test_openlineage_reveals_objects_of_unreadable_folders, PASSED): an editor of folder 'team-a' created a flow referencing datasourceId and connectionId that live in folder 'finance' (which ensure_can(...,VIEW) denies them), and GET-equivalent flow_openlineage returned JSON containing 'salaries_2026', the folder name 'finance', the storage key datasets/77/...parquet, the column name 'iban', 'pg-finance.internal.corp:6432' and the connection name 'finance-prod'. create_flow accepted the cross-folder references without error, confirming the save-time authorization gap.

**Fix.** In flow_inputs/flow_outputs (or at the lineage route), skip or replace with a 'restricted' placeholder any datasource/connection whose project_id is not in perm_service.readable_project_ids(session, user) / connectable_project_ids, mirroring services/lineage.py's restricted-node handling. Additionally, extend _authorize_definition_keys to resolve datasourceId/connectionId references in the definition and require VIEW (datasource) / the object's folder access at save time, so a flow can never statically reference objects its author cannot see.

*Verification:* confirmed (media); confirmed (media).

#### `sec-authz-2` — Observer role sees unmasked email recipients, DB/webhook hostnames and other personal data in audit-log `detail`

Gravità **media** (proposta: media), sforzo S. Corsia `sec-authz`.

Dove: `gateway/app/routes/audit.py:68`, `gateway/app/routes/audit.py:84`, `gateway/app/routes/runs.py:433`, `gateway/app/services/audit.py:160`

**Description.** The audit list endpoints are readable by observers (router dependency require_observer, audit.py:30) and _to_out(pieno=False) is the masking path for a non-admin observer. It masks actor_label and target_label when they are emails, nulls ip/user_agent, and tokenizes detail['prefix'], but it passes the rest of the `detail` dict through UNCHANGED (audit.py:84-88). Several audit events put personal data and infrastructure topology into detail: EMAIL_SEND records the full list of recipient addresses `to: [...]` (runs.py:436-438, also contract_notifier/notifier), webhook create records `host` (webhooks.py:148), connection create records `host` (connections.py:350), SIEM records destination `changes`, SSO records group/identity detail. AuditEntryOut.detail is returned in the response (audit.py:46), so an observer reads all of it. This contradicts masking.py's stated intent that an observer 'cannot identify the people' and _to_out's own docstring that for an observer 'da dove e con che cosa' disappears.

**Impact.** The least-trusted role (per project memory, the shared Guest account of a public demo) can read real recipient email addresses of reports (including external client domains), production database hostnames, webhook endpoints and SSO group names straight out of the audit feed, defeating the email-masking that protects observers everywhere else.

**Evidence.** Verified in a throwaway tabularia-gateway:dev container (scratch test_zz_review_authz.py::test_observer_sees_email_recipients_and_actor_address_in_audit_detail, PASSED): two EMAIL_SEND events were written by actor alice.rossi@azienda.it; list_audit called as an observer returned actor_label correctly masked to 'a***@azienda.it' but detail still contained the full addresses 'alice.rossi@azienda.it' and 'mario.bianchi@cliente.com'.

**Fix.** In _to_out(pieno=False), redact personal/topology fields from detail before returning: drop or mask known keys such as `to`, `cc`, `recipients`, `host`, and any value containing '@', rather than only `prefix`. A conservative allowlist of detail keys shown to observers is safer than a denylist.

*Verification:* confirmed (media); confirmed (media).

#### `sec-authn-1` — SSO users keep API access after they are disabled or demoted at the IdP: personal tokens last up to 365 days and SSO roles are only re-synced at the next SSO login

Gravità **media** (proposta: alta), sforzo M. Corsia `sec-authn`.

Dove: `gateway/app/deps/auth.py:176-206`, `gateway/app/services/api_tokens.py:101-104`, `gateway/app/core/config.py:90-94`, `gateway/app/routes/api_tokens.py:271-284`, `gateway/app/services/sso.py:404-427`, `docs/design/sso-group-mapping.md:190-196`, `docs/design/sso-group-mapping.md:218-221`, `docs/api.md:19-60`

**Description.** Tabularia learns about an SSO user's state only when that user signs in through SSO. There is no SCIM and no back-channel. For an SSO-only or SSO-linked user, `_utente_del_token` (deps/auth.py:176-206) accepts a personal API token after checking only four things: the token hash, `expires_at`, `user.is_active`, and the `personal_tokens`/observer rules. Nothing records or checks when the user last proved their identity at the IdP. `create_my_token` lets any SSO session create a write token valid for up to `AUTH__API_TOKEN_MAX_DAYS` (365 by default). Roles also come from the IdP only at login: `is_superuser` from `OIDC__SUPERUSER_GROUP` is written to the users row (sso.py:404-405), and with `authoritative` the group memberships are reconciled (sso.py:416-427). A user who never does another SSO login therefore keeps the admin flag and the group memberships they had last time. The SSO design doc sets the offboarding bound at the session TTL: 'disabling the user at the IdP stops new logins but does not revoke a live JWT until it expires' (12 h). docs/api.md says nothing about SSO. The API tokens added on 2026-10-12 broke that bound without any note.

**Impact.** Scenario: an SSO admin or data person creates a personal write token, then leaves the company or is removed from the admin group in Entra/Keycloak. The token keeps working with all their folder permissions, and with admin rights if they were admin, for up to 365 days. That covers reading and exporting every dataset they could see, running flows, creating webhooks and changing schedules. It only stops if a Tabularia administrator also deactivates the account by hand, and nothing in the product or the docs tells them to. This is the usual offboarding gap that enterprise SSO deployments are expected to close.

**Evidence.** Verified in code. deps/auth.py:195-206: the only checks on a token are is_active, personal_tokens, observer-only and scope. No column or query concerns SSO freshness: grep for oidc_subject/oidc_issuer outside services/sso.py and the models finds only migrations and users.py:56 (`sso_only` display). config.py:90 `personal_tokens: bool = True`, :94 `api_token_max_days ... default=365`. sso.py:405 `user.is_superuser = cfg.superuser_group in idp_groups` runs only inside provision_and_sync, which is called only from the SSO callback. The design doc lines 194-196 and 218-221 promise that access ends with the 12 h session. All 36 tests in test_sso.py and 32 in test_api_tokens.py pass, and none of them covers this case.

**Fix.** Store `last_idp_login_at` on users (set in sso.provision_and_sync). In `_utente_del_token`, reject personal tokens of users with `oidc_subject` when that timestamp is older than a configurable window (e.g. OIDC__REAUTH_DAYS, default 7-30). Optionally also cap the expiry of tokens created by SSO users to that window, so renewal requires a fresh SSO login. Service account tokens are unaffected. Then document the bound in docs/api.md and in the SSO doc, and say that immediate offboarding still needs 'deactivate' in Tabularia, which already revokes tokens and sessions (users.py:173-185).

*Verification:* confirmed (media); confirmed (media).

#### `sec-authn-3` — Login response time tells which emails have a password account (about 35 ms vs about 300 ms), and unknown emails are not rate-limited

Gravità **media** (proposta: media), sforzo S. Corsia `sec-authn`.

Dove: `gateway/app/routes/auth.py:40-53`, `gateway/app/services/login_throttle.py:363-404`, `gateway/app/core/security.py:10-19`

**Description.** `login` short-circuits before bcrypt in several cases: `if user is None or user.is_service or not verificata or not verify_password(...)`. Unknown emails, service accounts and SSO-only users (hashed_password NULL) therefore get their 401 after one SELECT. Accounts with a local password get it after a bcrypt cost-12 check (about 250 ms). The message is the same, but the timing differs by a factor of about 8 live and about 45 in-process. The throttle keys on (ip, email). Each probed email is a new key, so enumeration never trips it. One password tried against each enumerated account (spraying) never reaches the 5-failure threshold either.

**Impact.** An unauthenticated client can enumerate which addresses have local password accounts, and tell them apart from SSO-only or non-existent ones, with one request per candidate. That leaks personal data (who uses the tool). It also lets an attacker aim password spraying only at accounts that can be guessed. The minimum length is 6 characters (next finding) and no per-email cap across IPs exists. On a public demo or an Internet-facing install, this is the cheapest first step of an account takeover.

**Evidence.** Measured live on prodtest (http://localhost:8088/api/auth/login). Unknown email zz-review-nobody@review.invalid, 3 tries: 48.9, 35.2, 35.6 ms (401). Admin email with a wrong password, 2 tries: 301.1, 297.0 ms (401). Successful login: 277.5 ms. In-process, in a throwaway tabularia-gateway:dev container with SQLite (median of 4): missing 5.6 ms, SSO-only 6.3 ms, existing account with wrong password 258.3 ms, all with the identical body {"detail":"Email o password errati"}. bcrypt 5.0.0 `$2b$12$` checkpw is 249 ms. The successful admin login reset the throttle row.

**Fix.** Always spend the bcrypt time. At import, compute `_DUMMY = hash_password(secrets.token_urlsafe(16))` and call `verify_password(body.password, _DUMMY)` when the user is missing, is a service account or has no local password, before returning 401. Optionally add a second, looser throttle keyed on email alone (e.g. 20 failures per 15 minutes across all IPs, delay only, no lockout) and one on IP alone, to blunt spraying.

*Verification:* confirmed (media); confirmed (media).

#### `sec-secrets-infra-3` — SMTP credentials sent over unverified TLS (no certificate validation)

Gravità **media** (proposta: media), sforzo S. Corsia `sec-secrets-infra`.

Dove: `backend/app/ingest/email_destination.py:145`, `backend/app/ingest/email_destination.py:231`

**Description.** _spedisci opens smtplib.SMTP_SSL(...) and calls server.starttls() with no ssl context, so Python's default _create_stdlib_context is used: verify_mode=CERT_NONE, check_hostname=False. The connection is encrypted but the server certificate is never validated. Unlike the SQL Server connector (which exposes verify/trust/none in extra.tls), SMTP offers no way to request verification, and tls='none' additionally sends credentials in cleartext.

**Impact.** An active network attacker between the worker and the SMTP relay can present any certificate, terminate the TLS and capture the SMTP username and password sent by server.login(). The SMTP account can then be abused to send mail as the organisation.

**Evidence.** Confirmed in tabularia-backend:dev (Python 3.12.15): smtplib default context has verify_mode 0 and check_hostname False; smtplib.SMTP.starttls and SMTP_SSL default `context=None` to ssl._create_stdlib_context (== _create_unverified_context). No context is passed at either call site.

**Fix.** Pass ssl.create_default_context() to SMTP_SSL(context=...) and starttls(context=...), and add an extra.tls option (verify/trust/none) mirroring the SQL Server connector so self-signed relays are an explicit opt-in rather than the silent default.

*Verification:* confirmed (media); confirmed (media).

#### `quality-architecture-6` — No audit entry for 21 state-changing routes, including folder create/move/delete, user deletion and datasource moves

Gravità **media** (proposta: media), sforzo S. Corsia `quality-architecture`.

Dove: `gateway/app/routes/projects.py:29`, `gateway/app/routes/projects.py:70`, `gateway/app/routes/projects.py:111`, `gateway/app/routes/users.py:219`, `gateway/app/routes/datasources.py:477`, `gateway/app/routes/queue.py:33`, `gateway/app/routes/groups.py:37`, `gateway/app/services/audit.py:22-102`

**Description.** Every catalog object except folders has create/update/delete audit actions: flows, datasources, connections, saved views, flow groups. Folders have none. Yet moving a folder changes the inherited permissions of a whole subtree; that is the A1 escalation fixed on 2026-09-19 by requiring MANAGE. Also unaudited:
- DELETE /users/{id};
- PATCH /datasources/{id}, which moves a datasource and can silently clear someone else's schedule (the equivalent flow and group routes are audited);
- POST /queue/jobs/{id}/stop (an admin killing a worker task);
- POST /groups.
No middleware audits writes generically, so each route has to remember record_audit, and no test enforces it.

**Impact.** After a compromised admin or write token, the audit trail and the Splunk/SIEM forwarding miss exactly the operations that change who can see what (folder moves), remove evidence (user deletion) or disrupt runs (job stop). The docs tell operators to check 'the audit log for the actions carrying its name' after revoking a leaked token, and these actions would not be there.

**Evidence.** AST scan of gateway/app/routes for POST/PUT/PATCH/DELETE handlers whose body (or a local helper) never calls record_audit: 21 routes, including projects.py:29,70,111, users.py:219 (delete_user → elimina_utente, no record_audit), datasources.py:477, queue.py:33 and groups.py:37. `grep -c record_audit gateway/app/routes/projects.py` = 0. services/audit.py defines no PROJECT_*/FOLDER_* action. The user and group part overlaps A7 ('chiuso a metà') in docs/audit/AUDIT-2026-09-19.md; the folder, datasource-move and queue gaps are not recorded there.

**Fix.** Add PROJECT_CREATE/UPDATE/MOVE/DELETE, USER_DELETE, DS_UPDATE (detail: changed fields, moved_to, schedule_cleared) and QUEUE_STOP to services/audit.py and record them in those handlers. Then add a test, in the style of the existing RottaCheRilascia check, that walks app.routes and fails for any POST/PUT/PATCH/DELETE handler that neither calls record_audit nor appears in an explicit allowlist of non-mutating POSTs (previews, connection tests, presence, chat).

*Verification:* confirmed (media); confirmed (media).

#### `gap-1-5-4` — Helm: by default the engine and workers have unrestricted egress; the production example's 0.0.0.0/0 includes the cloud metadata address, and the template cannot exclude it

Gravità **media** (proposta: media), sforzo S. Corsia `gap-1-5`.

Dove: `infrastructure/helm/tabularia/values.yaml:580-583`, `infrastructure/helm/tabularia/templates/networkpolicy.yaml:59`, `infrastructure/helm/tabularia/templates/networkpolicy.yaml:68-72`, `infrastructure/helm/tabularia/templates/networkpolicy.yaml:76-84`, `infrastructure/helm/tabularia/templates/networkpolicy.yaml:86-90`, `infrastructure/helm/tabularia/values-production.example.yaml:131-135`, `docs/deploy/kubernetes.md:94-97`

**Description.** With networkPolicy.enabled true and the default egressCIDRs [], only the engine-ingress policy renders, because the egress policy is wrapped in `if egressCIDRs` (networkpolicy.yaml:59). The engine, workers, preview workers and beat can therefore reach any address, including 169.254.169.254, which the Kubernetes guide says to block. When CIDRs are given, each one renders as a bare `ipBlock.cidr` with no `except` support, so the example's '0.0.0.0/0  # REPLACE' cannot be written as 'everything but the metadata endpoint'. The same policy also allows port 53 to any destination, since the rule has no `to`, and any port on any pod in the namespace (`podSelector: {}`). Its podSelector matches on app.kubernetes.io/component alone, without the name or instance labels the ingress policy uses, so it also restricts other releases' or other apps' pods in the namespace labelled worker, engine or beat.

**Impact.** Egress control is the only network barrier behind connection-based SSRF, a documented limit, and behind the reported Polars SQL file-function bypass. Polars SQL's read_csv fetches http:// URLs, so where pods can reach the node's metadata service, open egress lets that bypass read cloud credentials. Operators who copy the example keep that path open. A namespace shared with another app that uses component=worker gets that app's egress silently cut down to Tabularia's list.

**Evidence.** Default render: one NetworkPolicy (t-tabularia-engine-ingress), no egress policy. Example render: t-tabularia-backend-egress with `ports 53` (no `to`), `podSelector: {}`, `cidr: 10.0.0.0/8` and `cidr: 0.0.0.0/0`. In a --network none container, `pl.SQLContext().execute("SELECT * FROM read_csv('http://169.254.169.254/latest/meta-data/')")` failed with 'object-store error: Generic HTTP error: Error performing HEAD http://169.254.169.254/latest/meta-data/', so Polars attempts the HTTP request. README:330-333 lists unfiltered outbound connections as a known limit; this finding is about the chart's mitigation not delivering the guide's metadata block.

**Fix.** Render the egress policy whenever networkPolicy.enabled is true. Default to DNS restricted to kube-dns plus the configured Postgres, Redis and storage endpoints. Accept egress entries as {cidr, except} and ship `except: [169.254.169.254/32, fd00:ec2::254/128]` on any 0.0.0.0/0 or ::/0 entry. Add name and instance labels to the egress podSelector.

*Verification:* confirmed (media); confirmed (media).

#### `sec-authn-2` — /api/docs and /api/redoc load floating-version CDN scripts with no SRI and no CSP, on the app's own origin, and the API tokens dialog links there

Gravità **bassa** (proposta: media), sforzo S. Corsia `sec-authn`.

Dove: `gateway/app/main.py:144-154`, `frontend/server/plugins/csp.ts:316-322`, `infrastructure/production/Caddyfile:26-63`, `frontend/components/ui/ApiTokensDialog.vue:107`

**Description.** The Nuxt CSP (hash-based script-src, connect-src 'self') is added only in the Nitro `render:html` hook, so it covers only the frontend pages. The same origin also serves FastAPI's default Swagger UI and ReDoc under /api. Those pages are enabled because main.py does not set docs_url/redoc_url to None. They load `https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js` and `redoc@2/bundles/redoc.standalone.js`. These are floating major versions, the tags have no `integrity` attribute, and the response has no Content-Security-Policy, because Caddy adds HSTS, nosniff, XFO and Referrer-Policy but not CSP. The session cookie is HttpOnly, but script running on that origin can still make credentialed same-origin requests, and the anti-CSRF header is a constant ('1'). The 'API tokens' dialog links every user to `${apiBase}/docs`.

**Impact.** Today's bundles are fine, so this is not exploitable now. But a malicious swagger-ui-dist 5.x or redoc 2.x release on npm, or a jsdelivr compromise, would run with the session of every user who opens the API docs, and admins are the most likely visitors. One `fetch('/api/auth/tokens', {method:'POST', credentials:'include', headers:{'X-Tabularia-Csrf':'1','Content-Type':'application/json'}, body:'{"name":"x","scope":"write","expires_in_days":365}'})` returns a 365-day write token. Without a CSP, the script can send it anywhere. The access then outlives the page and the session. The CSP that the 2026-10-08 audit (M7) added for exactly this case does not cover these pages.

**Evidence.** Measured live on prodtest. `curl -D - http://localhost:8088/api/docs` returns 200 with headers Referrer-Policy, Strict-Transport-Security, X-Content-Type-Options and X-Frame-Options, and no Content-Security-Policy. The page loads https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css and swagger-ui-bundle.js, with 0 `integrity` attributes. /api/redoc returns 200 and loads https://cdn.jsdelivr.net/npm/redoc@2/bundles/redoc.standalone.js. For comparison, /login returns `Content-Security-Policy: default-src 'self'; script-src 'self' 'sha256-…' 'sha256-…'; … connect-src 'self'; …`. Related, but a different angle: the 2026-10-08 audit B12 noted only that the docs are public.

**Fix.** Choose one. (a) Disable docs in production: `FastAPI(docs_url=None if production else '/docs', redoc_url=None, …)`, with a flag for owners who want them. (b) Self-host pinned Swagger UI assets: vendor an exact version and serve it with `get_swagger_ui_html(swagger_js_url='/api/static/…', swagger_css_url=…)` and a custom /docs route, drop ReDoc, and add a CSP for `/api/docs*` in Caddy or in a gateway middleware (`script-src 'self' 'sha256-<inline init>'; connect-src 'self'`). At minimum, pin exact versions with SRI hashes.

*Verification:* confirmed (media); confirmed (bassa).

#### `sec-authn-4` — Users cannot change their own password, and the only rule is 6 characters minimum

Gravità **bassa** (proposta: bassa), sforzo M. Corsia `sec-authn`.

Dove: `gateway/app/schemas/models.py:94-107`, `gateway/app/routes/users.py:132-185`, `gateway/app/routes/auth.py:20-204`, `frontend/components/AdminPanel.vue`

**Description.** Only `PATCH /users/{id}` (require_superuser) can set a password. auth.py has no self-service route, and the frontend has password fields only in the admin panel and on the login page. The admin therefore picks every local user's password and keeps knowing it. A user who suspects a compromise cannot rotate it without asking an admin. The policy is `Field(min_length=6)` on UserCreate and UserUpdate, with no maximum, no blocklist and no check against breached passwords. The 72-byte bcrypt 5 limit is the owner-declassified M21 and is not repeated here.

**Impact.** Local accounts (the break-glass admin and every installation without SSO) live with admin-chosen passwords of possibly 6 characters. Actions under a user's name cannot be attributed with confidence when an admin also knows the password. Combined with the enumeration above, short passwords are within reach of slow spraying from a few IPs.

**Evidence.** grep over gateway/app/routes for 'password' finds only auth.py (login), users.py (admin create/update), connections and SIEM/webhooks. grep over frontend/pages, components and composables finds no change-password UI outside AdminPanel.vue and login.vue. The en locale says 'password (min 6)' (i18n/locales/en.gen.ts:153). schemas/models.py:96 `password: str = Field(min_length=6)`, :104 the same for updates.

**Fix.** Add `POST /auth/password` with body {current, new}. Use a session only (refuse API tokens through nega_se_da_token), verify `current` with bcrypt, then call `sessions.chiudi_tutte(..., tranne=current sid)` and write audit. Add a 'Change password' entry in Settings. Raise the minimum to 12 (NIST 800-63B recommends at least 15 for single-factor) and return 422 above 72 bytes instead of 500.

*Verification:* confirmed (bassa); confirmed (bassa).

#### `sec-authn-5` — An IdP group whose name matches a Tabularia admin or observer group still grants that role (AUDIT-2026-09-19 M2 is still open), and personal tokens now make the grant persistent

Gravità **bassa** (proposta: bassa), sforzo S, già noto. Corsia `sec-authn`.

Dove: `gateway/app/services/sso.py:400-427`, `gateway/app/services/sso.py:306-320`

**Description.** `provision_and_sync` maps IdP group names onto Tabularia groups by name. With `OIDC__GROUP_ALLOWLIST` empty (the default) no filter applies, so membership in an `is_admin` group, or in an `is_observer` group (the observer role was added after M2 was recorded), is granted without going through `OIDC__SUPERUSER_GROUP`. No `admin_group_join` audit event is written: the grant appears only inside the SSO_LOGIN `groups_added` detail. New since M2: the user can then create a 365-day personal token, and per the first finding that token keeps the admin rights until the next SSO login, which may never happen.

**Impact.** Exploiting this requires power in the IdP to get a group with the right name onto one's token. Examples are an Entra app assignment, or a Keycloak realm where group names are not tightly controlled. The result is full admin in Tabularia, and with a token it outlives the IdP-side fix.

**Evidence.** Code at sso.py:410-427: `wanted = _group_ids(session, idp_groups, …)`, then `session.add(UserGroupLink(...))` for each group, with no `Group.is_admin`/`is_observer` exclusion and no record_audit of ADMIN_GROUP_JOIN. docs/audit/AUDIT-2026-09-19.md:205 records M2 with the fix 'escludere i gruppi is_admin dalla mappatura SSO'. Line 56 lists it as still open, and the 2026-10-08 report does not mention it.

**Fix.** In `_group_ids` or just after it, drop groups with `is_admin` (and `is_observer`, unless explicitly allowlisted) from `wanted`. IdP-driven admin then goes only through `OIDC__SUPERUSER_GROUP`. Emit ADMIN_GROUP_JOIN/LEAVE audit events from the SSO path as users.py does.

*Verification:* confirmed (bassa); refuted (bassa); confirmed (bassa, tie-break).

#### `sec-authz-3` — GET /tasks/{task_id} proxy performs no per-object authorization (documented debt)

Gravità **media** (proposta: bassa), sforzo M. Corsia `sec-authz`.

Dove: `gateway/app/routes/proxy.py:359`, `gateway/app/routes/proxy.py:9`

**Description.** The data-plane proxy authorizes storage keys on preview/transform/export/upload, but GET /tasks/{task_id} and GET /tasks/operations are left at plain authentication (any logged-in user), as the module docstring states. task_status forwards the engine's TaskStatusResponse, which for a finished transform/export/ingest can contain output_key, rows_written and (on FAILURE) error/error_detail. task_ids are UUID4 and not enumerable, and the backend redacts secrets from error/traceback (tasks.py:324, redact_secrets), so a user would have to know another user's task id to read its status. This is accepted debt, reported for completeness, not a new exposure.

**Impact.** A user who obtains another user's task UUID (not guessable) could read that task's result metadata (output key, row count, redacted error). Low likelihood given UUID4 ids and secret redaction.

**Evidence.** proxy.py:9-11 documents the exception; task_status (proxy.py:359-361) forwards without any object check; backend tasks.py:324 redacts secrets before returning error_detail.

**Fix.** If tightened, correlate task_id to the Run row (runs.task_id) and apply the same VIEW/launched_by check as GET /runs/{id}; otherwise keep as documented debt.

*Verification:* confirmed (media); refuted (bassa); confirmed (media, tie-break).

#### `sec-secrets-infra-5` — Production .env secret files are protected only by a local, uncommitted git exclude

Gravità **bassa** (proposta: media), sforzo S. Corsia `sec-secrets-infra`.

Dove: `.gitignore:2`, `.git/info/exclude:24`

**Description.** The committed .gitignore ignores infrastructure/.env and infrastructure/.env.* but NOT infrastructure/production/.env*. On this machine the production env is ignored only via .git/info/exclude, which is per-clone and never committed. docs/deploy/docker-compose.md instructs operators to run ./gen-env.sh, which writes infrastructure/production/.env containing every generated secret (Fernet key, JWT secret, DB/Redis/admin/Grafana passwords). Other service env files (top-level .env, gateway/.env, backend/.env, e2e/.env, docs/examples/keycloak/gateway.env) are also not ignored.

**Impact.** A fresh clone that follows the documented production deploy creates infrastructure/production/.env; it shows as untracked but NOT ignored, so a routine `git add -A` / `git commit` can publish all production secrets to a public repository. The safeguard that exists on the owner's machine does not travel with the repo.

**Evidence.** git check-ignore -v infrastructure/production/.env.prodtest -> matched by .git/info/exclude:24 (not .gitignore). git check-ignore of .env, gateway/.env, backend/.env, e2e/.env, docs/examples/keycloak/gateway.env all report NOT ignored.

**Fix.** Add to the committed .gitignore: `infrastructure/production/.env`, `infrastructure/production/.env.*`, `!infrastructure/production/.env.example`, and a broad `*.env`/`**/.env` with explicit `!*.example` negations. Do not depend on .git/info/exclude for secret-file coverage.

*Verification:* confirmed (bassa); confirmed (bassa).

### P4 — Memoria del motore: nessun processo che fa cadere gli altri

#### `engine-dataplane-1` — DuckDB runs with no per-process memory or thread budget: every connection assumes it has 80% of the container's memory

Gravità **alta** (proposta: alta), sforzo S. Corsia `engine-dataplane`, segnalato anche da `measure`, `backend-api-tasks`, `infra-perf`.

Dove: `backend/app/engine/duckdb_engine.py:130-142`, `backend/app/engine/duckdb_ops.py:472`, `backend/app/api/routes/tasks.py:198-205`, `backend/app/core/config.py:122-134`, `backend/app/engine/chdb_engine.py:41-44`, `infrastructure/production/docker-compose.yml:389-408`

**Description.** `DuckDBEngine._connection()` opens `duckdb.connect(':memory:')` and only sets `temp_directory` and `TimeZone`. It never sets `memory_limit` or `threads`, and nothing in backend/app, the production compose or the Helm chart does either (grep for memory_limit, SET threads and POLARS_MAX_THREADS finds nothing). DuckDB sizes `memory_limit` from the cgroup for each database instance, not for the container. So each of the 4 forked preview children in the 4 GiB preview worker believes it may use 3.1 GiB and 8 threads. The owner already computed a budget per child: resolve_max_memory_per_child_kb gives 4 GiB × 0.75 / 4 = 768 MB. But that budget is only a Celery recycle threshold checked after the task ends, and DuckDB ignores it. chDB, by contrast, gets an explicit `max_memory_usage`. The same gap applies in three more places: the run worker (6 GiB, concurrency 2, so 4.8 GiB per process); the `sql` sandbox connection (a second instance inside the same preview, see the separate finding); and the export route, which runs `get_engine('duckdb').run(...)` inside the single uvicorn engine-API process.

**Impact.** Hash aggregates and hash joins only spill to disk once a process reaches its 3.1 GiB limit, so 2–4 concurrent previews or runs that aggregate or join a few-million-row dataset exceed the container limit first. The kernel then SIGKILLs a child (WorkerLostError), which can fail another user's preview with a 500 or fail a scheduled run. This is the same failure mode as yesterday's crash, now on the engine the owner is switching to. A DuckDB export in the engine API can OOM-kill the only API process, and every user's in-flight preview goes down with it. With 30 users and 4 preview slots, two heavy previews at once are routine.

**Evidence.** Measured inside tabularia-preview-worker-1: duckdb 1.5.4, a fresh connection reports memory_limit='3.1 GiB', threads=8 and temp_directory='.tmp'; cgroup memory.max is 4294967296 and cpu.max is 'max'. In a throwaway container from tabularia-backend:dev (2 GiB, 4 CPUs, --network none), the real DuckDBEngine.preview ran in 4 forked children like the prefork pool: group_by over 8M rows / 4M groups on a 149 MB parquet with 1M-row row groups, limit 100. With the default settings, 2 of 4 children exited with -9 (OOM) and the survivors peaked at 709–712 MB RSS (3.6 s). With `SET memory_limit='384MB'` added in _connection, 4 of 4 completed with peaks of 492–529 MB (7.1 s, spilling). A single preview that joins 100 rows (8M×8M self-join) peaked at 813 MB, and a single group_by at 715 MB.

**Fix.** In `_connection()`, and in the sandbox connection, set `memory_limit` to cgroup limit × headroom / worker concurrency (reuse cgroup_mem_limit_bytes and resolve_max_memory_per_child_kb, minus about 150 MB for Python, Polars and Arrow). Set `threads` to max(1, cpus // concurrency) and keep temp_directory under TMPDIR. In the engine API process, use a small fixed budget, or move DuckDB and other non-Polars exports to the worker. Add a test asserting `current_setting('memory_limit')` on the engine's connection.

*Verification:* confirmed (alta); confirmed (alta).

#### `engine-dataplane-3` — DuckDB `sql` node copies the entire upstream into an in-memory table in an unbounded sandbox, even for a 100-row preview

Gravità **alta** (proposta: alta), sforzo S. Corsia `engine-dataplane`.

Dove: `backend/app/engine/duckdb_ops.py:455-496`, `backend/app/engine/duckdb_ops.py:469-474`

**Description.** `op_sql` writes the whole upstream relation to parquet (`rel.write_parquet(in_tmp)`). It then runs `CREATE TABLE self AS SELECT * FROM read_parquet(...)` in a new `duckdb.connect(':memory:')`, which loads the entire input into RAM. Only afterwards does it apply `preview_limit` to the result. This sandbox instance has DuckDB's default memory_limit (80% of the container) and the default temp_directory '.tmp'. In production that path is relative to cwd /app, and the root filesystem is read-only (verified: touching /app in the preview worker fails), so the sandbox cannot spill. The preview's main connection is a separate instance with its own 80%.

**Impact.** A preview of an Execute-SQL node costs memory of about 8× the parquet size, whatever the query. In the 4 GiB preview worker, one such preview on a dataset of about 450 MB, or 3–4 concurrent ones on 150 MB datasets, gets SIGKILLed. The user sees an unexplained 500, and other users' previews in the same container are at risk.

**Evidence.** Real DuckDBEngine.preview, op sql `SELECT * FROM self WHERE cat = 3` on an 8M-row, 149 MB parquet: 2.9 s and 1190 MB peak RSS. The plain LIMIT preview of the same file took 0.23 s and 100 MB. In a 1 GiB container run --read-only like production (cwd /app read-only, /tmp writable), the same single preview was SIGKILLed (exit -9) and left 149 MB + 27 MB of temp parquet in /tmp. A group_by preview in the same container completed (742 MB).

**Fix.** Open the sandbox as a file-backed database in TMPDIR (e.g. duckdb.connect(os.path.join(tmpdir, 'sbx.duckdb'))). Set `memory_limit` (the per-process budget) and `temp_directory` before `lock_configuration`, so `self` lives in the buffer manager and spills instead of sitting entirely in RAM. Remove the database file in cleanup.

*Verification:* confirmed (alta); confirmed (alta).

#### `backend-api-tasks-2` — Any user's Excel export of a big view can OOM-kill the whole engine API, which runs exports and upload conversions in-process

Gravità **alta** (proposta: alta), sforzo M. Corsia `backend-api-tasks`.

Dove: `backend/app/api/routes/tasks.py:175-231`, `backend/app/engine/polars_engine.py:225,276-283`, `backend/app/ingest/service.py:34,78-80`, `backend/Dockerfile:102`, `infrastructure/production/docker-compose.yml:337-355`, `frontend/pages/viewer.vue:226,538`

**Description.** `POST /tasks/export` is a sync route that runs the engine inside the single uvicorn process of the engine API. For xlsx it collects up to XLSX_MAX_ROWS=1,048,575 rows in memory and writes them with Polars `write_excel` (xlsxwriter, pure Python, all cells in RAM). For an engine other than Polars it first runs the whole flow in-process (DuckDB with the 3.1 GiB default from the DuckDB limits finding above), uploads a snapshot to S3, then downloads it again with Polars. Uploads of 50 MB or less are also converted synchronously in the same process (`pl.read_excel` / `pl.read_json`). Closing the browser does not stop the work, so a retry doubles it.

**Impact.** The Viewer's Excel button lets anyone with VIEW access trigger this. At the 20-column shape measured, about 800k rows (about 320k at 50 columns) exceeds the 4 GiB backend limit. The kernel then kills uvicorn, the only process in the container, and every in-flight preview (up to 200 waiting threads), upload, export and task-status poll fails for all users until the container restarts. Previews already running on workers become orphans (see the time-limit finding below). Even without an OOM, one export holds the GIL long enough to add tens to hundreds of ms to every other request in that process for minutes.

**Evidence.** Measured in tabularia-backend:latest with --cpus 2. write_excel of 100k x 20: 17.2 s, maxrss 574 MB. 250k x 20: 52.1 s, maxrss 1,317 MB. That is linear at about 4.95 MB and 0.23 s per 1k rows, so the 1,048,575-row cap is about 5.3 GB and about 4 min. During the write, a second thread's 5 ms sleeps were delayed p99 21-35 ms, max 175-566 ms (GIL). Upload path: a 14.7 MB xlsx (150k x 20) read with pl.read_excel took 2.0 s and peaked at 260 MB RSS (baseline 43 MB), so a 50 MB sync upload is about 0.8 GB inside the API. Backend CMD is a single `uvicorn app.api.main:app` with no --workers; mem_limit BACKEND_MEM_LIMIT=4g.

**Fix.** Move exports, and sync conversions above a few MB, out of the API process. Run them as Celery tasks on a dedicated queue with its own memory cap that write the file to /tmp or S3, and have the API stream the finished file. Short of that: lower the xlsx cap substantially (for example 200k rows, or by cell count), write with xlsxwriter constant_memory, and add a process-wide semaphore of 1 for xlsx and DuckDB exports.

*Verification:* confirmed (alta); confirmed (alta).

#### `sec-injection-3` — Synchronous in-process xlsx/csv ingest (<=50MB) can drive the shared backend data-plane API to multi-GB RSS

Gravità **media** (proposta: media), sforzo M. Corsia `sec-injection`, segnalato anche da `engine-dataplane`.

Dove: `backend/app/ingest/service.py:34`, `backend/app/ingest/service.py:78`, `backend/app/ingest/converters.py:78`, `backend/app/api/routes/files.py:46`, `backend/app/api/routes/tasks.py:175`

**Description.** Uploads up to SYNC_THRESHOLD_BYTES = 50MB are converted to parquet synchronously inside the backend FastAPI process (service.py:34,78; files.py:46), and xlsx conversion reads the entire workbook into memory via pl.read_excel (converters.py:78-80) with no row/cell cap. xlsx is a zip, so a <=50MB file can decompress to tens of millions of cells. Crucially this runs in the same backend API process that also serves /tasks/export synchronously (tasks.py:175) and proxies the data plane, so an OOM there is shared-fate. The gateway /files route only requires EDIT and streams the body through without a size check (proxy.py:364).

**Impact.** A user with upload rights can send a compact (<=50MB) but highly amplifying xlsx; the backend API process (mem_limit 4g, typically 1 replica) spikes and can be OOM-killed, taking down uploads and direct exports for everyone until it restarts. Concurrent uploads stack. This is a distinct path from the already-known unbounded CSV export, but the same no-cap theme.

**Evidence.** Measured in backend:dev: reading a 10.7MB xlsx with 400k rows x 8 string columns peaked at 267MB RSS (2.2s). Linear extrapolation to the 50MB sync ceiling is ~1-1.5GB for one file; several in parallel exceed the 4g limit. The sync threshold and in-process pl.read_excel were confirmed in code.

**Fix.** Cap ingest by row/cell count (or decompressed size) and reject or offload above it; lower the synchronous threshold so large/amplifying files always go to the worker; stream/limit xlsx parsing rather than loading the whole frame in the API process.

*Verification:* confirmed (media); confirmed (media).

#### `backend-api-tasks-3` — All DuckDB connections in a container share one spill directory, so concurrent spills fail or read each other's data

Gravità **media** (proposta: alta), sforzo S. Corsia `backend-api-tasks`.

Dove: `backend/app/engine/duckdb_engine.py:135-137`, `backend/app/engine/duckdb_ops.py:472`

**Description.** `_connection()` points every connection's temp_directory at the same fixed path, `os.path.join(tempfile.gettempdir(), 'duckdb')`. DuckDB names its spill files deterministically (duckdb_temp_storage_DEFAULT-0.tmp, ..._S64K-0.tmp, ...). Two database instances spilling at once, whether they are Celery prefork children in the same container or export threads in the API process, write and read the same files.

**Impact.** Today this is masked by the DuckDB limits finding above: with the 80%-of-container default, a container is OOM-killed before two queries spill. It becomes live as soon as per-connection memory limits are set. Concurrent heavy runs, previews, materializations or exports then fail with I/O errors. A query that reads another process's spill pages can also compute on foreign data: in the experiment one query summed foreign bytes until it overflowed, and an aggregate that cannot overflow would return a wrong number instead of failing. A wrong number in a run's output would then be published.

**Evidence.** Two processes in one container, as with prefork children (tabularia-backend:latest, -m 1g, memory_limit 300MB each, temp_directory '/tmp/duckdb' as the engine sets it, 25M-group GROUP BY). Trial 1: 'Out of Range Error: Overflow in HUGEINT addition: -131907362869051640873358273547616469181 + ...' and 'IO Error: Could not read enough bytes from file "/tmp/duckdb/duckdb_temp_storage_S192K-0.tmp"'. Trial 2: same two failures. A count-based variant: 2/2 trials both IO errors. Control with a per-process directory (/tmp/duckdb-<pid>): both correct (25,000,000 groups, expected sum 1249999975000000) in 30 s. Two threads in one process (the API export case) gave the same IO error and HUGEINT overflow.

**Fix.** Give each connection its own spill directory: `d = tempfile.mkdtemp(prefix='duckdb-')`, `SET temp_directory = d` before any query, and `shutil.rmtree(d, ignore_errors=True)` in the finally that closes the connection. Do the same for the sql sandbox (which today has no writable temp_directory at all). Add a regression test with two processes spilling at once.

*Verification:* confirmed (media); confirmed (media).

#### `sec-injection-2` — foreach re-downloads its nested source once per iteration and keeps every temp copy until run end, filling worker /tmp

Gravità **media** (proposta: media), sforzo M. Corsia `sec-injection`.

Dove: `backend/app/engine/operations.py:412`, `backend/app/engine/operations.py:665`, `backend/app/engine/context.py:40`, `backend/app/engine/duckdb_ops.py:500`, `backend/app/engine/duckdb_engine.py:59`

**Description.** A foreach body that contains a join/union (or any op that reads a second source) rebuilds its right side on every iteration: op_foreach re-applies the substituted body per item (operations.py:665-690), and _build_right calls ctx.scan for each one (operations.py:412-428). ctx.scan downloads the parquet to a fresh temp file appended to _temp_paths, which is only cleaned at the very end of the run (context.py:40-71 tempfile/cleanup). The DuckDB engine (now the default) behaves identically (duckdb_ops.py op_foreach -> build_right -> scan -> ctx.tempfile, cleaned only in the finally of preview/run, duckdb_engine.py:59). The per-level cap is 1000 iterations and the cumulative cap is 100000 (operations.py MAX_FOREACH_ITERATIONS / MAX_FOREACH_TOTAL), so one run can stage up to 1000 simultaneous full copies of the same source on disk.

**Impact.** A normal user with RUN capability can build a flow whose foreach (up to 1000 iterations) joins a modest datasource; the worker downloads that source 1000 times and holds all 1000 temp copies at once on its /tmp disk volume (docker-compose.yml:375 is a disk volume, not tmpfs). A 50MB source becomes ~50GB on disk in a single run, exhausting the worker /tmp and failing not just that run but every concurrent task sharing the volume. On the preview-worker the same flow in a preview starves previews for all users.

**Evidence.** Measured with a fake in-memory storage counting downloads: a 1000-item foreach whose body does a left join on a datasource triggered exactly 1000 downloads of the same right-side source, each to its own temp file retained until ctx.cleanup() at run end.

**Fix.** Cache the nested-source scan within a single foreach (download/scan the right side once and reuse the relation across iterations), or clean each iteration's temp files before the next. At minimum, lower the default MAX_FOREACH_ITERATIONS and cap total staged temp bytes per run.

*Verification:* confirmed (media); confirmed (media).

#### `engine-dataplane-7` — Polars foreach collects the entire driver before checking the 1,000-iteration cap

Gravità **bassa** (proposta: bassa), sforzo S. Corsia `engine-dataplane`.

Dove: `backend/app/engine/operations.py:680-686`

**Description.** `driver_df = _build_right(params['driver'], ctx).collect(engine='streaming')` materializes the whole driver and only then checks `height > MAX_FOREACH_ITERATIONS`. The DuckDB version correctly uses `.limit(MAX_FOREACH_ITERATIONS + 1)` (duckdb_ops.py:509). Polars is still the default engine (`DEFAULT_ENGINE = 'polars'`).

**Impact.** Wiring a large dataset as the driver by mistake loads all of it into the preview or run process (millions of rows) just to report 'too many rows'. That is an OOM risk in the preview worker.

**Evidence.** Verified in code only. Polars was not executed, per the review's duckdb-only rule.

**Fix.** `_build_right(...).head(MAX_FOREACH_ITERATIONS + 1).collect(engine='streaming')`.

*Verification:* confirmed (bassa); confirmed (bassa).

#### `engine-dataplane-8` — OOM-killed or hard-time-limited tasks leave full source copies in /tmp, and nothing sweeps them

Gravità **bassa** (proposta: bassa), sforzo S. Corsia `engine-dataplane`.

Dove: `backend/app/engine/duckdb_engine.py:59-62`, `backend/app/engine/duckdb_engine.py:245-247`, `backend/app/engine/context.py:44-47`, `infrastructure/production/docker-compose.yml:393-394`

**Description.** Temp files are removed only in `finally` blocks, which never run on SIGKILL (cgroup OOM, Celery hard time limit, revoke with SIGTERM/SIGKILL). No worker-start sweep of `duckdb_*` or `dataprep_*` files exists (grep found none). /tmp is an anonymous volume, which docker compose carries over when it recreates the container.

**Impact.** Every killed preview or run leaks a full copy of its source, gigabytes for big datasets, in the worker's /tmp volume. That disk is also the DuckDB spill disk, so repeated OOMs make later spills fail.

**Evidence.** After the measured OOM kill of the sql-node preview in a read-only 1 GiB container, /tmp still held duckdb_*.parquet (148,975,355 bytes) plus a 27 MB partial file.

**Fix.** On worker_process_init or worker_ready, delete `duckdb_*`, `dataprep_*` and `chdb_state_*` entries in TMPDIR older than the task time limit. Alternatively give each task its own temp dir and remove stale ones at start.

*Verification:* confirmed (bassa); confirmed (bassa).

#### `infra-perf-2` — Default memory ceilings add up to more than the RAM the guide asks for (17 GiB vs 8 GB; prodtest 20 GiB on a 15.4 GiB host), so no ceiling protects the host or Postgres

Gravità **bassa** (proposta: media), sforzo S. Corsia `infra-perf`.

Dove: `infrastructure/production/docker-compose.yml:150-151`, `infrastructure/production/docker-compose.yml:224-225`, `infrastructure/production/docker-compose.yml:283-293`, `infrastructure/production/docker-compose.yml:354-355`, `infrastructure/production/docker-compose.yml:385-386`, `infrastructure/production/docker-compose.yml:408-409`, `infrastructure/production/docker-compose.yml:423-424`, `infrastructure/production/docker-compose.yml:430-455`

**Description.** The env file itself warns: 'Keep their sum below the machine's RAM: past that, the host picks the victim, and it may well be the database'. The defaults break that rule on the machine the guide recommends. With default replicas the ceilings are gateway 1g + orchestrator 1g (it inherits the gateway's mem_limit, which the guide's count of '16 GB' leaves out) + backend 4g + worker 6g + preview-worker 4g + frontend 512m + beat 512m = 17 GiB. The guide says 8 GB is enough to start. Postgres, Valkey, MinIO, VictoriaMetrics, Grafana and cAdvisor have no ceiling at all. Postgres has no oom_score_adj, so the host OOM killer can pick it. Every ceiling is also a hard memswap ceiling, so the per-container limits only bite after the host has run out.

**Impact.** On any host with less than about 19 GB, a burst of previews and runs exhausts host memory before any container reaches its own limit. The kernel then kills arbitrary processes, including Postgres (outage plus crash recovery), dockerd children, or the desktop session on a single-machine install. With the prodtest sizing (3 gateways and 2 orchestrators) the ceilings reach 20 GiB on a 15.36 GiB host, and engines claim up to 80% of each ceiling per process (see the previous finding).

**Evidence.** `docker stats` total memory is 15.36 GiB; summing the mem_limit values of the running prodtest gives 3x1 + 2x1 + 4 + 6 + 4 + 0.5 + 0.5 = 20 GiB. Unbounded services at idle use Postgres 140 MiB, MinIO 220 MiB, VictoriaMetrics 222 MiB, cAdvisor 166 MiB and the sample ClickHouse 781 MiB. Previous boot's kernel log: memcg kills of python at 1.06 GB and 1.56 GB anon (00:59:11 and 00:59:15), then a host-level OOM (constraint=CONSTRAINT_NONE, invoked by dockerd) at 01:06:09 that killed a desktop process, then a memcg kill of a 4.18 GB python at 01:06:12, then a reboot at 01:08. The development stack was also running at that time, so this shows the mechanism, not prodtest alone.

**Fix.** Make the defaults fit the documented host. For example, at 16 GB RAM: gateway and orchestrator 512m (they use about 130 MiB), backend 2g, worker 4g, preview-worker 3g with concurrency 2, giving about 11 GiB plus about 2 GiB for the unbounded services. Alternatively, have gen-env.sh derive the ceilings from /proc/meminfo and refuse a sum above RAM minus 2 GiB. Give postgres `oom_score_adj: -800` (or a mem_reservation) so a host OOM never picks the metadata database. Fix the guide's arithmetic (17 GiB, the orchestrator counts).

*Verification:* confirmed (bassa); confirmed (media).

#### `gap-1-5-2` — Helm: memory requests are 1/4 to 1/8 of the limits the application sizes itself to, so the scheduler reserves 5-7x less memory than the pods are built to use

Gravità **media** (proposta: media), sforzo S. Corsia `gap-1-5`.

Dove: `infrastructure/helm/tabularia/values.yaml:403-405`, `infrastructure/helm/tabularia/values.yaml:418-420`, `infrastructure/helm/tabularia/values.yaml:438-440`, `infrastructure/helm/tabularia/values.yaml:460-462`, `infrastructure/helm/tabularia/values.yaml:485-487`, `infrastructure/helm/tabularia/values-production.example.yaml:79-101`, `infrastructure/helm/tabularia/templates/_helpers.tpl:188-189`, `backend/app/core/config.py:122-134`

**Description.** This is the chart version of the reported compose finding that memory ceilings add up past the host. In Kubernetes it shows up as requests far below limits. Both the application and DuckDB set their budgets from the container limit: the Celery per-child recycle cap is limit x 0.75 / concurrency (config.py:122-134), and each DuckDB connection takes 80% of the limit (measured 3.1 GiB of 4 GiB, 6.3 GiB of 8 GiB). The scheduler, though, only reserves the request. Rendered totals with default values: 5.12 GiB requested against 36.5 GiB of limits (7.1x). With values-production.example.yaml: 10.12 GiB against 56.5 GiB (5.6x). The run worker reserves 1 GiB of its 8 GiB (2 of 12 in the example), and the preview worker 0.5 of 4 GiB (1 of 4). No anti-affinity or topology spread is set, so replicas of one role can land on the same node.

**Impact.** A 16 GiB, 4-vCPU node accepts 2 run workers, 2 preview workers and 1 engine API: 3.5 GiB and 1.75 CPU requested, 28 GiB of limits. Two concurrent heavy runs plus a few previews push the node, not just a container, out of memory. The kubelet then evicts the pods furthest above their request, which are the run workers. Their in-flight runs die without redelivery (acks_late is off, celery_app.py:64-68), are marked failed only after ENGINE__RUN_STALE_TIMEOUT_SECONDS = 3900 s (docs/deploy/kubernetes.md:174-179), and can leave append outputs half written. If eviction is too slow, the kernel OOM killer picks across the whole node. The example's preview worker (4 children in 4 GiB) is exactly the compose setting that took the host down in this review: Polars previews measured 1.0-1.5 GB each. Its derived per-child cap of 768 MiB does not help, because Celery checks it only after a task ends.

**Evidence.** Rendered with helm v4.2.0 (`helm template`) and tabulated per Deployment. Default: engine 2x(512Mi/4Gi), gateway 2x(256Mi/1Gi), orchestrator 1x(256Mi/1Gi), worker 2x(1Gi/8Gi, conc 2, child cap 3072 MiB, DuckDB 6554 MiB per connection), preview-worker 2x(512Mi/4Gi, conc 2, child cap 1536 MiB, DuckDB 3277 MiB), beat 1x(128Mi/512Mi), frontend 2x(128Mi/512Mi). Example: worker 3x(2Gi/12Gi, child cap 4608 MiB, DuckDB 9830 MiB), preview-worker 2x(1Gi/4Gi, conc 4, child cap 768 MiB). DuckDB 1.5.4 memory_limit in a container capped at 4g: '3.1 GiB'; at 8g: '6.3 GiB'.

**Fix.** Set memory requests equal to limits for worker, preview-worker and engine (Guaranteed QoS for memory), or lower the limits to what the node can actually reserve. Document a node size, e.g. the sum of requests per node for the shipped values. Add a podAntiAffinity or topologySpreadConstraint for the worker roles. In values-production.example.yaml, drop the preview worker's concurrency to 2 or raise its limit to at least concurrency x 1.5 GiB.

*Verification:* confirmed (media); confirmed (media).

#### `gap-1-5-3` — Helm: with no CPU limits, DuckDB and Polars use one thread per node vCPU in every Celery child, and DuckDB's spill ceiling ignores the emptyDir sizeLimit, so a large spill evicts the whole pod

Gravità **bassa** (proposta: media), sforzo M. Corsia `gap-1-5`.

Dove: `infrastructure/helm/tabularia/values.yaml:404-405`, `infrastructure/helm/tabularia/values.yaml:461-465`, `infrastructure/helm/tabularia/values.yaml:486-488`, `infrastructure/helm/tabularia/values-production.example.yaml:86-101`, `infrastructure/helm/tabularia/templates/_helpers.tpl:178-198`, `backend/app/engine/duckdb_engine.py:130-137`

**Description.** This extends the reported finding that DuckDB has no per-process memory or thread budget, and the chart makes it worse in two ways. (1) No workload has a CPU limit, and nothing sets thread counts. The image has no POLARS_MAX_THREADS, and duckdb_engine.py sets only temp_directory and TimeZone. Both libraries read the cgroup CPU quota, so with none set they start one thread per node vCPU in every Celery child. (2) DuckDB's max_temp_directory_size defaults to 90% of the free space of the filesystem holding /tmp. A disk-backed emptyDir sizeLimit is not a filesystem quota: DuckDB sees the node's disk, while the kubelet evicts the pod once the volume passes 20 GiB (preview, engine) or 50/100 GiB (worker). The ephemeral-storage scheduler accounts for none of it either: there are no ephemeral-storage requests, and sizeLimits total 184 GiB (default) or 384 GiB (example).

**Impact.** On a 32-vCPU node, the example's preview pod runs 4 children x 32 DuckDB threads on a 0.5-CPU request. Each connection gets 3.1 GiB / 32, about 100 MiB per thread, below DuckDB's documented minimum of 125 MB per thread, so joins and aggregations spill or fail sooner than on the 8-core compose host (about 400 MiB per thread). Run workers likewise take every core of a shared node. Gateway pods request only 250m of CPU although the values say a replica needs one core. Under contention they are guaranteed a quarter core, so the roughly 2 s of GIL-bound CPU already reported for a Flows page open can stretch toward 8 s. This is computed from CFS weights, not measured. A spill past the emptyDir limit evicts the whole pod rather than failing one query: every task in it dies, including the 2-4 previews or the 2 runs sharing the pod, and runs are reported failed only after 3900 s. Inside one pod, the 2-4 children still share /tmp/duckdb, which is the reported shared-spill problem at pod scope. That part is the same as compose, not worse.

**Evidence.** Probes in throwaway tabularia-backend:dev containers (DuckDB 1.5.4, Polars 1.41.2) on the 8-core host. --memory=4g with no CPU quota: DuckDB threads 8, Polars threads 8, cpu.max 'max 100000'. --memory=4g --cpus=2: DuckDB threads 2, Polars threads 2. With temp_directory=/tmp/duckdb, max_temp_directory_size reads '90% of available disk space'; /tmp free was 1.0 GiB on a 1g tmpfs and 75.7 GiB on a disk-backed volume (the host's free disk), so the ceiling follows the filesystem, not a size limit. Rendered manifests: no `limits.cpu` and no `ephemeral-storage` in any container. Per-pod DuckDB claims: 1.6x the memory limit by default, 3.2x for the example's preview worker.

**Fix.** Add per-role engine settings to the application (DuckDB memory_limit = limit x 0.75 / concurrency, threads, max_temp_directory_size = tmpSizeLimit / concurrency) and have the chart render them per Deployment from resources, concurrency and tmpSizeLimit. Until then, ship CPU limits or POLARS_MAX_THREADS for the worker roles, add ephemeral-storage requests equal to tmpSizeLimit, and raise the gateway's CPU request to 1 to match the documented one core per replica.

*Verification:* confirmed (media); confirmed (bassa).

#### `backend-api-tasks-7` — Previews inherit the 1-hour run time limit and rely on the API to revoke them

Gravità **bassa** (proposta: bassa), sforzo S. Corsia `backend-api-tasks`.

Dove: `backend/app/tasks/celery_app.py:112-123`, `backend/app/api/routes/tasks.py:128-131,151-155`, `backend/app/api/routes/contracts.py:35`

**Description.** `_limiti_di_tempo_in_vigore` stamps the run limits (task_time_limit 3600 s, soft 3300 s) on every message that does not carry its own, including preview_task, contract tasks and materialize_step_task. The API stops waiting after PREVIEW_TIMEOUT_SECONDS=120 and revokes from the same request thread. If the API process dies (as in the Excel-export finding above) or is restarted during a deploy, nothing revokes the previews already running. A slot is reclaimed only when the same user starts another preview in the same panel.

**Impact.** After an API crash or restart, up to 4 heavy orphan previews can occupy every preview slot for up to 55 minutes, and every user's previews time out with 504 meanwhile. This is rare, but it compounds the API OOM described above.

**Evidence.** celery_app.py:120-123 sets headers['timelimit'] = (task_time_limit, task_soft_time_limit) when the message has none. tasks.py:129 send_task(...) passes no time_limit. The revoke at tasks.py:152 is the only stop.

**Fix.** Pass `soft_time_limit=PREVIEW_TIMEOUT_SECONDS+10` and `time_limit=PREVIEW_TIMEOUT_SECONDS+30` to send_task for previews and contract tasks, and a bounded limit for materialize_step_task. The publish hook already keeps limits that a message declares itself.

*Verification:* confirmed (bassa); confirmed (bassa).

### P5 — Anteprime veloci anche sui dati grandi

#### `engine-dataplane-2` — Every preview downloads the whole source parquet (or the whole cached parent) from S3: no reuse across previews, none within one preview

Gravità **alta** (proposta: alta), sforzo M. Corsia `engine-dataplane`, segnalato anche da `backend-api-tasks`.

Dove: `backend/app/engine/duckdb_engine.py:64-73`, `backend/app/engine/context.py:52-56`, `backend/app/engine/chdb_engine.py:108-111`, `backend/app/engine/stepcache_defer.py:435-445`, `backend/app/core/config.py:151`, `backend/app/engine/duckdb_ops.py:342-346`, `backend/app/engine/duckdb_ops.py:533-535`, `backend/app/engine/operations.py:419-433`

**Description.** `DuckContext.scan` and `OperationContext.scan` (Polars) call `storage.download_file` for the whole object into a fresh temp file and delete it at the end of the request. chDB does the same. A LIMIT-100 preview therefore costs O(file size) of S3 transfer plus /tmp writes before DuckDB reads even the first row group. The step cache does not help large data. Any step with more than CACHE__MAX_STEP_ROWS (1M) rows is marked skipped and recomputed from the source, so every preview of a large dataset re-downloads it in full. Within one preview there is no memoization by (bucket, key) either: an aggregate-and-join-back flow downloads the same source twice, and a join inside a foreach body downloads its right side once per iteration (up to 1000). One editor click issues 2–3 previews: the limit-1 column lookup for the parent, the limit-100 preview, and the right side for join/union. The viewer issues 2 per refresh. Every one of them is a full download. Source keys are immutable (gateway/app/routes/runs.py:87 uses a timestamp plus a uuid, and step-cache keys are content-addressed), so a local cache would need no invalidation.

**Impact.** At measured local MinIO speed (170–374 MB/s), a 149 MB dataset costs 0.4–0.9 s of download per preview against 0.06 s of query. The 25M-row, 2.4 GB parquet cited in config.py costs 6.4–14 s per preview, and more on the cloud S3 that the production compose requires. 4 preview slots × at least 6.4 s caps throughput at about 0.6 previews/s. 30 users clicking every 20 s at 2–3 previews per click ask for 3–4.5 previews/s. The queue grows without bound and previews hit the 120 s timeout (504). /tmp also churns 4 × 2.4 GB at once.

**Evidence.** In a throwaway container running the real DuckDBEngine.preview with a fake storage that copies a 149 MB file from local disk: the LIMIT-100 preview took 0.23 s, of which the copy took 0.17 s (74%). A single boto3 download of a 9.7 MB cache object from prodtest MinIO took 26–58 ms (170–374 MB/s, 3 requests). Download counts per preview, counted with the real engine: an aggregate-and-join-back flow made 2 downloads of the same key; foreach with 50 items and a join in the body made 51 downloads, 50 of them the same lookup. A 8M-row self-join preview copied 2 × 149 MB (copy_s 0.45 vs 0.17).

**Fix.** (1) Memoize `scan()` by (bucket, key) inside a context: one download per object per request. (2) Add a read-through LRU file cache per worker container on the /tmp volume, keyed by bucket/key, with a byte budget and hard links into the request's temp dir. This is safe without invalidation because keys never change content. (3) Next step: read with range requests (pyarrow S3FileSystem dataset into DuckDB, or Polars scan_parquet('s3://…')) so LIMIT and schema previews read only the footer and the needed column chunks of the first row group.

*Verification:* confirmed (alta); confirmed (alta).

#### `engine-dataplane-4` — The editor's column lookups run the full chain (limit 1) and queue a materialization just to read column names

Gravità **media** (proposta: media), sforzo S. Corsia `engine-dataplane`.

Dove: `frontend/components/FlowEditor.vue:1129-1146`, `backend/app/api/models.py:67`, `backend/app/engine/duckdb_engine.py:209-244`, `backend/app/engine/polars_engine.py:183-220`

**Description.** `ensureColumns()` sends a normal preview with `limit: 1` and uses only `res.columns`. The engines execute the whole upstream chain to produce one row: full aggregation, hash join build, pivot. With the default use_cache they also defer a materialization of the parent's parent. Both engines can return the schema without executing anything: Polars `LazyFrame.collect_schema()` and DuckDB `rel.columns`/`rel.types`. PreviewRequest forbids limit 0 (ge=1), so there is no schema-only path. The lookup fires for the left parent on every click on an uncached node, for the right side on join/union nodes, and for the driver inside foreach containers. `invalidateColumns()` clears the memo after every edit.

**Impact.** Each click adds 1–3 full executions on top of the real preview, each also a full source download (see the download finding). That roughly doubles preview-pool load during editing at 30 users.

**Evidence.** Real DuckDBEngine on an 8M-row parquet with a group_by node: limit=1 preview took 882 ms with 424 MB peak RSS. Schema only (same chain built, `rel.columns` read) took 254 ms with 132 MB peak, of which about 170–200 ms is the local copy of the file. The gap grows linearly with data size.

**Fix.** Add a schema-only mode (limit 0 or `schema_only: true`). Have the engines return columns from `collect_schema()` or `rel.columns`/`rel.types`, with no fetch and no deferred materialization, and use it from ensureColumns.

*Verification:* confirmed (media); confirmed (media).

#### `frontend-perf-1` — Every editor parameter change wipes all cached node columns, so each preview waits for 1-2 extra serial preview tasks; Output-node text fields start previews on every keystroke

Gravità **media** (proposta: media), sforzo S. Corsia `frontend-perf`, segnalato anche da `frontend-correctness`.

Dove: `frontend/components/FlowEditor.vue:932-942`, `frontend/components/FlowEditor.vue:1125-1127`, `frontend/components/FlowEditor.vue:1129-1145`, `frontend/components/FlowEditor.vue:1149-1227`, `frontend/components/ui/NodePanel.vue:317`, `frontend/components/ui/NodePanel.vue:331`, `frontend/components/ui/NodePanel.vue:385`, `frontend/components/ui/NodePanel.vue:392`

**Description.** patchSelected() calls invalidateColumns() with no argument. That deletes the cached output columns of every non-source node, not only the edited node and the nodes after it. refreshForNode() then has to rebuild the edited node's input columns (its parent's output, which the edit cannot change) with a separate preview request (limit 1, slot 'cols'). It waits for that request before it starts the real preview. Join, union and foreach nodes also refetch the right-hand side, so their edits cost 3 serial preview tasks instead of 1. The cache stays empty after the edit, so the next clicks on other nodes pay the extra request too. On top of that, the Output node's fields (datasource name, description, mirror bucket/key, S3 bucket) send an update on every @input, so typing a name starts a preview of the unchanged parent chain on each keystroke spaced more than 180 ms apart. Each one is aborted by the next (the server revokes the task and kills the query).

**Impact.** Every edit's preview takes one extra preview round trip (two for join/union/foreach), and the preview worker gets 2-3 tasks per edit instead of 1. Above CACHE max_step_rows (1,000,000 rows per step, backend/app/core/config.py:151) the step cache does not apply, so each extra task recomputes the upstream chain from parquet. The preview worker is the resource that was OOM-killed in yesterday's load test. With 30 users editing, the extra tasks are of the same order as the useful ones. Typing a 25-character Output name at normal speed starts about 25-50 preview tasks that are aborted one after another.

**Evidence.** Code: FlowEditor.vue:935 invalidateColumns() → 1125-1127 deletes every non-source key → 1169 `await ensureColumns(leftId)` (cache miss → apiPreview limit 1 'cols') → 1208 the right side for join/union/foreach → 1226 runPreview. NodePanel.vue:317 `@input="emit('update', { name: ... })"` → patchSelected → refreshForNode(output). resolveChain (useFlowModel.ts:152-161) skips output nodes, so that preview is the parent's chain again. Measured on prodtest with duckdb (idle): order_rows group_by preview limit 1 = 50-51 ms, limit 100 = 51-53 ms. So the time from an edit to its preview goes from about 50 ms to about 100 ms for a mid-chain node and about 150 ms for a join, before any queueing on the worker.

**Fix.** Invalidate only the edited node and the nodes after it: reuse the breadth-first search of invalidateStats(fromId) for nodeColumns, because the edited node's input columns never change from its own edit. For patches that do not change data (Output name/description/overwrite/mirror*/email fields, comment text), skip refreshForNode, or debounce on change instead of input. Optionally start runPreview in parallel with the column fetch.

*Verification:* confirmed (media); confirmed (media).

#### `infra-perf-3` — Background step-cache copies run on the 4 interactive preview slots: 39% of the preview worker's measured busy time

Gravità **media** (proposta: media), sforzo S. Corsia `infra-perf`, segnalato anche da `backend-api-tasks`, `engine-dataplane`.

Dove: `backend/app/tasks/jobs.py:105-112`, `backend/app/tasks/celery_app.py:62`, `backend/app/tasks/celery_app.py:81-85`, `backend/app/core/config.py:151`, `infrastructure/production/docker-compose.yml:389-409`

**Description.** After answering, each preview that left uncached steps enqueues `materialize_step_task` explicitly on queue='preview' (jobs.py:110). That is the same queue, and the same 4 prefork children, that serve the interactive clicks. Celery serves the queue FIFO with prefetch 1 and no priority. A materialization recomputes the chain and writes up to CACHE__MAX_STEP_ROWS (1,000,000) rows to object storage. Each one occupies a preview slot and its share of the 4 GiB for as long as an interactive preview would, or longer.

**Impact.** Measured on prodtest during the review: about one materialization per three previews, and they consumed 39% of preview-slot time. The preview worker is the binding resource for tens of editors, so a click arriving behind a few background copies waits for them. On GB-scale data, where a copy takes seconds, this wait adds directly to the latency of every click. In memory it is one more heavy engine process inside the oversubscribed 4 GiB container.

**Evidence.** tabularia-preview-worker-1 logs over 90 minutes: 1369 preview_task (mean 63.5 ms, max 841 ms) and 475 materialize_step_task (mean 117 ms, max 981 ms). Slot time is 87 s for previews and 55.5 s for materializations, so materializations took 39% of busy time. Backend /metrics agrees: 1410 POST /tasks/preview, mean 82 ms, all under 1 s.

**Fix.** Route materialize_step_task to its own queue (for example 'cache') with a small dedicated worker (concurrency 1, its own memory limit), or to the run worker. Alternatively enable Celery priorities on Redis (broker_transport_options priority_steps plus queue_order_strategy='priority') and enqueue copies at the lowest priority. Keep the Valkey lock that already prevents duplicates.

*Verification:* confirmed (media); confirmed (media).

#### `measure-5` — Step cache grows about 60 MB per minute under 20-30 editing users and is kept 24 h; DuckDB writes it as SNAPPY

Gravità **bassa** (proposta: bassa), sforzo S. Corsia `measure`.

Dove: `backend/app/engine/duckdb_engine.py:166-181`, `backend/app/engine/duckdb_engine.py:225`, `backend/app/engine/stepcache_defer.py:52-110`, `backend/app/core/config.py:143-151`

**Description.** Every preview whose parent step is not cached queues a materialize_step_task. That task computes the WHOLE parent step, not only LIMIT rows, and uploads it as parquet to cache/. DuckDB uses `rel.write_parquet(path)` with its default SNAPPY codec, while the datasets are ZSTD. Entries live CACHE__TTL_SECONDS=86400 and the sweep runs hourly; only CACHE__MAX_STEP_ROWS=1,000,000 caps an entry.

**Impact.** With this editing mix the bucket grew by about 3.6 GB/hour for 5.2 MB of source data. This is an upper bound: the virtual users click random nodes of 8 flows, each with a per-user plan. Over a working day that means tens of GB kept for 24 h on the single-machine MinIO. The materialisations were also 30% of the preview queue's tasks; they did not hurt latency at this data size, but each one is a full recomputation of the parent step.

**Evidence.** In the preview-worker logs for the ramp window: 1,078 preview_task and 466 materialize_step_task received. MinIO cache/ (listed through the backend's storage client): 482 objects = 620.9 MB written in the last 30 minutes, avg 1.29 MB, largest 9.7 MB (358,340 rows x 28 columns, SNAPPY, written by DuckDB 1.5.4). All 41 dataset objects total 5.2 MB; order_rows (105,035 x 6) is 1.84 MB in ZSTD.

**Fix.** Write cache parquet with ZSTD (`rel.write_parquet(path, compression='zstd')`, and likewise in Polars sink_parquet). Use a shorter TTL for entries that are never re-read, or a size cap with LRU eviction. Skip materialising parents that are cheap to recompute (small row counts or short compute times).

*Verification:* confirmed (bassa); confirmed (bassa).

#### `backend-api-tasks-4` — Data-contract profiling and checks scan the whole dataset in memory with Polars on the preview queue

Gravità **media** (proposta: media), sforzo M. Corsia `backend-api-tasks`.

Dove: `backend/app/tasks/celery_app.py:84`, `backend/app/api/routes/contracts.py:19,34-51`, `backend/app/contracts/service.py:19-35`, `backend/app/contracts/evaluator.py:336,378-388`, `backend/app/tasks/jobs.py:228-231,369-374,422-426`

**Description.** `profile_dataset_task` and `evaluate_contract_task` are routed to the `preview` queue and wait up to WAIT_SECONDS=120 before revoking. They download the whole parquet and run eager Polars (`lf.select(aggs).collect()`). The profile computes `n_unique()` for every column in one pass, so the hash sets for all columns are held in memory together. The engine the user picked is ignored (always Polars, non-streaming). The same `evaluate` runs inside every run or import that publishes to a datasource with a contract, on the run worker. `_run` never calls `forget()`, so results stay in Valkey for 24 h.

**Impact.** One 'propose a contract' click on a dataset of tens of millions of rows needs more memory than the whole 4 GiB preview container: the child is OOM-killed and the other users' preview children are squeezed. Even when it fits, it holds one of the 4 interactive slots for up to 2 minutes. This is the same failure mode as yesterday's Polars previews, and it stays reachable after switching previews to DuckDB.

**Evidence.** In a fresh dev-image container (-m 3g, --cpus 2), `app.contracts.evaluator.profile` on a 10M-row x 12-column parquet (84 MB on disk) peaked at 2,266 MB RSS (baseline 43 MB) in 7.8 s. The preview worker's per-child budget is 786 MiB (4 GiB x 0.75 / 4), and its container is 4 GiB shared by 4 children. The preview worker log for the current container showed 0 contract tasks, so no live sample was available.

**Fix.** Run profile and evaluate with `collect(engine='streaming')`, or on DuckDB with a bounded memory_limit. Use `approx_n_unique` for profiling. Send manual contract tasks to a separate low-priority queue, or give them their own time limit well below 120 s. Call `forget()` in `contracts._run`.

*Verification:* confirmed (media); confirmed (media).

#### `frontend-perf-2` — The Viewer's hidden chart runs a full-dataset aggregation on every datasource open, and the chart never re-queries after Apply, a datasource switch or a node change

Gravità **media** (proposta: media), sforzo S. Corsia `frontend-perf`.

Dove: `frontend/pages/viewer.vue:566-567`, `frontend/pages/viewer.vue:115`, `frontend/pages/viewer.vue:150-153`, `frontend/pages/viewer.vue:156-162`, `frontend/components/ui/ChartPanel.vue:77-79`, `frontend/components/ui/ChartPanel.vue:164-175`, `frontend/components/FlowEditor.vue:1902`

**Description.** In the Viewer, ChartPanel is mounted with v-show, while the default view is 'table'. When a datasource is picked, baseCols changes and ChartPanel's column watcher sets xCol to cols[0] (xCol starts as ''). That change fires refresh(): with the defaults (bar, count) it runs group_by(first column) count + sort + limit 20 with no_cache: true over the whole dataset. Nobody sees the result. The chart re-queries only when its own controls change. After Apply with new filters, or after switching to a datasource whose first column has the same name, the watcher finds xCol still valid and the chart keeps the old data. The editor's ChartPanel (v-else, not keyed) shows the previous node's chart when another node with the same first column is selected.

**Impact.** Each datasource opened in the Viewer, including opening a saved view, costs 2 preview-worker tasks instead of 1. The hidden one aggregates every row on the first column, which is usually a unique id, so it builds one group per row: the most memory-hungry query shape, with the cache disabled. The second effect is a correctness bug: users see charts that do not match the filters or the datasource on screen.

**Evidence.** Measured on prodtest with duckdb, order_rows and commercial_costs (105,035 rows each, first column 'id' in both): table query (limit 500) 54-63 ms, hidden chart query 55-62 ms. Every open runs both. Because both datasources have 'id' as first column, switching from one to the other leaves the first datasource's chart (ChartPanel.vue:168 only resets xCol when the column is missing).

**Fix.** Mount the chart only when its view is shown (v-if or a lazy component). Pass a revision prop that the page bumps on Apply, datasource change or node change, and re-query on it while the chart is visible. Do not default to grouping by a unique first column: prefer a low-cardinality string column or wait for the user's choice.

*Verification:* confirmed (media); confirmed (media).

#### `engine-dataplane-9` — A run re-downloads its own output once per post-step: contract check, DB destination, S3 copy, email

Gravità **bassa** (proposta: bassa), sforzo M. Corsia `engine-dataplane`.

Dove: `backend/app/tasks/jobs.py:210-331`, `backend/app/contracts/service.py:21`, `backend/app/ingest/db_destination.py:340`, `backend/app/ingest/s3_destination.py:164`, `backend/app/engine/duckdb_engine.py:272-279`

**Description.** The engine writes the output locally, uploads it (and uploads it again into the step cache when it has ≤1M rows), then deletes the local file. `check_before_publish`, `write_parquet_to_db`, `write_output_to_s3` and the email attachment (via export) each download the same object again.

**Impact.** A run with a contract, a DB output and an email transfers its output 1 + 3 extra times. For a multi-GB scheduled output that adds tens of seconds per run and the matching S3 egress.

**Evidence.** Code path verified: one download_file call each at contracts/service.py:21, db_destination.py:340 and s3_destination.py:164, plus email through Engine.export with a DataSource of the output key.

**Fix.** Have run() return or keep the local output path for the duration of transform_data_task, and pass it to the contract, destination, mirror and email steps. Fall back to download only when it is missing.

*Verification:* confirmed (bassa); confirmed (bassa).

### P6 — Gateway e database: query, indici, permessi, compressione

#### `gateway-queries-1` — /lineage loads every run of every readable flow as full ORM rows: 5.5-7 s and about +700 MB per call, enough to OOM-kill a 1 GiB gateway replica

Gravità **alta** (proposta: alta), sforzo S. Corsia `gateway-queries`.

Dove: `gateway/app/services/lineage.py:116-123`, `gateway/app/routes/lineage.py:97`, `frontend/pages/lineage.vue:266`, `frontend/pages/lineage.vue:268-272`, `infrastructure/production/docker-compose.yml:150`

**Description.** build_full_graph keeps only the last run and the last success of each flow, but to get them it runs `select(Run).where(Run.flow_id.in_(flow_ids), Run.kind == 'flow').order_by(Run.started_at)` with no limit. That loads the whole run history of every readable flow as full Run objects, including error_detail (up to 20 KB), destination, mirror and email, and then loops over all of it in Python. Every /lineage call builds the full graph first, including the centred calls (routes/lineage.py:97). The Lineage page makes two such calls one after the other when it opens (lineage.vue:268-272), and makes another each time the centre, direction or depth changes (lineage.vue:266).

**Impact.** Measured on a copy of the prodtest schema holding one year of synthetic history (488K runs, 181K of kind=flow): one call takes 7.0 s for an admin and 5.5 s for a normal user, fetches 182K rows, and raises the gateway process's peak RSS from 89 MB to 783 MB (+694 MB). The gateway container limit is 1 GiB and the idle replicas use 127-134 MiB. A single page open therefore costs about 12 s of a gateway thread. Two calls running at once on the same replica (two users, or one user changing depth while the first call runs) go past 1 GiB and get the replica OOM-killed, which fails every request in flight there. While a call runs it holds the GIL, which slows every other request on that replica. The cost grows linearly with run history.

**Evidence.** bench.py on the synthetic one-year DB (postgres:16 restored from `pg_dump --schema-only` of tabularia-postgres-1, gateway code from the working tree in tabularia-gateway:dev, 2 CPUs): 'GET /lineage (admin, full graph) median 7009.9 ms sql=6 rows=182235'; 'GET /lineage (normal) median 5460.1 ms rows=182359'; mem.py: 'lineage: 6118 ms, RSS before 89 MB, peak 783 MB (+694 MB)'. docker inspect tabularia-gateway-1 Memory=1073741824; docker stats: gateway replicas at 127-134 MiB.

**Fix.** Fetch only one row per flow. Use `SELECT DISTINCT ON (flow_id) flow_id, status, started_at FROM runs WHERE kind='flow' AND flow_id = ANY(:ids) ORDER BY flow_id, started_at DESC` (or a LATERAL ... LIMIT 1 per flow), plus one more for the last SUCCESS, backed by a new index runs(flow_id, started_at DESC). In the throwaway DB, the LATERAL version with that index took 0.6 ms for 50 flows. In the frontend, reuse the graph already loaded for the picker on the first unfocused render instead of calling /lineage twice, or cache the full graph for each user for a few seconds.

*Verification:* confirmed (alta); confirmed (alta).

#### `gateway-request-path-1` — Every permission check reloads the whole projects and permissions tables as ORM objects, several times per request, with no per-request reuse

Gravità **media** (proposta: alta), sforzo M. Corsia `gateway-request-path`, segnalato anche da `gateway-queries`.

Dove: `gateway/app/services/permissions.py:102`, `gateway/app/services/permissions.py:135`, `gateway/app/services/permissions.py:152`, `gateway/app/services/permissions.py:165`, `gateway/app/services/permissions.py:201`, `gateway/app/services/permissions.py:35`, `gateway/app/services/objects.py:65`, `gateway/app/services/objects.py:152`

**Description.** `_all_projects` (permissions.py:102) runs `SELECT * FROM projects` and builds an ORM `Project` for every folder. `has_capability`/`ensure_can` (permissions.py:135) calls it on every check. `_granted_project_ids` (permissions.py:152-170), which sits behind `readable_project_ids`, `runnable_project_ids`, `connectable_project_ids`, `visible_project_ids` and `can_upload`, also loads EVERY grant of EVERY user (`select(Permission)` with no WHERE, line 165) and filters them in Python. Nothing is reused within a request. `visible_project_ids` calls readable again. `is_admin` is one query per call for group admins and non-admins, and it is called again at each layer.

What the harness counted for a non-admin:
- POST /tasks/preview: 3x `is_admin` (objects.py:65, objects.py:157, permissions.py:160), 1 full projects load, 1 full permissions load, plus key lookups. 9 SQL statements, against 1 for an admin.
- GET /search: projects loaded 5 times (readable, visible, connectable, `nomi_cartella`, plus the folder query) and permissions 3 times. 19 statements.
- GET /runs/{id}: `ensure_can` does 1 full projects load and runs ON THE EVENT LOOP, because the route is `async def`.
- Flow-group detail (flow_groups.py:139-140) calls `has_capability` twice plus `readable_project_ids`. The flow-group page polls /runs every 2.5 s while a run is live.

Admins with the personal flag skip all of this. That is why the owner's capacity figure does not show it: 95 req/s per process (helm values.yaml:379-383), measured by ~/tabularia-carico/carico.py:33-42 with a guest that has VIEW on the ~4 sample folders.

**Impact.** Gateway CPU per request for a non-admin, with no network to Postgres (SQLite in process), so real Postgres only adds time. Each figure is median ms, admin vs non-admin.

At 500 folders / 1,500 grants:
- preview: 6.7 vs 34
- search: 46 vs 192
- GET /projects: 16 vs 39
- GET /runs/{id}: 7.6 vs 13.5, on the event loop

At 2,000 folders / 6,000 grants:
- preview: 7.6 vs 256
- search: 307 vs 593
- GET /projects: 73 vs 182
- GET /runs/{id}: 8.8 vs 64, on the event loop

This work holds the GIL. Each gateway replica is one process.

With 30 people editing flows: one node click fires 1 to 3 previews (FlowEditor.vue:1149-1300). One click every ~7 s each gives ~10 previews/s, or ~3.3/s per replica across 3 replicas. At 2,000 folders that is ~0.85 of a core per replica for RBAC alone, with previews queueing behind each other. At 500 folders it is ~10%, before page loads and polls. Opening a folder in Explore sends ~6 requests, each with its own `ensure_can`, plus /projects. That is ~100 ms of gateway CPU per folder open at 500 folders and ~390 ms at 2,000. Cost grows linearly with folders plus grants, and the grants of other users count too.

**Evidence.** Harness /tmp/claude-1000/-home-ltrivelli-Scrivania-projects-tabularia-data-prep-clone/e4512242-d2ed-46a2-b103-4c17f1ec0061/scratchpad/review/gateway-request-path/harness.py: real app, SQLite in memory, statements counted with before_cursor_execute, median of 15 runs (output for 2,000 folders in scale4.txt). A non-admin preview showed the statement sequence users / groups / groups / projects(all columns) / groups / user_groups / permissions(all) / uploads / datasources.

Micro-benchmark bench_perm.py, 500 folders → 2,000 folders, ms:
- `readable_project_ids`: 39.1 → 142.1
- `has_capability`: 6.4 → 38.3
- `_all_projects` alone: 5.8 → 39.2
- `select(Permission)` as ORM objects: 16.1 → 102.9
- `select(Project.id, Project.parent_id)` as tuples: 0.8 → 3.8
- permission columns as tuples: 3.7 → 19.5

**Fix.** 1. Load the tree as `(id, parent_id)` tuples, not ORM objects (7-10x cheaper, measured).
2. Filter grants in SQL by subject: `WHERE user_id = :u OR group_id IN (:gids)`. The per-user index already exists.
3. Compute `is_admin`, the group ids and the readable/visible/runnable/connectable sets once per request, memoized in `session.info` or `request.state`, keyed by user id.
4. Have `visible_project_ids` reuse readable, and let search derive `nomi_cartella` from the tree it already loaded.
5. For single-folder checks, use a recursive CTE over ancestors joined with the subject's grants (one query, a few rows).
6. Re-measure the 95 req/s capacity with a non-admin on a few hundred folders.

*Verification:* confirmed (media); confirmed (media).

#### `gateway-queries-2` — Import polling and the refreshing flag scan all ingest history: runs.datasource_id has no index (36-53 ms per call, polled every 2.5 s per page)

Gravità **media** (proposta: alta), sforzo S. Corsia `gateway-queries`.

Dove: `gateway/app/models/run.py:107`, `gateway/app/routes/datasources.py:458-473`, `gateway/app/routes/datasources.py:117-131`, `frontend/pages/datasources.vue:164-174`, `frontend/pages/datasources.vue:194-202`, `frontend/components/ProjectBrowser.vue:346-356`

**Description.** runs.datasource_id has no index; runs has indexes only on kind, flow_id, flow_group_id, task_id, parent_run_id and three partial ones. GET /datasources/{id}/runs runs `WHERE datasource_id=? AND kind='ingest' ORDER BY started_at DESC LIMIT 50`. Postgres can only use ix_runs_kind for this, so it reads every ingest run ever recorded, filters them and sorts. Every open Datasources page and Explore folder calls this endpoint every 2.5 s for each datasource being imported (the server's `refreshing` flag re-attaches the poll). Each poll returns 50 RunOut objects including error_detail, and each poll also reconciles the live run with an engine HTTP call. Separately, every /datasources, /datasources/search and /projects/{id}/datasources list runs _refreshing_ids, which has the same shape (`kind='ingest' AND datasource_id IN (...) AND status NOT IN (...)`) and so the same scan.

**Impact.** With one year of history (175K ingest runs from 40 datasources refreshed every 2 h), one poll costs 38.6 ms of Postgres time on a parallel bitmap heap scan with 3 processes (175,200 rows read, about 171K discarded) and 36 ms end to end in the gateway. _refreshing_ids costs 53 ms on every datasource list. Scheduled imports with the same cron fire together. With 30 users on Datasources or Explore pages while 10 imports run, that is 30×10/2.5 = 120 polls/s ≈ 4.5 s of Postgres time per second, more than the host's 8 CPUs can absorb next to the workers. It also means 120 engine status calls per second and about 24 KB of response per poll.

**Evidence.** EXPLAIN ANALYZE on the synthetic DB: 'Parallel Bitmap Heap Scan on runs ... Rows Removed by Filter: 56940 (loops=3) -> Bitmap Index Scan on ix_runs_kind rows=175200 ... Execution Time: 38.562 ms'; _refreshing_ids for 400 ids: 'Execution Time: 53.303 ms'. After `CREATE INDEX ON runs (datasource_id, started_at DESC)`: 'Index Scan using zz_runs_ds_started ... Execution Time: 0.114 ms'. After a partial index on (datasource_id) WHERE status IN ('PENDING','STARTED'): 0.264 ms. bench.py: 'GET /datasources/1/runs (poll every 2.5 s) median 35.8 ms resp=24322'. `\di` on tabularia-postgres-1 lists no runs index on datasource_id, started_at or status.

**Fix.** Add an Alembic migration 0007 with `CREATE INDEX CONCURRENTLY ix_runs_datasource_started ON runs (datasource_id, started_at DESC)`. This also speeds up the FK check when a datasource is deleted. Add a partial index `ON runs (datasource_id) WHERE status IN ('PENDING','STARTED')` and write _refreshing_ids as `status IN ('PENDING','STARTED')`, because NOT IN cannot use a partial index. Poll a `limit=1` variant (or a /latest endpoint) instead of 50 runs, and leave error_detail out of list payloads; the detail view can load it.

*Verification:* confirmed (media); confirmed (media).

#### `gateway-queries-3` — Flows page open costs about 2 s of GIL-bound gateway CPU: /runs/activity pulls 175K rows into Python and /flows/search pulls every run of the page's 50 flows

Gravità **media** (proposta: alta), sforzo M. Corsia `gateway-queries`.

Dove: `gateway/app/routes/runs.py:1111-1148`, `gateway/app/routes/flows.py:162-171`, `frontend/components/ui/RunCalendar.vue:38`, `frontend/components/ui/RunCalendar.vue:56`, `frontend/pages/flows.vue:277`

**Description.** The calendar on the Flows page asks for 180 days of activity. runs_activity selects every top-level run in that window (`started_at` has no index) and builds the per-day buckets in a Python loop, when it only needs about 181 counts. On the same page, _with_owner_names fetches the last run status for the 50 flows on the page by loading ALL their runs with kind='flow', ordered by started_at, and keeping the last one per flow in a dict. Both costs grow with run history, not with the number of users, and they are paid on every page open, search keystroke (debounced) and page turn.

**Impact.** With one year of synthetic history, /runs/activity takes 1.43 s for an admin and 1.80 s for a normal user, fetches 175K rows and adds 74 MB of RSS per call. Postgres accounts for only 235 ms of that; the rest is transfer and Python. /flows/search page 1 takes 182 ms and fetches 34,834 rows; the database side takes 200 ms and its sort spills 4.6 MB to disk. Add /schedule/load (next finding) and a Flows page open costs about 2.1 s of gateway CPU. While one activity call runs, light requests on the same replica slow down: a permission read went from a p50 of 10.7 ms to 37.2 ms (GIL contention).

**Evidence.** bench.py: 'GET /runs/activity days=180 (admin) median 1429.6 ms sql=2 rows=175187', '(normal) 1798.4 ms rows=175425'; 'GET /flows/search page 1 (admin) median 184.7 ms rows=34834'. EXPLAIN ANALYZE: last-run query 'Bitmap Index Scan on ix_runs_kind rows=181400 ... Sort Method: external merge Disk: 4664kB ... Execution Time: 200.292 ms'; activity 'Index Scan using ix_runs_parent_run_id rows=175765 ... 235.081 ms'. With an index on runs (flow_id, started_at DESC), a LATERAL LIMIT 1 per flow took 0.609 ms; the same activity grouped in SQL returned 181 rows. gil.py: 'readable_project_ids alone: p50 10.7 ms' vs 'during runs_activity: p50 37.2 ms, p95 51.4 ms'.

**Fix.** Do the aggregation in SQL: `SELECT date_trunc('day', started_at - :off) k, count(*), count(*) FILTER (WHERE status='SUCCESS'), count(*) FILTER (WHERE status='FAILURE'), count(*) FILTER (WHERE trigger_type='schedule') ... GROUP BY k`, and the same with 'hour' for the day drill-down, backed by a partial index on runs(started_at) WHERE parent_run_id IS NULL. For the last run per flow, use a LATERAL (... ORDER BY started_at DESC LIMIT 1) or DISTINCT ON over the 50 ids, with an index on runs(flow_id, started_at DESC); the same index serves list_runs and flow_stats.

*Verification:* confirmed (media); confirmed (media).

#### `gateway-queries-4` — Run history and performance pages sort or scan the whole runs table (no index on started_at) and load error tracebacks they do not show

Gravità **media** (proposta: media), sforzo S. Corsia `gateway-queries`.

Dove: `gateway/app/routes/runs.py:1238-1246`, `gateway/app/routes/runs.py:1213-1228`, `gateway/app/routes/performance.py:43-45`, `gateway/app/schemas/models.py:555`

**Description.** search_runs counts across a 4-table outer join and orders by Run.started_at DESC LIMIT 50. Without an index on started_at, every page sorts all runs, top-N. Search text is matched with ILIKE across error and error_detail (tracebacks up to 20 KB) plus four names. runs_performance loads full Run rows, including error_detail, group_cells and destination, for up to 20,000 runs, only to compute durations per flow in Python. The RunOut/RunSearchOut lists include error_detail for every row.

**Impact.** With one year of synthetic history (488K runs): GET /runs with no filter takes 719 ms (the page query alone is 72.7 ms of DB; with an index on started_at it is 0.106 ms). The default status=FAILURE view takes 140 ms. Searching with q=flow12 takes 2.07 s. /admin/performance/runs takes 429 ms for 7 days (9,922 full rows) and 965 ms for 90 days, where it hits the 20K cap and is truncated. The /runs?status=FAILURE response was 110 KB with 3 KB synthetic tracebacks; real tracebacks reach 20 KB, so a page can approach 1 MB.

**Evidence.** bench.py: 'GET /runs (all, admin) median 719.4 ms', 'GET /runs?status=FAILURE (admin) median 139.6 ms resp=110106', 'GET /runs?q=flow12 (admin) median 2068.2 ms', 'GET /admin/performance/runs days=7 median 428.6 ms rows=9922', 'days=90 median 964.7 ms rows=20700'. EXPLAIN ANALYZE of the page query: 'Parallel Seq Scan on runs r rows=162667 loops=3 ... top-N heapsort ... Execution Time: 72.698 ms' -> with index 'Index Scan using zz_runs_started ... Execution Time: 0.106 ms'.

**Fix.** Add an index on runs(started_at DESC); it serves /runs, the performance window and the activity range. In runs_performance, select only id, kind, flow_id, datasource_id, status, started_at, engine_started_at, finished_at, rows_written and left(error, 240), or compute median/p95 in SQL with percentile_cont grouped by (kind, flow_id or datasource_id). Leave error_detail out of list schemas and serve it from GET /runs/{id}.

*Verification:* confirmed (media); confirmed (media).

#### `gateway-queries-5` — /schedule/load expands every cron firing in Python on every Flows page open: 0.5 s for 7 days, 2.1 s for 30 days, uncached and GIL-bound

Gravità **media** (proposta: media), sforzo M. Corsia `gateway-queries`.

Dove: `gateway/app/services/schedule_load.py:104-135`, `frontend/components/ui/ScheduleLoad.vue:34`, `frontend/components/ui/ScheduleLoad.vue:53`, `frontend/pages/flows.vue:280`

**Description.** compute_schedule_load iterates croniter.get_next for every firing of every active schedule over the horizon, up to 5,000 per schedule, and builds dicts per minute. It runs synchronously on every Flows page open (7 days by default, up to 30). The result only changes when a schedule changes, yet nothing is cached. It also loads full Flow rows, definition included, just to read name and cron.

**Impact.** croniter costs 54-85 µs per firing. With 70 two-hourly schedules (5,880 firings), the call takes 504 ms for 7 days and 2.07 s for 30 days. With 70 schedules every 15 minutes it would be about 47K firings: roughly 2.6 s for 7 days and about 11 s for 30 days. All of it is pure Python holding the GIL, which also slows the other requests on the same replica (see the GIL measurement in the Flows page finding).

**Evidence.** bench.py: 'GET /schedule/load days=7 median 503.7 ms', 'days=30 median 2066.7 ms' (70 schedules '0 */2 * * *'). cron.py in tabularia-gateway:dev: '0 */2 * * * 7d: 84 firings in 7.2 ms (85 us/firing)', '*/15 * * * * 30d: 2884 firings in 154.7 ms (54 us/firing)', '*/5 * * * * 7d: 2016 firings in 108.0 ms'.

**Fix.** Cache the result for each (readable-set hash, days) key and invalidate it on any schedule update (the latest updated_at of flows, datasources and groups with a schedule). Alternatively, compute the weekday×hour grid analytically from the expanded cron fields instead of enumerating firings, and keep enumeration only for the minutes that collide. Select only the id, name and cron columns.

*Verification:* confirmed (media); confirmed (media).

#### `gateway-queries-6` — `.first()` on ORDER BY queries without LIMIT transfers the whole history (7 sites: refresh, scheduler, orchestrator, flow save, failure notice)

Gravità **bassa** (proposta: media), sforzo S. Corsia `gateway-queries`, segnalato anche da `background-loops`.

Dove: `gateway/app/routes/datasources.py:392-396`, `gateway/app/services/scheduler.py:113-117`, `gateway/app/services/orchestrator.py:141-145`, `gateway/app/services/flow_group_runner.py:290-291`, `gateway/app/routes/flows.py:236-240`, `gateway/app/services/notifier.py:49-57`, `gateway/app/services/contracts.py:208-212`

**Description.** In SQLAlchemy 2.0, Result.first() does not add LIMIT, and psycopg2 uses client-side cursors, so Postgres computes and sends every matching row before the first one is kept. The sites are:
- the last ingest run of a datasource, as full Run rows: on a manual refresh, on every scheduled refresh fire, on every refresh step of an orchestration or flow-group cell;
- the last FlowVersion, with its full definition: on every flow save, create and promote;
- the previous orchestration of a flow: on every failure notification;
- the last contract version.

**Impact.** For a datasource with 4,380 ingest runs (one year at 2 h): 160 ms and 4,380 full rows, against 34 ms with LIMIT 1 (and 0.07 ms with the datasource_id index from the polling finding). For a flow with 400 versions of 11 KB: 30.6 ms and 400 definitions (about 4.5 MB) on each save, against 0.9 ms with LIMIT 1. The scheduler pays the refresh cost for every datasource that fires: 40 fires at once is about 6.4 s of serial work in the orchestrator tick.

**Evidence.** bench.py (show_sql printed the statement without LIMIT): 'save: last FlowVersion .first() flow 1 median 30.6 ms rows=400' vs 'save: same with .limit(1) median 0.9 ms rows=1'; 'refresh/scheduler: last ingest .first() ds 1 median 160.4 ms rows=4380' vs '.limit(1) median 33.8 ms rows=1'. An AST scan found exactly these 7 `.first()` calls with order_by and no limit. Driver: psycopg2-binary (gateway/pyproject.toml:11), SQLAlchemy 2.0.51.

**Fix.** Add `.limit(1)` at all 7 sites. Select only the columns that are used: the scheduler needs id, status and task_id, and _snapshot_version needs version and definition from the latest row. Combine with the runs(datasource_id, started_at DESC) index.

*Verification:* confirmed (bassa); confirmed (bassa).

#### `gateway-request-path-2` — Run and ingest pollers are async routes that do SQL on the event loop and keep a pooled connection while awaiting the engine (the stall fixed in proxy.py, still open here)

Gravità **media** (proposta: media), sforzo M. Corsia `gateway-request-path`.

Dove: `gateway/app/routes/runs.py:1159`, `gateway/app/routes/runs.py:1065`, `gateway/app/routes/runs.py:777`, `gateway/app/routes/datasources.py:458`, `gateway/app/routes/connections.py:459`, `gateway/app/routes/connections.py:490`, `gateway/app/routes/flows.py:633`, `gateway/app/routes/flow_groups.py:288`

**Description.** `RottaCheRilascia` (routing.py:242-246) exempts async routes. proxy.py:78-93 records why this matters: SQL on the loop and connections held across awaits together froze the gateway for 28 s. The proxy routes were fixed and have tests (test_proxy_releases_db.py). These routes were not.

Hot, polled:
- GET /runs/{id} (runs.py:1159-1181). Polled every 1.5 s per running Output node and every 2 s for the orchestration run (FlowEditor.vue:1706, :1552).
- GET /flows/{id}/runs (runs.py:1065-1076). Polled every 2.5 s while a flow's run list is open in Explore (ProjectBrowser.vue:311-315). For 50 runs it awaits the engine once per live run, one after another.
- GET /datasources/{id}/runs (datasources.py:458-471). Polled every 2.5 s during imports (datasources.vue:165-173, ProjectBrowser.vue:348-355).

These pollers run `session.get`, `ensure_can` (4 queries for a non-admin, including a full projects load) and `run_out` on the event loop. `_reconcile` then awaits the engine (runs.py:777, timeout 30 s) while the request's transaction and pooled connection stay open (idle in transaction).

Not hot, same pattern:
- POST /flows/{id}/runs and /email-test (runs.py:91, :102, :137).
- POST /datasources/{id}/refresh and the db/sharepoint datasource creates (datasources.py:242, :322, :375).
- Connection test, tables and SharePoint files (connections.py:427-500). These keep the connection while the engine probes an external database, with NO gateway timeout (engine_client.py:275).
- dbt export (flows.py:272-360): admin-only, holds the connection across the AI calls.
- Admin pages: engine settings (engine_settings.py:105), SIEM test (siem.py:164), /ai/models (ai.py:81, :92), the AI export step and chat (ai.py:210, :261).

Async but with no `await` at all, so the whole body runs on the loop:
- POST /flows/{id}/run-now (flows.py:633-672), which includes `pg_advisory_xact_lock`. The loop waits if another replica or the scheduler holds the lock.
- POST /flow-groups/{id}/run-now (flow_groups.py:288-307).
- DELETE /datasources/{id} (datasources.py:533-566). It loads every run of the datasource and updates them one by one, for example ~8,760 runs for an hourly refresh kept a year.

Done right: contracts.py (`run_in_threadpool`), proxy.py, queue.py, performance.py, system.py (no DB).

Pool model per api replica in prodtest:
- 12 threads (APP__GATEWAY_THREADS).
- Main pool 12 + 10 overflow = 22, `pool_timeout` 30 s.
- Reader pool 3 (session.py:72-74).
- At most 25 connections per process. 5 processes stay under 130 of `max_connections` 300.

The rule 'one connection per thread' (session.py:58-62) only bounds sync code. Async routes check out connections outside the thread limiter, so they alone can drain the 22. The next checkout on the loop then blocks the whole replica.

**Impact.** Steady state: every poll runs its SQL round trips and RBAC on the loop. For a non-admin that is 6 statements, plus 13.5 ms of CPU at 500 folders or 64 ms at 2,000 (finding 1). Each one delays every other request on the replica: streaming previews, auth, catalog.

When the engine slows down or hangs, each poll holds a DB connection for up to 30 s. A hung backend under memory pressure is the incident of 2026-10-13. Once more than 22 of these requests wait at once on one replica, the next async checkout freezes the event loop for `pool_timeout` (30 s). Every request on that replica stalls, including login and catalog pages, and the waiting ones answer 500. With 30 users running flows and watching Explore or imports, a replica sees about one poller per running Output plus one per orchestration and per open run list. Reaching 22 on one replica takes ~60 active pollers across 3 replicas, or fewer in role `all` and on 1-replica installs. The connection-test routes, with no timeout, can pin connections indefinitely.

**Evidence.** Demonstration with stall.py (scratch folder): the real app through ASGITransport, QueuePool pool_size=4 / max_overflow=0 / pool_timeout=3 s, an engine that answers in 0.3 s, admin user.
- 3 concurrent GET /runs/{id}: all 200, wall time 0.47 s.
- 6 concurrent GET /runs/{id}: two answered 500 (QueuePool TimeoutError) after 3.06 s and 6.06 s, the other four took 6.06-6.2 s. An unrelated GET /projects sent at the same time took 6.01 s instead of 0.01 s.
- 12 concurrent GET /tasks/{id} through the proxy, which releases before awaiting: all 200 in 0.56 s.

Code: runs.py:1166-1181 (`session.get` / `ensure_can` / `await _reconcile` with the session open); runs.py:777 (await engine). No test covers these routes; test_connection_per_hop.py:240 checks only the route class.

**Fix.** Apply the proxy rule to these routes:
1. Read the run and check RBAC inside `run_in_threadpool`, then close the session.
2. Await the engine with no connection held.
3. Apply the transition (the conditional UPDATE claim already makes it safe) in a new short thread-side transaction.

Alternatively, make the pollers plain `def` routes that call the engine through a sync httpx client with the CONTROLLO timeout. Turn the routes with no `await` (run-now x2, DELETE datasource) into `def`. Add CONTROLLO-style timeouts to `_engine_inspect`. Extend test_proxy_releases_db.py to these routes so the property is tested.

*Verification:* confirmed (media); confirmed (media).

#### `measure-3` — GET /queue never answers in under 1 s: four Celery inspect broadcasts each wait their full 1.0 s timeout

Gravità **bassa** (proposta: media), sforzo S. Corsia `measure`.

Dove: `backend/app/api/routes/tasks.py:234-252`, `gateway/app/routes/queue.py:22-29`, `frontend/pages/queue.vue:28-50`

**Description.** _inspect_together runs `celery_app.control.inspect(timeout=1.0)` for active, reserved, stats and active_queues in four new threads. Without `limit`, Celery collects replies until the timeout expires, even though both workers answer within milliseconds.

**Impact.** This is the slowest GET in the app by about 40x. The queue page polls 2 s after each answer, so it refreshes every ~3.1 s instead of 2 s. Each open page sends 4 broadcasts every ~3 s to every worker's control channel and holds a backend thread for 1 s, plus 4 new threads per call. The stop button waits another 1 s or more. At 30 users with 3 admin sessions this was 0.49 calls/s.

**Evidence.** At idle /queue p50 was 1089 ms (max 1098); every other GET route had p50 of 26 ms or less. Under load: p50 1073 ms at 10 users, 1062 ms at 20, 1056 ms at 30, p99 1113 ms, 121 calls in 248 s. In the backend container, control.inspect(timeout=1.0).active() took 1132 ms, the same call with limit=2 (the two workers) took 14 ms, and control.ping(timeout=1.0) took 1016 ms.

**Fix.** Pass limit = the number of workers expected, taken from the previous answer or from celery-exporter, and fall back to the timeout only when a worker is missing. Alternatively shorten the timeout to ~0.25 s, or cache one overview in the backend for ~1.5 s shared by every admin.

*Verification:* confirmed (media); confirmed (bassa).

#### `measure-4` — Memory badge: the most frequent request scrapes 86 KB from node-exporter inline, under a lock, with a 3 s per-replica cache

Gravità **bassa** (proposta: bassa), sforzo S. Corsia `measure`.

Dove: `gateway/app/routes/system.py:91-118`, `gateway/app/core/config.py:241-244`, `frontend/composables/useSystemMemory.ts:45-46`

**Description.** _read_memory holds an asyncio.Lock while it downloads node-exporter's full /metrics page, then keeps the value for monitoring.cache_seconds=3 s, separately in each gateway replica. The UI polls every 30 s, or every 10 s in the editor, so in practice the cache is usually expired: one request pays a full scrape and the requests that arrive meanwhile wait behind the lock.

**Impact.** This is the top request by count, 12.7% of all traffic at 30 users (1.61/s), and it has a recurring 0.4-0.7 s tail on every replica. The 3 replicas scrape node-exporter up to about once a second (86 KB each) on top of VictoriaMetrics. Nobody is blocked (it is a background poll), but it is the second-slowest route and it adds load for a value that is the same for everyone.

**Evidence.** At 30 users: n=398 of 3141, p50 15 ms, p95 423, p99 520, max 729 ms. At 10 users: p95 612, p99 726. At idle: max 322-426 ms. node-exporter /metrics fetched from inside a gateway container: 86,150 bytes in 118-310 ms (5 tries).

**Fix.** Ask only the meminfo collector (`/metrics?collect[]=meminfo`, a few KB). Refresh in the background and serve the last value without waiting. Raise the cache to at least 10 s, which still matches the UI's 10-30 s polling.

*Verification:* confirmed (bassa); confirmed (bassa).

#### `infra-perf-4` — API responses are never compressed: Caddy compresses only the frontend handler, so JSON travels 5-13x larger

Gravità **bassa** (proposta: media), sforzo S. Corsia `infra-perf`, segnalato anche da `frontend-perf`.

Dove: `infrastructure/production/Caddyfile:41-63`, `infrastructure/production/Caddyfile:89-90`, `infrastructure/helm/tabularia/templates/ingress.yaml:21-30`, `frontend/components/FlowEditor.vue:590`

**Description.** `encode zstd gzip` sits only in the catch-all `handle` that proxies the frontend. The `handle_path /api/*` block has no encode, the gateway has no GZipMiddleware, and the Helm ingress sets no compression (ingress-nginx leaves gzip off unless its ConfigMap enables it). Every API response therefore goes out uncompressed: lists, previews, the full datasource catalog (loaded on every editor open, FlowEditor.vue:590, and by the viewer, chat and flow-group pages), runs, audit and lineage.

**Impact.** Measured ratios: /api/datasources is 1.22 KB per datasource raw and 0.22 KB gzipped. A catalog of 500 datasources means about 610 KB per editor open instead of about 107 KB, which on a 10 Mbit/s remote or VPN link is roughly 490 ms against 86 ms on every open. The runs page (50 rows) is 36.6 KB against 3.6 KB, and audit (100 rows) 27 KB against 2 KB. Previews average 17.6 KB on the sample data and grow with columns and limit. On a LAN this is noise; for remote users of a self-hosted single machine it is visible latency on every page and click.

**Evidence.** Single GET of each endpoint through the proxy with Accept-Encoding: gzip, br, zstd. Every response came back without Content-Encoding. Raw bytes vs local gzip -6: /api/runs?limit=50 36,621 vs 3,631; /api/audit?limit=100 26,952 vs 1,974; /api/datasources (20 items) 24,445 vs 4,307; /api/lineage 9,428 vs 1,309; /api/flows 5,661 vs 1,371. Backend /metrics: 1410 previews, 24.8 MB of responses, 17.6 KB average. Frontend assets are compressed (the 679 KB chunk goes out as 234 KB zstd) and are cached as immutable.

**Fix.** Inside `handle_path /api/*`, add `encode zstd gzip { match { header Content-Type application/json* } minimum_length 1024 }`. This compresses JSON, including streamed JSON, and leaves SSE (text/event-stream), xlsx and binary downloads alone. Optionally add text/csv for exports. In the chart, document setting `use-gzip: "true"` and `gzip-types: application/json` in the ingress-nginx ConfigMap.

*Verification:* confirmed (bassa); confirmed (media).

#### `gateway-queries-8` — List endpoints load Flow.definition, Datasource.columns and the contract report for every row only to return names

Gravità **bassa** (proposta: bassa), sforzo S. Corsia `gateway-queries`.

Dove: `gateway/app/routes/flows.py:113-125`, `gateway/app/routes/search.py:87-111`, `gateway/app/services/schedule_load.py:104-111`, `gateway/app/routes/flow_groups.py:70-76`, `gateway/app/services/contracts.py:183-192`, `frontend/components/FlowEditor.vue:590`, `frontend/components/FlowEditor.vue:613`, `frontend/pages/flow-group.vue:262`

**Description.** These lists all select whole ORM rows:
- GET /flows: the editor and the flow-group page use it for a picker of id and name.
- /search: every flow and datasource whose name matches.
- schedule_load.
- flow_groups._objects: when a group is opened or saved.
So every flow definition (5.8 KB on average on prodtest, up to 12.7 KB) is read from TOAST and sent to the gateway even though FlowOut and SearchHit drop it. contracts.summaries loads last_report and document for every datasource in a list. Separately, the editor loads the full /datasources list, with every column of every datasource, each time it opens.

**Impact.** With 300 flows of 11 KB: select(Flow) takes 24.1 ms and moves about 3.4 MB per call, against 4.0 ms with the definition deferred (6× faster). /datasources for 400 datasources × 40 columns takes 81 ms and returns an 892 KB JSON on every editor open. /search?q=flow takes 51 ms for a normal user because all 300 matching definitions are loaded for 30 hits.

**Evidence.** defer.py: 'select(Flow) 300 rows: 24.1 ms' vs 'select(Flow) defer(definition): 4.0 ms'. bench.py: 'GET /datasources (admin, 400) median 80.7 ms resp=892599'; 'GET /search?q=flow (normal) median 50.9 ms sql=20 rows=1129'. prodtest: `avg(length(definition))=5849, max=12669`.

**Fix.** Use `.options(defer(Flow.definition))` or select columns explicitly in these lists. Defer last_report and document in contract summaries. Give the editor's source picker a light datasource list (id, name, project_id, key, rows) and load the columns of the chosen datasource on demand.

*Verification:* confirmed (bassa); confirmed (bassa).

#### `gateway-queries-10` — Key authorization fallback scans runs by output_key/input_key (no index), loads full rows and calls session.get(Flow) per match

Gravità **bassa** (proposta: bassa), sforzo M. Corsia `gateway-queries`, segnalato anche da `gateway-request-path`.

Dove: `gateway/app/services/objects.py:107-115`, `gateway/app/services/objects.py:120-122`, `gateway/app/routes/proxy.py:258`, `frontend/components/FlowEditor.vue:580-581`

**Description.** When a storage key is neither the user's own upload nor the current key of a readable datasource, _can_read_key runs `select(Run).where(or_(Run.output_key == key, Run.input_key == key))`. Neither column is indexed, so this is a full scan returning full Run objects, followed by one session.get(Flow) per matching run, and then a LIKE over every readable flow definition. This path runs for a non-admin preview whose source still carries an earlier snapshot key: the editor only re-syncs keys when it loads the datasource list (FlowEditor.vue:580), and a scheduled refresh writes a new timestamped key every time.

**Impact.** 67 ms per key on the fallback path against 8 ms for a current datasource key (488K runs), on a preview that otherwise adds only milliseconds of gateway work. Each extra matching run adds a Flow load that includes its definition.

**Evidence.** bench.py: '_can_read_key fallthrough (old snapshot key, normal) median 66.6 ms sql=9' (the printed SQL shows `SELECT runs.id, runs.kind, ... FROM runs WHERE runs.output_key = ... OR runs.input_key = ...`), vs '_can_read_key current ds key (normal) median 8.2 ms'.

**Fix.** Select only Run.launched_by and Flow.project_id with a join, and check `EXISTS` instead of loading rows. Add an index on runs(output_key), and on input_key if that branch is kept, or record key ownership in a small table. Separately, have the editor re-resolve source keys when it gets a stale-key error.

*Verification:* confirmed (bassa); confirmed (media).

#### `gateway-request-path-4` — Engine client has no timeouts on several control calls, and its default 100-connection pool has no wait limit

Gravità **bassa** (proposta: bassa), sforzo S. Corsia `gateway-request-path`.

Dove: `gateway/app/core/engine_client.py:275`, `gateway/app/routes/queue.py:26`, `gateway/app/routes/proxy.py:137`, `gateway/app/routes/proxy.py:201`, `gateway/app/routes/performance.py:92`, `gateway/app/routes/connections.py:239`

**Description.** The shared `httpx.AsyncClient` is created with `timeout=None` and default limits (100 connections, 20 keep-alive). It applies `timeout=None` to the read, the write and the wait for a pool slot. Only `_reconcile`, run launches and engine settings pass CONTROLLO.

Calls with no timeout:
- GET /tasks/{id} and /tasks/operations through `_forward` (proxy.py:137). The editor polls them every 1-2 s for uploads and transforms.
- /engines (proxy.py:201).
- /queue and job stop (queue.py:26, :37).
- The two performance views (performance.py:92).
- Connection inspect (connections.py:239-251), which also holds a DB connection (finding 2).

Previews, exports and uploads hold a client connection for their whole duration, by design.

**Impact.** If the backend hangs instead of refusing connections, these requests never finish, and the browser pollers stack up on them. Once 100 long previews, exports or hung calls are in flight in one process, every other engine call without its own timeout waits indefinitely for a pool slot. One process with 30 editors, each with up to 4 preview slots, gives up to ~120 previews in flight. That is reachable with 1 replica or in role `all`, not with 3 replicas.

**Evidence.** engine_client.py:268-276; queue.py:25-29; proxy.py:127-143; httpx defaults (`Limits(max_connections=100, max_keepalive_connections=20)`, `Timeout(None)` applies to the pool too).

**Fix.** Give the short control calls (task status, operations, engines, queue, performance, inspect) `CONTROLLO` or a specific timeout. Set explicit `httpx.Limits`, with a pool timeout for the short calls, so long previews and exports cannot starve them. Optionally use a separate client for streaming transfers.

*Verification:* confirmed (bassa); confirmed (bassa).

#### `background-loops-4` — The webhook reclaim UPDATE scans the whole delivery log on every dispatch round

Gravità **bassa** (proposta: bassa), sforzo S. Corsia `background-loops`.

Dove: `gateway/app/services/webhooks.py:413-418`, `gateway/app/services/webhooks.py:571-587`, `gateway/app/models/webhook.py:36-38`

**Description.** Every round of `consegna_loop` first runs `UPDATE webhook_deliveries SET status='pending' WHERE status='sending' AND claimed_at < now()-5min`. The only partial index covers status='pending', so this is a sequential scan of the 30-day delivery log. Rounds happen every 2 s in each orchestrator, and also each time an in-flight delivery completes (asyncio.wait FIRST_COMPLETED). The work runs in a thread, so the cost lands on Postgres, not on the event loop.

**Impact.** With webhooks subscribed to every event at the target load (about 3.4k closures a day per webhook; 2 webhooks give roughly 200–300k rows over 30 days), each scan costs about 165 ms. Two orchestrators idle at 1 scan/s, which is about 17% of a Postgres core spent permanently on a no-op. While a backlog drains, each completion triggers a scan, so up to about 5 scans/s per process, roughly 1.5 cores, until the queue empties.

**Evidence.** Scratch Postgres with 300,000 delivered rows (156 MB): the reclaim UPDATE took 167.6 ms and 163.6 ms (rolled back). With `CREATE INDEX ... ON webhook_deliveries(claimed_at) WHERE status='sending'` it took 5.7 ms on first touch. The due-window query already uses ix_webhook_deliveries_due.

**Fix.** Add a partial index on webhook_deliveries(claimed_at) WHERE status='sending' (migration), or run the reclaim at most once a minute instead of on every round.

*Verification:* confirmed (bassa); confirmed (bassa).

### P7 — Data contract

#### `gap-1-3-1` — One rule that fails at run time (strict CAST, regex syntax Polars rejects) wipes out the whole contract report and refuses every refresh, even when that rule is only a warning

Gravità **media** (proposta: alta), sforzo S. Corsia `gap-1-3`.

Dove: `backend/app/contracts/evaluator.py:316-318`, `backend/app/contracts/evaluator.py:336`, `backend/app/contracts/evaluator.py:203-209`, `backend/app/contracts/evaluator.py:259-264`, `backend/app/contracts/service.py:47-60`, `gateway/app/services/contracts.py:87-93`, `gateway/app/routes/runs.py:843-852`, `docs/specs/data-contracts.md:115-117`

**Description.** Before reading any data, evaluate() tests each rule only against the schema (collect_schema, line 318). It then computes every count in a single collect() at line 336, with no per-rule fallback. Some errors only show up while that collect runs: a strict CAST that meets a bad value, an expression that returns a string, or a regex that Python's re accepts but Polars' Rust engine rejects (lookahead, backreferences). The gateway (contracts.py:91) and the evaluator (evaluator.py:204) both check regexes with Python's re. When the collect fails, evaluate() raises and check_before_publish catches it. It then returns outcome 'failed' with rules: [] whenever the contract has any blocking rule, and _reconcile refuses the snapshot. The spec says the opposite: 'A rule that cannot be evaluated (... the expression does not run on this data) does not hold, and says why', meaning that single rule fails, with its own severity. In practice a warning rule like `CAST(code AS INTEGER) > 0`, which exists to spot non-numeric codes, blocks the whole publication the first time a non-numeric code arrives. A warning pattern such as `(?=.*\d).+` blocks every refresh from the moment it is saved. PUT /contract only shows a check_error and saves it anyway. The proposal adds a blocking row_count rule to every contract, so the fallback almost always turns into a refusal.

**Impact.** Good data is blocked. Every refresh or publish of the datasource is refused ('contratto non valutabile: ...'), the datasource serves stale data, and the history holds no per-rule results until someone edits the contract. Whether a regex blocks depends on its syntax; whether a CAST blocks depends on the data, so a contract that works today can stop all updates tomorrow.

**Evidence.** Scratch test test_zz_contracts.py run in the tabularia-backend:dev container. Expression `CAST(code AS INTEGER) > 0` (warning) plus not_null (error): `RAISED InvalidOperationError: conversion from str to i32 failed ... ["x"]`. Expression `s` (string result): `RAISED InvalidOperationError: casting from Utf8View to Boolean not supported`. Expression `CAST(s AS DATE) IS NOT NULL` (warning): `RAISED ... conversion from str to date failed` (the TRY_CAST form works: 1 violation). Patterns `(?=.*\d).+` and `(a)\1` (warning): `RAISED ComputeError: regex error: regex parse error`. check_before_publish with a runtime-failing warning expression plus row_count min 1 (error) returns `{"outcome": "failed", "rows": null, "errors": 1, "rules": [], "error": "conversion from str to i32 failed ..."}`. The gateway's validate_document accepts both the lookahead pattern and the CAST expression (test_zz_odcs_validate.py: `validate_document accepts: [p1, p2, e1, a1]`).

**Fix.** When the single pass raises, run each check's aggregate on its own (one collect per rule, or bisect the list) and turn only the failing rule into `{passed: False, error}` with its own severity. In the evaluator's pre-check, compile each regex with Polars' engine (e.g. `pl.select(pl.lit('').str.contains(rx))`) so Rust-only syntax errors become per-rule errors at save time. Consider documenting TRY_CAST for expressions.

*Verification:* confirmed (media); confirmed (media).

#### `gap-1-3-2` — Snapshot freshness on flow-published datasources counts from creation or the last metadata edit, not the last publish: the contract turns red about 60 s after every publish and sends 2 emails per publish

Gravità **media** (proposta: media), sforzo S. Corsia `gap-1-3`.

Dove: `gateway/app/routes/runs.py:996-1008`, `gateway/app/routes/runs.py:1018-1032`, `gateway/app/routes/runs.py:947-955`, `gateway/app/services/contracts.py:340`, `gateway/app/routes/contracts.py:120-125`, `gateway/app/routes/datasources.py:525`, `backend/app/contracts/evaluator.py:237-239`

**Description.** refresh_freshness (the scheduler sweep) and _snapshot (Check now and the check on save) take the production time as `ds.refreshed_at or ds.updated_at or ds.created_at`. Only _finalize_ingest (the database refresh) sets refreshed_at and updated_at. _publish_datasource changes key, rows and columns when a flow overwrites a datasource, but sets neither timestamp, and a newly created flow datasource has no refreshed_at. For kind='flow' datasources the 'age of the data' is therefore the time since creation, reset only by PATCH /datasources (rename, description, column descriptions), which has nothing to do with the data. When a flow publishes, the worker evaluates with snapshot_at=None, i.e. now, so the rule passes. On the next scheduler tick the sweep recomputes the age from created_at or updated_at, flips the state to failed and queues an email. The next publish flips it back to passed and queues another. The reverse also happens: editing the description of a stale flow datasource makes its freshness rule pass.

**Impact.** Freshness rules without a column cannot be used on any datasource a flow publishes, one of the two enforcement points. The spec's own example (r8: freshness 26 h, no column) is this kind of rule. The state icon is red almost all the time, Check now reports failed, and the notification list gets 2 emails per publish (48 per day for an hourly flow). Real staleness is hidden after any metadata edit.

**Evidence.** test_zz_contract_gateway.py::test_flow_published_freshness_flip_flops (SQLite, foreign keys ON). Flow datasource created 3 days ago, freshness 24 h (error), notifications on, two publishes through _launch_flow_run plus _reconcile, each followed by sweep_freshness. Output: `publish 0: [SUCCESS, new key, refreshed_at=null, updated_at=2026-10-11 ..., passed]`, `sweep after publish 0: [1, failed, 72.0]`, `publish 1: [..., passed]`, `sweep after publish 1: [1, failed, 72.0]`. Results: `[publish passed notify=null], [freshness failed notify=pending], [publish passed notify=pending], [freshness failed notify=pending]`.

**Fix.** In _publish_datasource set `refreshed_at` (and `updated_at`) to now on overwrite and on creation, the same way _finalize_ingest does. Better: keep one 'data produced at' column written only by data swaps, and stop falling back to updated_at in refresh_freshness and _snapshot, so metadata edits no longer count.

*Verification:* confirmed (media); confirmed (media).

#### `gap-1-3-4` — Freshness rules on a column never age: the sweep skips them, so a datasource whose updates stop keeps a green state forever

Gravità **media** (proposta: media), sforzo S. Corsia `gap-1-3`.

Dove: `gateway/app/services/contracts.py:326-329`, `gateway/app/services/contracts.py:337`, `docs/specs/data-contracts.md:93`

**Description.** refresh_freshness re-evaluates only the freshness rules that have no column (`not r.get('column')`). The docstring says the column variants 'are evaluated by the engine', but the engine evaluates them only when new data arrives. For a fixed snapshot the newest value in the column never changes, so its age grows exactly with wall-clock time, and the report already holds what is needed: `observed` (the age in hours at evaluation) plus the evaluation time. The spec presents freshness as 'the only rule that can break without new data arriving' and lists the column form in the same row. Column freshness (e.g. on updated_at, like dbt's loaded_at_field) is the usual way to write it.

**Impact.** When refreshes or publishes stop succeeding (schedule switched off, connection broken, upstream flow no longer running), a column freshness rule stays 'passed' indefinitely. Consumers see a green contract on data that may be days old, and nobody is notified.

**Evidence.** test_zz_contract_gateway.py::test_column_freshness_never_ages. Contract with only `freshness column=updated_at max 24h`, last report passed with observed 2.0 h, sweep_freshness 8 days after the last refresh: `[changed=0, status=passed]`.

**Fix.** In refresh_freshness, also update the column rules that have a numeric observed age: age_now = observed + (now - c.checked_at) in hours, passed = age_now <= max_age_hours. Then compute the outcome as for snapshot rules.

*Verification:* confirmed (media); confirmed (media).

#### `gap-1-3-3` — The worker checks the contract version from launch time, but the gateway records the report as the status of whatever version is current at reconcile: tightened rules are skipped and the new data shows as 'passed'

Gravità **bassa** (proposta: media), sforzo M. Corsia `gap-1-3`.

Dove: `gateway/app/routes/runs.py:463`, `gateway/app/routes/runs.py:512`, `gateway/app/routes/runs.py:838-843`, `gateway/app/routes/runs.py:696-708`, `gateway/app/services/contracts.py:273-288`, `gateway/app/routes/contracts.py:301-305`, `backend/app/tasks/jobs.py:228-238`

**Description.** The contract document goes to the worker at launch (payload with its version). At reconcile, _reconcile checks only that some enabled contract still exists (runs.py:839). record_result then stores `contract_version=c.version`, the current version, and copies the old report into c.status and c.last_report. The report's own 'version' field is never compared. This causes three verified problems. (a) The contract is tightened while a refresh or publish is running (a new blocking rule): the data is published without ever being checked against that rule, and the state reads 'v2 passed'. last_report contains only the old rules. (b) A contract is created or enabled while a run is going: the worker evaluated nothing, the snapshot is swapped unchecked, and the green state from the save-time check describes the old key. The manual Check now has the same gap: its guard compares only the document, not ds.key, so a refresh that lands during the 120 s evaluation gets the old snapshot's report. (c) A contract is removed or disabled while a run is going and the worker's report was 'failed': the worker already returned early and skipped the S3 copy and email (jobs.py:232-238), but the gateway now ignores the report and closes the run as SUCCESS, so the copy is silently never made and its summary stays at ok: null.

**Impact.** Data that breaks the current blocking rules can be published and labelled compliant. The window is the run's duration (minutes to an hour), so tightening a contract while a scheduled refresh is running is enough. The history attributes reports to the wrong contract version, and case (c) gives a green run whose deliveries never happened.

**Evidence.** test_zz_contract_gateway.py, SQLite with foreign keys ON. test_contract_version_drift: `[sent_version=1, run=SUCCESS, ds key=new, contract version=2 status=passed, result.contract_version=2, report.version=1, rules in stored report=['r1']]`, so the new blocking rule r2 was never evaluated. test_contract_created_mid_run: `[SUCCESS, ds key=datasets/8/new.parquet, status=passed, status refers to datasets/8/old.parquet]`. test_contract_removed_mid_run_silent_non_delivery: `[SUCCESS, datasets/5/new.parquet, email {... "ok": null}, mirror {... "ok": null}]`.

**Fix.** At reconcile, compare report['version'] with the current contract version and check whether a contract exists that the worker did not see. When they differ, apply the decision of the version that was actually evaluated, but record contract_version from the report and set the state to 'pending' (or queue a check on the new key) instead of reporting it as the current version's result. In the manual check, compare ds.key as well as the document. When a failed report is ignored because the contract is gone, mark the mirror and email as 'not sent' or fail the run, instead of leaving them pending.

*Verification:* confirmed (bassa); confirmed (bassa).

#### `gap-1-3-5` — accepted_values on datetime columns matches only Polars' cast text, which changes with the time unit, so ISO values (even the value shown in the report's sample) never match

Gravità **bassa** (proposta: bassa), sforzo S. Corsia `gap-1-3`.

Dove: `backend/app/contracts/evaluator.py:160-175`, `backend/app/contracts/evaluator.py:275-281`

**Description.** For families other than numeric and boolean, accepted_values compares `c.cast(pl.String)` with `str(v)`. Datetime columns cast to '2026-10-01 00:00:00.000000' for us, '.000' for ms and '.000000000' for ns. None of the forms a person would type ('2026-10-01T00:00:00', '2026-10-01 00:00:00', '2026-10-01') match, including the ISO text the report shows as the sample (_plain uses isoformat). The time unit depends on the source and engine that wrote the parquet, so the same contract passes or fails depending on who produced the data. Two smaller coercion problems sit in the same code. If one entry in a numeric list is not a number, the whole list falls back to text comparison, so 1.0 no longer equals '1'. Integers are compared after a cast to Float64, so values above 2^53 collide.

**Impact.** A blocking accepted_values rule on a datetime column refuses every update. A warning rule reports every row as a violation.

**Evidence.** test_zz_contracts.py and test_zz_contracts2.py. A datetime(2026,10,1) column with value '2026-10-01 00:00:00', with '2026-10-01T00:00:00' and with '2026-10-01' each gives `passed: false, violations: 1, sample: ["2026-10-01T00:00:00"]`. Only '2026-10-01 00:00:00.000000' passes. Cast to text: ms `2026-10-01 00:00:00.000`, us `...000000`, ns `...000000000`. Float column [1.0, 2.0] with values [1, 2, 'n/a']: `violations: 2, sample: [1.0, 2.0]`. Int64 9007199254740993 with values [9007199254740992]: `passed: true`.

**Fix.** Parse the values into the column's type with _literal for the date and datetime families and compare typed (`c.is_in([...])`). Compare integer columns as Int64 when every value is integral. Coerce values one at a time (drop or flag only the ones that do not parse) instead of switching the whole list to text.

*Verification:* confirmed (bassa); confirmed (bassa).

#### `gap-1-3-6` — Two orchestrators can record the same freshness transition twice, and each copy sends its own email

Gravità **bassa** (proposta: bassa), sforzo S. Corsia `gap-1-3`.

Dove: `gateway/app/services/contracts.py:352-355`, `gateway/app/services/contracts.py:359-363`, `gateway/app/services/scheduler.py:313-324`, `gateway/app/services/contract_notifier.py:141-148`, `docs/specs/data-contracts.md:199-201`

**Description.** sweep_freshness runs on every orchestrator's 60 s tick, with no lock. refresh_freshness compares the new outcome with the c.status it has already loaded, then inserts a DataContractResult that carries notify='pending'. If two orchestrators read the contract before either commits, both write a result. The notifier deduplicates per result row, not per transition, so two emails go out. The docstring accepts this ('al più scrivono lo stesso referto due volte'), but the spec and the notifier promise 'several orchestrators send one mail'. The window is short (read to commit, a few ms per contract), so a duplicate is unlikely on any single transition. A sweep that reads a datasource just before another replica swaps its snapshot can, in the same way, record a spurious failed state followed by a passed one.

**Impact.** An occasional duplicate 'contract violated' email and a duplicate history row. prodtest has 0 contracts, so I could not check whether this has happened there.

**Evidence.** test_zz_contract_gateway.py::test_two_sweeps_interleaved. Two Sessions load the contract, then each runs refresh_freshness and commits: `[changed A=true, changed B=true, results: refresh passed, freshness failed notify=pending, freshness failed notify=pending]`. Rough estimate with two orchestrators at random tick phases: about 2 x 5 ms / 60 s, roughly 1.7e-4 per transition.

**Fix.** Make the transition conditional: `UPDATE data_contracts SET status=:new ... WHERE id=:id AND status=:before`, and insert the result and pending notification only when rowcount is 1. Alternatively, run the sweep under a Postgres advisory lock.

*Verification:* confirmed (bassa); refuted (bassa); confirmed (bassa, tie-break).

#### `gap-1-3-7` — ODCS export drops the full-match meaning of pattern rules: logicalTypeOptions.pattern is the raw, unanchored regex

Gravità **bassa** (proposta: bassa), sforzo S. Corsia `gap-1-3`.

Dove: `gateway/app/services/odcs.py:171-177`, `backend/app/contracts/evaluator.py:208-209`

**Description.** Tabularia evaluates a pattern against the whole value (`^(?:regex)$`). The ODCS export writes `logicalTypeOptions.pattern: <regex>` unchanged. ODCS follows JSON Schema / ECMA-262, where a pattern is not implicitly anchored, so a consumer tool reads '[A-Z]{3}' as 'contains three capitals' and accepts 'ABC-anything', which Tabularia rejects. Only the free-text quality item says 'in full'. Severities and freshness (latency, unit h) do carry over.

**Impact.** Catalogs or tools that enforce the exported contract apply a weaker check than Tabularia for every pattern written without ^ and $.

**Evidence.** test_zz_odcs_validate.py: regex `[A-Z]{3}` is exported as `logicalTypeOptions: {"pattern": "[A-Z]{3}"}`. The existing test_odcs.py:104 asserts the raw 'x' is exported unchanged.

**Fix.** Export `^(?:<regex>)$` in logicalTypeOptions.pattern, or add the anchors only when the regex does not already start with ^ and end with $.

*Verification:* confirmed (bassa); confirmed (bassa).

### P8 — Editor e interfaccia

#### `frontend-correctness-3` — Global search hits do nothing when they point to the page already open (Explore folder, Datasources, Connections, Viewer)

Gravità **media** (proposta: media), sforzo S. Corsia `frontend-correctness`.

Dove: `frontend/composables/useSearch.ts:31`, `frontend/components/ui/GlobalSearch.vue:91`, `frontend/components/ProjectBrowser.vue:284`, `frontend/pages/datasources.vue:25`, `frontend/pages/datasources.vue:28`, `frontend/pages/connections.vue:24`, `frontend/pages/connections.vue:26`, `frontend/pages/viewer.vue:398`

**Description.** Nuxt 3.21 keys pages by the interpolated route path, not the query (generateRouteKey in nuxt/dist/pages/runtime/utils.js), and no page defines a key. Going from /?folder=1 to /?folder=3, from /datasources to /datasources?q=x&ds=7, or from /viewer to /viewer?view=5 therefore reuses the mounted component. These pages read the query only once: ProjectBrowser in onMounted, datasources and connections through usePagedList initialQuery plus a `const target`, viewer in onMounted. Only flow-group.vue watches route.query.id. The top search is in AppShell on every page, so its folder, datasource, connection and view hits silently fail whenever the user is already on the matching page. Browser back and forward between such URLs fail the same way.

**Impact.** The URL changes but the content does not: Explore still shows the old folder, and the Datasources list is neither filtered nor expanded. The search looks broken in exactly the situation where it is most used (searching the catalog from the catalog). A reload fixes it.

**Evidence.** utils.js:8-12: the key is `interpolatePath(route, matchedRoute)`, the path only. ProjectBrowser.vue:284 `const linked = Number(route.query.folder)` inside onMounted, with no route watcher in the file. datasources.vue:25 `initialQuery: String(useRoute().query.q ?? '')` and :28 `const target = Number(useRoute().query.ds)`. GlobalSearch.vue:91-95 `vai()` → `navigateTo(searchHitTarget(hit))`.

**Fix.** Watch route.query in these four pages (open the folder, reset q and target, open the view), or give them `definePageMeta({ key: r => r.fullPath })`.

*Verification:* confirmed (media); confirmed (media).

#### `frontend-correctness-4` — Delete, copy and cut in the editor act on the panel's node, not the node selected on the canvas after a drag or keyboard selection

Gravità **media** (proposta: media), sforzo S. Corsia `frontend-correctness`.

Dove: `frontend/components/FlowEditor.vue:705`, `frontend/components/FlowEditor.vue:983`, `frontend/components/FlowEditor.vue:1104`, `frontend/node_modules/@vue-flow/core/dist/vue-flow-core.mjs:4967`, `frontend/node_modules/@vue-flow/core/dist/vue-flow-core.mjs:1294`, `frontend/node_modules/@vue-flow/core/dist/vue-flow-core.mjs:9683`

**Description.** selectedId (the node in the panel) is set only by onNodeClick. Vue Flow selects a node in two other ways. On drag start, selectNodesOnDrag defaults to true and handleNodeClick deselects the others (4967-4985); d3-drag then suppresses the click after a moving drag (yesdrag with mousemoving, 1294-1301 and 1371-1377). The keyboard Enter key also selects a node (9683-9697) without emitting click. selectedIds() returns [selectedId] whenever the canvas selection holds a single node, so Delete, Backspace, Ctrl+C, Ctrl+X and Ctrl+D apply to the panel node and not to the highlighted one.

**Impact.** A user clicks node A, drags node B to tidy the layout (B now shows as selected) and presses Delete: A is deleted, and A may be off-screen. Ctrl+Z restores it, but the user may not notice before saving. Keyboard users who select a node with Enter hit the same mismatch.

**Evidence.** FlowEditor.vue:983-987: `const multi = getSelectedNodes.value.map(n => n.id); if (multi.length > 1) return multi; return selectedId.value ? [selectedId.value] : multi`. FlowEditor.vue:1104-1107: Delete → `deleteNodes(selectedIds())`. selectNodesOnDrag default true (vue-flow-core.mjs:6208). Not reproduced in a browser: launching a headless browser container was denied.

**Fix.** Keep selectedId in sync with Vue Flow's selection (onSelectionChange or onNodeDragStart), and have selectedIds() prefer getSelectedNodes whenever it is non-empty.

*Verification:* confirmed (media); confirmed (media).

#### `gap-1-4-1` — Editing a connection after another type was open blanks its database and schema (and a SharePoint tenant and library), and Save stores the blanks

Gravità **media** (proposta: alta), sforzo S. Corsia `gap-1-4`.

Dove: `frontend/components/ui/ConnectionDialog.vue:63`, `frontend/components/ui/ConnectionDialog.vue:150`, `frontend/components/ui/ConnectionDialog.vue:101`, `frontend/pages/connections.vue:60`, `frontend/components/ProjectBrowser.vue:617`, `gateway/app/routes/connections.py:381`

**Description.** The Connections page and the Explore folder each keep one ConnectionDialog instance and reuse it. When it opens, the `open` watcher (line 63) copies the stored connection into the form, including `dbType`. That assignment also triggers `watch(dbType, …)` at line 150, which treats the change as a user switching type. When the old or new type is SharePoint, the watcher clears `port`, `dbSchema`, `database` and `library`, in the same flush, after the form was filled. `draft()` always sends `database` and `db_schema`, and the gateway PATCH writes any non-None value (connections.py:381), so the empty strings replace the stored values. `port` goes out as null and survives, because None means unchanged on the server.

**Impact.** Case 1: a user opens a SharePoint connection, then opens a Postgres, MySQL, Oracle, Trino or S3 connection in the same page visit and saves any change, even just the description. The stored database (Oracle service name, Trino catalog, S3 bucket) and schema (S3 region) become ''. Every datasource and Output that uses that shared connection then fails, or reads another database or schema: Postgres falls back to the database named after the user and to `public`. Case 2: a SharePoint connection opened first, or after a non-SharePoint one, shows an empty tenant and library. Save stays blocked until the tenant is typed again. The library is silently reset to the default document library, so imports read different files.

**Evidence.** I compiled the real SFC with @vue/compiler-sfc and mounted it with a no-DOM Vue renderer in a throwaway node container (--network none). [A] Edit SharePoint as the first open: tenant field "", library "", incomplete=true. [B] Then edit Postgres {database:'erp_prod', db_schema:'sales', port:6543}: the form shows database "", port "", schema "". Save emits {host:'erp-db', port:null, database:'', db_schema:'', password:''}. The page deletes only the empty password (connections.vue:67, ProjectBrowser.vue:619). The gateway loop at connections.py:381-384 sets database and db_schema to ''.

**Fix.** Clear fields only when the user changes the type. Move the reset into the type Select's update handler, or set a `loading` flag in the open watcher and skip the dbType watcher while it is set. For safety, PATCH only the fields that differ from `props.existing`.

*Verification:* confirmed (media); confirmed (media).

#### `gap-1-4-2` — The parameter form rebuilds only when the node id changes: Undo, Redo and a change of operation type leave stale values that the next edit writes back

Gravità **media** (proposta: alta), sforzo M. Corsia `gap-1-4`.

Dove: `frontend/components/ui/ParamForm.vue:168`, `frontend/components/ui/ParamForm.vue:108`, `frontend/components/ui/ParamForm.vue:321`, `frontend/components/ui/NodePanel.vue:41`, `frontend/components/ui/NodePanel.vue:693`, `frontend/components/FlowEditor.vue:419`, `frontend/components/FlowEditor.vue:795`

**Description.** ParamForm copies `props.params` into local state only in `watch(() => props.nodeId, rebuild)`. NodePanel reuses the same ParamForm instance without a key, so a new instance does not reset the state either. Undo and Redo (`restoreSnapshot` → `applyDefinition`) restore the node with the same id and keep it selected. Changing the operation in the Type select (`changeType`) sends a new opType with default params on the same id. Neither triggers a rebuild. The form then shows values that are not in the node, and `build()` sends them back on the next change. The source-filter forms (NodePanel.vue:228) behave the same way.

**Impact.** Ctrl+Z after a parameter change is silently reverted by the next edit to any field of that node. After a type change the form shows selections that are not stored. Example: Drop nulls with subset [id] switched to Unique keeps 'id' checked while the node has {}, so it dedups on all columns. Pivot switched to Unpivot works the same way. Select switched to Drop shows the old columns checked while the node has no columns, then sends them all at the next click. Sort switched to Group by keeps `by` as a string: every key checkbox throws TypeError, `isChecked` matches substrings, and the next aggregation edit sends by:'region' as a string. The toolbar's '+ Operation' creates a 'cast' node that must be retyped (FlowEditor.vue:803), so changing the type is a normal path.

**Evidence.** Real ParamForm.vue compiled and mounted in a throwaway node container. Undo: params {value:'North'} → edit to 'South' → undo restores {value:'North'}. The form still shows "South". Changing the operator then stores {operator:'ne', value:'South'}. sort→group_by: state.by="region", isChecked(by,'reg')=true, toggleColumn throws "TypeError arr.push is not a function", emitted {"by":"region",…}. select→drop: checked [a,b], stored {}, next tick emits [a,b,c]. drop_nulls→unique: subset ["id"] shown, stored {}.

**Fix.** Rebuild on `[nodeId, opType]` and whenever `props.params` is not the object the form last emitted. Keep `lastEmitted` in `emitUpdate`, and use `watch(() => props.params, p => { if (p !== lastEmitted) rebuild() })`. Alternatively key ParamForm by node id, opType and a history version counter.

*Verification:* confirmed (media); confirmed (media).

#### `gap-1-4-3` — Filter and fill values are parsed as JSON whatever the column type: text codes become numbers, and an empty value becomes null and keeps zero rows

Gravità **media** (proposta: media), sforzo M. Corsia `gap-1-4`.

Dove: `frontend/components/ui/ParamForm.vue:93`, `frontend/components/ui/ParamForm.vue:250`, `frontend/components/ui/ParamForm.vue:277`, `frontend/components/ui/ValuePicker.vue:52`, `backend/app/engine/duckdb_ops.py:151`, `backend/app/engine/operations.py:142`

**Description.** `parseValue` runs JSON.parse on the text typed in the filter value and in fill_null rows, ignoring the column dtype that the form already has. So '20121' becomes 20121, 'true' becomes true and '1e3' becomes 1000. The 'is one of' picker's free value does the same (ValuePicker.vue:52). An empty value becomes null and is still sent: the operation filter receives {value:null}, both engines evaluate `col = NULL`, and every row is dropped. The same condition on a source filter counts as incomplete and is ignored (useFlowModel.ts:47). Users cannot filter for an empty string either.

**Impact.** Filtering or filling a text column of codes (postcodes, customer or product codes) by typing digits either fails or matches the wrong rows. Polars errors with "cannot compare string with numeric type". DuckDB casts the whole column to INTEGER: if one value is not numeric the run fails with a conversion error, otherwise codes with different leading zeros match each other. A filter node left without a value raises no error, previews 0 rows, and a run publishes an empty datasource.

**Evidence.** Harness on the real ParamForm: typed "20121" → value 20121 (number), "1e3" → 1000, "true" → true, ""/"null" → null. Engine check with the dev backend image, --network none. Text column ["00144","144","0144","20121"] with eq 144: DuckDB returns ['00144','144','0144'] and Polars raises ComputeError. With 'abc' in the column, DuckDB raises "Could not convert string 'abc' to INT32". gt 1000: DuckDB BinderException. in [144,20121]: DuckDB returns all four rows. fill_null {code: 0} on a text column: DuckDB BinderException "Cannot mix VARCHAR and INTEGER_LITERAL in COALESCE". eq/ne null: 0 of 4 rows on both engines.

**Fix.** Coerce by dtype. For String columns keep the text, and parse JSON only for list syntax in in/between. For numeric columns parse numbers. Do not send `value` when the box is empty. Make engines reject a filter whose operator needs a value but has none, as the source-filter path already does.

*Verification:* confirmed (media); confirmed (media).

#### `gap-1-4-4` — Schedule dialog: 6-field crons are described with seconds first but run by croniter with seconds last, and the description is always in Italian

Gravità **media** (proposta: media), sforzo S. Corsia `gap-1-4`.

Dove: `frontend/components/ui/ScheduleDialog.vue:82`, `frontend/components/ui/ScheduleDialog.vue:115`, `gateway/app/services/schedule.py:31`, `gateway/app/services/scheduler.py:150`

**Description.** The dialog describes the expression with cronstrue, which reads a 6-field cron as seconds-first (Quartz/Spring style). Its validity check uses that description. The gateway accepts anything `croniter.is_valid` accepts. croniter reads 6 fields with seconds LAST and never enforces 5 fields. The same text therefore means two different schedules. The description is also hard-coded to `locale: 'it'` in all five UI languages. The Enter key on the input sends save without the `!cronDescription.ok` and `busy` checks that the button applies. The timezone label is correct: /system/info returns APP__TIMEZONE, and the dialog has no next-run preview to disagree with it.

**Impact.** A user pastes '0 0 3 * * *' and the dialog shows 'At 03:00'. The server schedules it for 00:00:00 to 00:00:59 on day 3 of each month. With the 60 s tick that is one or two runs a month at midnight, not a daily 03:00 run, and nothing warns about it. English, French, German and Spanish users read the description in Italian, e.g. 'Alle 06:00, solo il lunedì'.

**Evidence.** cronstrue 3.24.0 (dev image): '0 0 3 * * *' → it 'Alle 03:00' / en 'At 03:00 AM'. Gateway dev image: validate_schedule accepts it, and next fires with APP__TIMEZONE Europe/Rome are 2026-11-02 23:00:00, 23:00:01, 23:00:02 UTC. '30 0 3 * * *' is described as 03:00:30 but fires at 23:30:00 UTC on the 2nd. '0 3 * * * 2027' is described as valid and rejected by the server.

**Fix.** In validate_schedule, require exactly 5 whitespace-separated fields, plus any macros you choose to allow. In the dialog, reject anything that is not 5 fields before calling cronstrue, pass the current i18n locale (cronstrue ships en/it/fr/de/es) and `use24HourTimeFormat`, and route Enter through the same guarded save as the button.

*Verification:* confirmed (media); confirmed (media).

#### `gap-1-4-5` — Stale column names stay in a node's params but are hidden in the form, so a renamed or removed upstream column breaks the node and cannot be unticked

Gravità **media** (proposta: media), sforzo M. Corsia `gap-1-4`.

Dove: `frontend/components/ui/ParamForm.vue:413`, `frontend/components/ui/ParamForm.vue:236`, `frontend/components/ui/ParamForm.vue:172`, `frontend/components/ui/ParamForm.vue:180`, `frontend/components/ui/ParamForm.vue:290`, `frontend/components/ui/ParamForm.vue:628`

**Description.** Checkbox lists (select, drop, group_by keys, unique and drop_nulls subset, pivot and unpivot index/on) render only the current input columns. A selected name that no longer exists upstream stays in `state[key]` and is sent with every toggle, but has no checkbox to untick. Join key rows hold a value missing from the options: the Select shows the empty placeholder while `build()` still sends the old key. For reorder, `syncReorder` removes the missing column from the displayed list but emits nothing, so the stored `columns` keep it.

**Impact.** After an upstream rename, a datasource switch or a removed select, preview and run fail with ColumnNotFound while the form looks valid. For checkbox lists the only way out is to delete and recreate the node, or switch its type (which itself carries stale state, see the stale-state finding).

**Evidence.** Harness on the real ParamForm. select {columns:[a,b]} with input [a,x]: rendered checkboxes ["a","x"], and ticking x emits {columns:["a","b","x"]}. reorder {columns:[c,b,a]} with input [a,c,d]: form ["c","a","d"], stored ["c","b","a"], 0 emits. Engines: Polars ColumnNotFoundError "b" for reorder, select and drop. DuckDB: 'Column "b" in EXCLUDE list not found' (reorder) and 'Referenced column "b" not found' (select).

**Fix.** Show selected names that are missing from the input as checked items marked 'missing', so they can be unticked. Show a stale join key as a value with a warning instead of the placeholder. In syncReorder, emit when the synced list differs from props.params.columns.

*Verification:* confirmed (media); confirmed (media).

#### `frontend-perf-4` — Page loads chain independent requests one after another; Explore reloads everything with a skeleton on every tab focus and has no guard against stale responses

Gravità **media** (proposta: media), sforzo M. Corsia `frontend-perf`, segnalato anche da `measure`, `frontend-correctness`.

Dove: `frontend/components/ProjectBrowser.vue:234-280`, `frontend/components/ProjectBrowser.vue:282-288`, `frontend/components/ProjectBrowser.vue:291-301`, `frontend/components/ProjectBrowser.vue:784`, `frontend/components/FlowEditor.vue:530-562`, `frontend/components/FlowEditor.vue:1115-1122`, `frontend/pages/viewer.vue:386-400`, `frontend/pages/lineage.vue:188`

**Description.** loadFolder() awaits flows, datasources, flow-groups, saved-views, connections and permissions (plus groups and users for admins) one by one; the Explore mount adds /projects before and /engines after. On every visibilitychange to visible, Explore runs loadProjects() and loadFolder() again. loadFolder empties every list, closes the expanded run history, restarts the import polls and sets loading=true, which swaps the table for SkeletonRows. Returning to the tab therefore blanks the list, the 'reads as a crash' effect the owner removed from Datasources in 835785e. loadFolder has no request token, so when folders are switched quickly a slower response from the previous folder (flows, permissions/canManage) can land in the new one. The editor waits for 6 serial requests before it loads the flow (fetchMe, /system/info, /tasks/operations, /projects, /datasources, then the flow) and refetches /connections, the full /datasources catalog and /flows on every tab focus. The Viewer chains 4 requests. Lineage fetches the full /lineage graph twice on mount. /projects is fetched twice on Explore, editor and Viewer (useFolderPaths plus the page's own list). AdminPanel chains 11 requests and reruns all of them after every toggle.

**Impact.** Each serial request costs one extra round trip. On the local stack that is about 50 ms per Explore folder open; for a remote user with a 30 ms RTT it is about 270 ms on every folder open and every return to the tab, plus a visible skeleton flash. Every Explore tab focus costs 7 requests and every editor tab focus 3 (including the full catalog with schemas).

**Evidence.** Measured on prodtest (idle, localhost, admin, folder 'Private tables'): the Explore chain of 10 requests takes 95.4 ms sequentially vs 48.0 ms with 6 in parallel (median of 5). The editor mount chain of 7 requests takes 56.9 ms sequentially. Single requests: /projects 7.4 ms, /projects/3/datasources 14.9 ms (21 KB), /flows 11.5 ms, /datasources 12.2 ms (24 KB), /lineage 25.2 ms.

**Fix.** Use Promise.allSettled for independent loads (as flow-group.vue:262 already does). On tab focus do a quiet in-place refresh (no skeleton, keep the expanded and moving state, like usePagedList's load({quiet})) and throttle it to once every 30-60 s. Guard loadFolder with a sequence token. Reuse useFolderPaths for /projects, fetch /lineage once, and in AdminPanel reload only the list that changed.

*Verification:* confirmed (media); confirmed (media).

#### `frontend-correctness-2` — Saving after a failed flow load overwrites the stored flow with an empty canvas

Gravità **media** (proposta: media), sforzo S. Corsia `frontend-correctness`.

Dove: `frontend/components/FlowEditor.vue:550`, `frontend/components/FlowEditor.vue:554`, `frontend/components/FlowEditor.vue:445`, `frontend/components/ui/Toolbar.vue:104`, `frontend/components/ui/Toolbar.vue:113`

**Description.** onMounted catches an error from loadFlow (a transient 5xx, a 502 while a gateway replica restarts, a network blip) and only sets a status message. flowId keeps the URL id. The canvas stays at the initial empty source node, the name stays 'Unnamed flow' and dirty is false. projectId is null (links open /editor?flow=N with no project), so the toolbar shows the folder picker and Save stays enabled. Save, or Ctrl+S, asks for a folder. After one is picked, saveFlow calls flowsApi.update(flowId, {name: 'Unnamed flow', definition: <empty canvas>}).

**Impact.** The existing flow is overwritten: its definition becomes the empty or half-rebuilt canvas, and its name becomes 'Unnamed flow'. Scheduled runs of that flow then run the new definition. An admin can restore the old definition with promote, because PATCH snapshots a version, but nobody is told this is needed. A user who sees an 'empty' flow is likely to rebuild it and save.

**Evidence.** FlowEditor.vue:550-561: `try { if (flowId.value !== null) await loadFlow(flowId.value) } catch (e) { setStatus(t('flowEditor.flowLoadFailed', ...), 'error') }` leaves flowId set. saveFlow (445-470) runs `flowsApi.update(flowId.value, { name: flowName.value.trim(), definition: definizione })`. Toolbar.vue:105 shows the picker when `projectId === null`, and line 113 enables Save whenever `!busy`.

**Fix.** Track a loadFailed state. While it is set, disable Save, Run and Ctrl+S, and show a retry action instead of the canvas, or drop flowId so that saving creates a new flow.

*Verification:* confirmed (media); confirmed (media).

#### `frontend-correctness-8` — 422 validation errors show as raw JSON or '[object Object]'

Gravità **bassa** (proposta: bassa), sforzo S. Corsia `frontend-correctness`.

Dove: `frontend/composables/useApi.ts:371`, `frontend/composables/useAi.ts:129`, `frontend/components/AdminPanel.vue:795`, `frontend/pages/chat.vue:556`, `frontend/components/FlowEditor.vue:1683`

**Description.** errMessage returns e.data.detail unchanged. For FastAPI validation errors that value is an array. ToastHost renders `{{ t.message }}`, so Vue prints the pretty-printed JSON array. Where the message is concatenated (e.g. `${label}: ${errMessage(e)}`) or wrapped in String() (useAi.ts:129), it reads '[object Object]'. Two inputs reach this today: a banner message longer than 500 characters (the input has no maxlength, the schema caps it at max_length=500) and an assistant message longer than 8000 characters (the textarea has no maxlength, ai.py:123). The fallback 'Errore sconosciuto' is Italian. DbtExportDialog.vue:181-182 already maps arrays correctly.

**Impact.** Admins and users get an unreadable error instead of a reason.

**Evidence.** Measured: GET /api/runs?limit=0 returns 422 `{"detail":[{"type":"greater_than_equal","loc":["query","limit"],"msg":"Input should be greater than or equal to 1",...}]}`. useApi.ts:372: `return e?.data?.detail ?? e?.data?.message ?? e?.message ?? 'Errore sconosciuto'`.

**Fix.** In errMessage (and in useAi), when detail is an array join the items' msg fields, and when it is an object use detail.message; translate the fallback. Add maxlength to the banner input and the chat textarea.

*Verification:* confirmed (bassa); confirmed (bassa).

#### `frontend-correctness-11` — After the session ends, the editor re-prompts every 15 s, and login loses the page the user was on

Gravità **bassa** (proposta: bassa), sforzo S. Corsia `frontend-correctness`.

Dove: `frontend/composables/useFlowPresence.ts:28`, `frontend/composables/useApiClient.ts:28`, `frontend/pages/login.vue`, `frontend/middleware/auth.global.ts:188`

**Description.** After a 401 (session expired or revoked), the presence heartbeat keeps POSTing /flows/{id}/presence every 15 s: beat() always calls schedule(), even on error. Each 401 calls navigateTo('/login'), which triggers the editor's leave guard again. A user who chose 'Stay' to copy nodes, as the design intends, gets the unsaved-changes dialog back every 15 s. Separately, the auth middleware redirects to /login without the original path, and login always goes to '/'. A deep link such as /editor?flow=12 sent in chat, or the page the user was on when the session ended, is lost after signing in.

**Impact.** Recovering work after a session expiry is harder than it needs to be, and each open editor tab keeps sending a stream of 401s.

**Evidence.** useFlowPresence.ts:28-46: the catch sets `others.value = []`, then `schedule()`. useApiClient.ts:28-32: `if (code === 401) { dropSession(); navigateTo('/login') }`. gateway services/flow_presence.py:27: HEARTBEAT_SECONDS = 15. login.vue onSubmit: `await navigateTo('/')`.

**Fix.** Stop the heartbeat while !present (and on a 401). Pass `?redirect=<fullPath>` from the middleware and the 401 handler, and honour it after login and SSO (same-origin paths only).

*Verification:* confirmed (bassa); confirmed (bassa).

#### `frontend-correctness-12` — Editor pollers outlive the page or die silently

Gravità **bassa** (proposta: bassa), sforzo S. Corsia `frontend-correctness`.

Dove: `frontend/components/FlowEditor.vue:755`, `frontend/components/FlowEditor.vue:1742`

**Description.** pollConversion, used for large uploads, has no pollToken or unmount check. After the user leaves the editor it keeps calling GET /tasks/{id} every 2 s until the conversion ends, for up to 1800 iterations (1 h). It then toasts 'ready' on whatever page is open and calls updateNodeData and refreshForNode on the unmounted editor, which can start a preview. In pollTask (unsaved flows), `await api.taskStatus(id)` is outside any try, and the caller does not await it. One transient error becomes an unhandled rejection, and the toolbar status stays 'busy' forever.

**Impact.** Stray requests, and possibly a stray preview, after leaving the page; a stuck status after a network blip.

**Evidence.** FlowEditor.vue:755-785: the loop has no `token !== pollToken` check, unlike pollRun and pollOrchestration. FlowEditor.vue:1742: `const st = await api.taskStatus(id)` with no try/catch.

**Fix.** Capture pollToken in pollConversion and stop when it changes (unmount already bumps it). Wrap taskStatus in pollTask in try/continue, as the other pollers do.

*Verification:* confirmed (bassa); confirmed (bassa).

#### `frontend-correctness-13` — Datasource documentation drafts are lost on navigation with no warning

Gravità **bassa** (proposta: bassa), sforzo S. Corsia `frontend-correctness`.

Dove: `frontend/pages/datasources.vue:55`, `frontend/pages/datasources.vue:95`

**Description.** The Datasources page lets users write a datasource description (up to 4000 characters), per-column descriptions and sort keys, then save them with one button. isDirty() exists, but there is no onBeforeRouteLeave or beforeunload guard. Only the editor and the flow-group page guard unsaved changes, and ContractDialog asks before discarding.

**Impact.** Documentation written for several columns disappears silently when the user clicks another nav link.

**Evidence.** A grep for onBeforeRouteLeave and beforeunload in pages/datasources.vue finds nothing; isDirty is defined at about lines 95-99 and is used only for the button state.

**Fix.** Add onBeforeRouteLeave and a beforeunload handler that check `items.some(isDirty)`, like flow-group.vue.

*Verification:* confirmed (bassa); confirmed (bassa).

#### `gap-1-4-6` — Connection 'Test' in edit mode tests the saved connection, not the edited host, database or user, and reports OK

Gravità **bassa** (proposta: bassa), sforzo S. Corsia `gap-1-4`.

Dove: `frontend/components/ui/ConnectionDialog.vue:164`, `frontend/components/ui/ConnectionDialog.vue:170`

**Description.** When the password field is empty, which the dialog presents as 'unchanged', `test()` calls `connApi.test(existing.id)`. That tests the stored connection and ignores every unsaved edit to host, port, database, username, type or TLS options. This is not the known retarget issue: no secret leaves the server here. The problem is that the result is shown for a configuration the user is not about to save.

**Impact.** A user moves a shared connection to a new host or database, presses Test and sees 'Connection OK', then saves a target that does not work. Every scheduled import and DB output on that connection then fails.

**Evidence.** Harness on the real ConnectionDialog: edit {id:8, host:'erp-db'} and set host='new-erp-db.internal' and database='erp_new'. test() called ["test",8], and testResult was {ok:true, message:'connectionDialog.testOk'}.

**Fix.** When any field differs from `props.existing`, call testDraft and ask for the secret again. That fits the fix for the known retarget finding (require the password when the target changes). Otherwise label the result 'saved connection'.

*Verification:* confirmed (bassa); confirmed (bassa).

#### `gap-1-4-7` — The palette and the Type select offer 'Sample rows', which has no parameter form, so every such node fails

Gravità **bassa** (proposta: bassa), sforzo S. Corsia `gap-1-4`.

Dove: `frontend/composables/useFlowModel.ts:288`, `frontend/components/ui/NodePanel.vue:670`, `frontend/components/ui/OpSidebar.vue:26`, `backend/app/engine/operations.py:225`

**Description.** GET /tasks/operations lists 'sample', and the palette and Type select show every operation except foreach. OP_SPECS has no 'sample' entry, so the form is empty and the node's params stay {}. Both engines require `fraction`.

**Impact.** Users who pick 'Sample rows' get a node that can never preview or run, with the Italian engine message "parametro obbligatorio mancante: 'fraction'".

**Evidence.** Live prodtest: GET /api/tasks/operations returned ['cast','compute',…,'reorder','sample','select',…] (one login, one GET, logout 204). OP_SPECS keys: select, reorder, drop, rename, cast, filter, sort, limit, unique, fill_null, drop_nulls, group_by, pivot, unpivot, compute, sql, join, union, foreach. en.ts:166 labels it 'Sample rows'.

**Fix.** Add an OP_SPECS entry for sample (a percentage converted to fraction, plus a seed), or filter 'sample' out of the palette and Type lists, since it exists for the development sample.

*Verification:* confirmed (bassa); confirmed (bassa).

#### `gap-1-4-8` — A typo in the foreach static items JSON silently deletes the saved items from the node

Gravità **bassa** (proposta: bassa), sforzo S. Corsia `gap-1-4`.

Dove: `frontend/components/ui/ParamForm.vue:259`, `frontend/components/ui/ParamForm.vue:513`

**Description.** For the 'json' control, `build()` leaves the key out when JSON.parse fails, to avoid sending broken JSON. But `build()` creates a new params object that replaces the old one entirely, so leaving the key out removes the last valid `items`. No error is shown next to the textarea.

**Impact.** One blur with a typo turns a statically driven foreach into 'foreach senza iterazioni'. Once the user selects another node, the textarea comes back empty, and saving the flow loses the items.

**Evidence.** Harness: params {items:[{paese:'IT'},{paese:'FR'}], add_keys_as_columns:true}. After a missing closing bracket, it emitted {"add_keys_as_columns":true}.

**Fix.** When the JSON is invalid, keep `props.params[f.key]` in the output and show an inline parse error.

*Verification:* confirmed (bassa); confirmed (bassa).

#### `gap-1-4-9` — The 'is one of' picker reuses another node's distinct values for a column with the same name

Gravità **bassa** (proposta: bassa), sforzo S. Corsia `gap-1-4`.

Dove: `frontend/components/ui/ParamForm.vue:196`, `frontend/components/ui/ParamForm.vue:198`, `frontend/components/FlowEditor.vue:1275`

**Description.** `distinctCache` is keyed only by column name and lives as long as the ParamForm instance. That instance is reused when the user clicks from one operation node to another, and `rebuild()` never clears the cache. fetchDistinctValues computes values for the selected node's own input chain, but after the first node the cache returns the earlier node's list.

**Impact.** A filter after a different branch, join or filter lists values from another point of the flow: missing values or values that do not exist. Users pick from a misleading list (free values remain possible).

**Evidence.** Harness: node A with column 'region' and operator 'in' shows ["North"]. Selecting node B (same column) shows ["North"] again, fetchDistinct calls = 1, and B's real values ['North','South','East'] are never fetched.

**Fix.** Clear distinctCache and distinctValues in rebuild(), and also when inputColumns change.

*Verification:* confirmed (bassa); confirmed (bassa).

#### `gap-1-4-10` — Import sort keys are split on spaces, so a column name with a space cannot be a sort key

Gravità **bassa** (proposta: bassa), sforzo S. Corsia `gap-1-4`.

Dove: `frontend/components/ui/DbDatasourceDialog.vue:148`, `backend/app/ingest/db_source.py:199`

**Description.** The dialog splits the sort-keys text on `[,;\s]+`. The backend quotes each key for the dialect and supports spaces, and the gateway only trims, but 'Order Date' arrives as two keys, 'Order' and 'Date'.

**Impact.** The first import of a table sorted by a column with a space fails with an unknown-column error.

**Evidence.** Code read: `sortKeysText.value.split(/[,;\s]+/)`. gateway clean_sort_keys (datasources.py:56) only strips. db_source._with_order_by quotes each key per dialect.

**Fix.** Split on commas and semicolons only, and trim each key.

*Verification:* confirmed (bassa); confirmed (bassa).

#### `frontend-perf-5` — Large flows: the editor re-serializes the whole canvas through reactive proxies on every drag frame, and all edges animate forever

Gravità **bassa** (proposta: bassa), sforzo S. Corsia `frontend-perf`.

Dove: `frontend/components/FlowEditor.vue:291-315`, `frontend/components/FlowEditor.vue:416`, `frontend/components/FlowEditor.vue:1796`, `frontend/assets/main.css:604-605`, `frontend/composables/useFlowHistory.ts:39`

**Description.** `watch(serializeCanvas, () => history.touch())` uses the full JSON.stringify of every node (including the source nodes' column schemas in data.columns) and every edge as its watch getter. The getter re-runs on every reactive change, and a drag changes node.position on every pointer move, so the canvas is stringified once per frame through Vue proxies, tracking every property it reads. All edges are also created with animated: true, and main.css runs an infinite stroke-dashoffset animation on them with no prefers-reduced-motion override. The SVG edge layer is repainted every frame for as long as the editor is open, even when idle. The history keeps up to 100 full snapshots.

**Impact.** Dragging stutters on large flows, and an open editor keeps using CPU and battery while idle. Mid-size flows (about 50 nodes) stay within the frame budget. Flows of about 100 nodes with wide sources take most of a 16.7 ms frame before vue-flow renders anything.

**Evidence.** Benchmark of the same getter with @vue/reactivity 3.5.39 (the repo's version) on Node 22 (a desktop CPU; browsers on laptops are slower): 18 nodes / 30 columns per source → 13.6 KB JSON, 1.32 ms per frame (0.04 ms on plain objects); 50 nodes / 80 columns → 51 KB, 3.52 ms (0.16 plain); 100 nodes / 150 columns → 166 KB, 11.83 ms (0.75 plain). History memory for 100 steps: 1.3 / 5.0 / 16.2 MB. The edge-animation cost could not be measured (no browser DevTools in this environment).

**Fix.** Call history.touch() from vue-flow's change events (onNodesChange/onEdgesChange/updateNodeData) or from a cheap version counter instead of a deep getter, and serialize with toRaw only when the change settles. Animate only the edges of a running flow or the selected path, and add a prefers-reduced-motion rule that stops dashdraw.

*Verification:* confirmed (bassa); confirmed (bassa).

#### `frontend-perf-6` — Status polling loops ignore hidden tabs, fetch more than they read, and one of them (/queue) costs 1 s of Celery broadcasts per call

Gravità **bassa** (proposta: bassa), sforzo M. Corsia `frontend-perf`.

Dove: `frontend/components/FlowEditor.vue:1701-1735`, `frontend/components/FlowEditor.vue:1549-1574`, `frontend/components/FlowEditor.vue:756-780`, `frontend/components/ProjectBrowser.vue:309-316`, `frontend/components/ProjectBrowser.vue:346-357`, `frontend/pages/datasources.vue:163-178`, `frontend/pages/flow-group.vue:356-377`, `frontend/components/admin/SiemSection.vue:50-55`

**Description.** Inventory. The steady loops are well behaved: /system/memory every 30 s (10 s in the editor) from one shared poller, paused when hidden; editor presence every 15 s, paused when hidden; the idle ping /auth/me at most once a minute; /queue 2 s after each reply, skipped when hidden. These loops have problems: pollRun polls /runs/{id} every 1.5 s per Output node for up to 1 h, hidden tabs included, and each call makes the gateway query the engine (runs.py:758-781). pollOrchestration gives up after 150 x 2 s = 5 min and shows a timeout while the run goes on. pollConversion has no unmount token: it keeps polling /tasks/{id} every 2 s for up to 1 h after the user leaves the editor, then runs refreshForNode (a preview) on an editor that is gone. The import polls in Explore and Datasources run every 2.5 s per refreshing datasource, hidden tabs included; each GET /datasources/{id}/runs returns up to 50 runs and checks every unfinished one against the engine, while the page reads only runs[0]. The flow-group page and the Explore run history poll every 2.5 s during a run, hidden tabs included. The SIEM section uses setInterval at 10 s and never pauses. Each /queue call waits for 4 Celery inspect broadcasts with a 1 s timeout, and there is no shared cache, so every open Queue tab adds its own broadcasts. Also, AppShell is mounted inside each page, so every navigation remounts MemoryGauge and fires an immediate /system/memory request.

**Impact.** Background budget: about 3 requests/min per user on list pages and about 11/min in the editor, so 30 editors make about 330/min (5.5 req/s), which is negligible. The cost lies in the loops above. A tab left in the background during a 1 h run makes 2,400 /runs calls, each of which queries the engine. A folder with 20 database refreshes in flight makes 480 requests/min from one tab, each returning up to 50 runs (about 22 KB uncompressed at 50 runs, against 446 B at 1 run measured today). Each admin watching the Queue page causes about 20 /queue calls and about 80 inspect broadcasts per minute.

**Evidence.** Measured on prodtest: /queue 1,029-1,066 ms per call (median 1,036 ms), /flows/1/runs 12.3 ms, /datasources/{id}/runs 6.7-7.4 ms (446 B with 1 run), /system/memory 4.6 ms cached and 77.7 ms median including cache misses. Loop bounds read in the code: pollRun 2,400 x 1.5 s, pollConversion 1,800 x 2 s, pollOrchestration 150 x 2 s.

**Fix.** Write one shared poll helper that pauses while document.hidden, backs off (1.5 s → 5 s → 15 s) and stops on unmount; use it for pollRun, the import polls, the flow-group runs, the run history and SIEM. Add ?limit=1 to the import poll. Drop the 5-minute cap for orchestrations, or switch to the backoff. Add the pollToken check to pollConversion. Cache /queue on the backend for about 2 s so all viewers share one inspect. Move AppShell into a Nuxt layout so it survives navigation.

*Verification:* confirmed (bassa); confirmed (bassa).

#### `frontend-perf-7` — The entry bundle ships all five UI languages; the editor preloads ECharts for a tab that is hidden by default; cronstrue ships about 30 locales

Gravità **bassa** (proposta: bassa), sforzo S. Corsia `frontend-perf`.

Dove: `frontend/i18n/index.ts:8-24`, `frontend/plugins/i18n.ts:13-19`, `frontend/components/FlowEditor.vue:1902`, `frontend/components/ui/ChartPanel.vue:28`, `frontend/nuxt.config.ts`

**Description.** i18n/index.ts statically imports en/it/fr/de/es (both the base and the .gen files) and passes them all to createI18n, so every user downloads and parses five catalogues to display one. ChartPanel is statically part of the editor, so the ECharts chunk is preloaded on /editor even though the chart tab is hidden by default (viewTab='table'). The cronstrue chunk used by the schedule dialogs contains about 30 languages.

**Impact.** About 110 KB gzip (about 380 KB raw) of other-language strings on every first visit and after every release, because the entry is cached immutably and changes with each build. The editor's first load is 572 KB gzip of JS, 174 KB of which is ECharts.

**Evidence.** Built bundle in tabularia-frontend-1:/app/.output/public/_nuxt. Entry DY3wlUYq.js is 679,462 B raw, 247,055 B gzip on the wire (Cache-Control: public, max-age=31536000, immutable; gzip/zstd, no brotli). German 'Host-RAM: {used} belegt', Spanish 'RAM del host: {used} en uso' and the Italian and English strings are all in the entry. Locale sources: 438 KB .gen plus 39 KB base (5 x about 26 KB gzip). JS gzip preloaded per page (from the SSR modulepreload links): Explore 288,551 B, editor 572,278 B, flows 522,305 B, audit 448,168 B. ECharts chunk 509,520 raw / 174,033 gz; vue-flow 164,312 / 53,403 (not preloaded on Explore); cronstrue chunk 190,952 / 32,078.

**Fix.** Load locale messages with a dynamic import per locale (SSR loads the cookie's locale; setLocaleMessage on switch). Make ChartPanel a lazy component in the editor and Viewer. Import cronstrue with only the five locales in use.

*Verification:* confirmed (bassa); confirmed (bassa).

#### `frontend-perf-8` — Search boxes can show results for an older query: Audit sends a request on every keystroke and the paged lists have no sequence guard

Gravità **bassa** (proposta: bassa), sforzo S. Corsia `frontend-perf`, segnalato anche da `gateway-queries`, `frontend-correctness`.

Dove: `frontend/pages/audit.vue:43-59`, `frontend/pages/audit.vue:273`, `frontend/composables/usePagedList.ts:165-195`

**Description.** audit.vue watches q through v-model, with no debounce, and calls load() for every keystroke; load() writes whatever response arrives last. usePagedList (Runs, Flows, Datasources, Connections) debounces for 350 ms but also has no sequence token or AbortController. When a short, broad query is slower than the longer one typed after it (more matches, more expensive ILIKE plus count), the older response overwrites the newer one, and the list does not match the search box.

**Impact.** Wrong results on screen while searching. On Audit, typing 'admin' sends 5 server queries. The cost grows with the audit table, which is the table most likely to be large under retention.

**Evidence.** Measured on prodtest (4,072 audit rows): /audit?q=a 20.8 ms, q=ad 23.0 ms, q=adm 18.6 ms, q=zzzz 22.6 ms, no q 13.4 ms. Response order already varies by query. GlobalSearch.vue:48-82 shows the correct pattern (debounce plus seq).

**Fix.** Move Audit onto usePagedList (or debounce it), and add a request sequence (or abort the previous request) in usePagedList.load, as GlobalSearch already does.

*Verification:* confirmed (bassa); confirmed (bassa).

#### `frontend-correctness-6` — Core editor paths are mouse-only: palette nodes cannot be added by keyboard, and keyboard selection does not open the node panel

Gravità **bassa** (proposta: media), sforzo M. Corsia `frontend-correctness`.

Dove: `frontend/components/ui/OpSidebar.vue:61`, `frontend/components/ui/OpSidebar.vue:131`, `frontend/components/ui/Toolbar.vue:122`, `frontend/components/FlowEditor.vue:705`

**Description.** Palette entries are `<div draggable>` with no tabindex, click or keydown handler, so they can only be dropped with a mouse. The toolbar offers keyboard-reachable buttons only for Source and Operation (an operation's type can then be changed in the panel). Output, Refresh, Run-flow, For-each and Note nodes have no keyboard path. Vue Flow nodes are focusable, and Enter selects them, but the editor opens the settings panel only from onNodeClick, which keyboard selection does not emit. A keyboard user can therefore configure only the node just added through the toolbar. Connecting edges requires mouse dragging, apart from the automatic edge added by 'Operation'. The flow-group page, by contrast, has a keyboard picker in every empty cell.

**Impact.** Keyboard and screen-reader users cannot add an Output node (needed to publish to a table, S3 or email) or reopen an existing node's settings on the main authoring page.

**Evidence.** OpSidebar.vue:61-139: every `.item` is a div with only @dragstart. Toolbar.vue:122-123: only add-source and add-op. FlowEditor.vue:705-708: selectedId is set only in onNodeClick. vue-flow-core.mjs:9683-9697: the Enter handler calls handleNodeClick without emit.click.

**Fix.** Render palette items as buttons that add the node near the selected one (reuse addOperation-style placement). Open the panel on Vue Flow's selection change, or on keydown Enter in the custom node components.

*Verification:* confirmed (bassa); confirmed (media).

#### `frontend-correctness-7` — French, German and Spanish are missing 98 keys: Explore, the top search and admin tables appear in English

Gravità **bassa** (proposta: bassa), sforzo M. Corsia `frontend-correctness`, segnalato anche da `quality-architecture`.

Dove: `frontend/i18n/locales/fr.gen.ts`, `frontend/i18n/locales/de.gen.ts`, `frontend/i18n/locales/es.gen.ts`, `frontend/i18n/index.ts:19`, `frontend/plugins/i18n.ts:17`

**Description.** en has 1742 keys and it has all of them. fr, de and es are each missing the same 98 keys: adminPanel 49 (users and groups table columns, banners section, engine policy), projectBrowser 23 (Explore search box, 'New' button, empty states, type segments, delete and revoke titles, the group kind), globalSearch 13 (the whole top search: placeholder, aria-label, 'searching', 'no results', kind headings), connectionDialog 9 (SMTP fields), banners 3 (level names) and flows 1. Each also keeps 17 stale keys under old projectBrowser names. fallbackLocale is en, so no raw keys appear, but these screens mix languages. On the other side, no key used in code is missing from en: 122 files and 1818 literal key references were checked, plus 30 dynamic key families (flowGroups status/trigger/state, queue, integrations, contracts, engineSettings, performance phases) against the gateway and backend enums.

**Impact.** For fr, de and es users, the Explore page and the search box on every page are partly in English, as are the admin users and groups tables.

**Evidence.** A node script flattened {...base, ...gen} per locale and diffed it against en: it 0 missing; fr/de/es 98 missing each, by namespace {adminPanel:49, projectBrowser:23, globalSearch:13, connectionDialog:9, banners:3, flows:1}, plus 17 extra. 85 of the missing keys are referenced as literals in components; the rest are dynamic (kind_*, level*).

**Fix.** Translate the 98 keys in the three gen files and delete the 17 stale ones. Add a key-parity check (the same script) to CI.

*Verification:* confirmed (bassa); confirmed (bassa).

#### `frontend-correctness-14` — Italian error messages in non-Italian UIs: worse than recorded, the gateway also sends them

Gravità **bassa** (proposta: bassa), sforzo L, già noto. Corsia `frontend-correctness`.

Dove: `frontend/composables/useApi.ts:372`, `gateway/app/routes/flows.py`, `gateway/app/routes/webhooks.py`, `frontend/components/FlowEditor.vue:1329`

**Description.** The known item covers 132 engine error messages. In addition, the gateway raises Italian HTTPException details that errMessage shows verbatim in all five UI languages: 144 of the 190 single-line details, e.g. 'Progetto di destinazione non trovato', 'Webhook non trovato', 'Il nome è obbligatorio (al più 120 caratteri)'. The frontend's own fallback is 'Errore sconosciuto', and export filenames use 'sorgente' and 'nodo'.

**Impact.** Error toasts on common admin and catalog actions are in Italian for English, French, German and Spanish users.

**Evidence.** A grep -E count of `HTTPException(... detail="...")` in gateway/app gives 190 single-line details, 144 of them matching Italian function words.

**Fix.** Use the same approach chosen for the engine messages (codes with parameters translated in the frontend, or Accept-Language on the gateway, which the dbt export already honours). Translate the frontend fallback.

*Verification:* confirmed (bassa); confirmed (bassa).

### P9 — Infrastruttura, Helm, test e struttura del codice

#### `gap-1-5-1` — Helm rollback cannot complete: the previous version's migrate init container fails on a database that a newer release has migrated

Gravità **media** (proposta: media), sforzo S. Corsia `gap-1-5`.

Dove: `infrastructure/helm/tabularia/templates/gateway.yaml:61-84`, `gateway/app/db/migra.py:85-101`, `gateway/app/db/migra.py:119-134`, `gateway/app/db/migra.py:174-179`, `infrastructure/helm/tabularia/README.md:276-287`, `docs/database-migrations.md:32-35`

**Description.** Every gateway and orchestrator pod starts with an init container that runs `python -m app.db.migra` (gateway.yaml:61-84). That step calls `command.upgrade(cfg, 'head')` with no check first (migra.py:101). When the database revision is one this code does not know, Alembic raises CommandError and the init container exits with 1. The application's own startup check accepts a newer database (controlla_revisione, migra.py:119-134), and the README and docs/database-migrations.md promise that the previous version keeps working on it (expand/contract). The init step does not follow that rule.

**Impact.** After any release that adds a revision (0006 in the working tree is one), `helm rollback`, the rollback path of `helm upgrade --atomic`, or redeploying the previous image tag leaves every newly created old-version gateway and orchestrator pod in Init:CrashLoopBackOff. Because gateway pods roll with maxUnavailable 0, the new version keeps serving and the rollback just never finishes. An operator rolling back a broken release cannot do it unless they know to set migrations.enabled=false, and nothing documents that. The same thing happens mid-rollout to an old pod that gets recreated (drain or eviction) after the first new pod has migrated. The production compose's one-off migrate service has the same failure when an older image is deployed.

**Evidence.** Reproduced in a throwaway postgres:16-alpine container on an isolated network namespace (removed afterwards). The working-tree gateway migrated an empty database: 'nessuna revisione -> 0006'. Then `jumpasaservice/tabularia-gateway:1.1.0 python -m app.db.migra`, the chart's default image and the init container's exact command, failed with `alembic.util.exc.CommandError: Can't locate revision identified by '0006'`, exit=1. In the same image, `controlla_revisione()` returned '0006' and logged 'più nuova di quella di questo gateway ...: va bene durante un rolling update'. gateway/tests/test_migrazioni.py:111-118 tests the newer-revision case only for controlla_revisione, not for migra().

**Fix.** In migra() (or in the 'migra' action at migra.py:174), read the current revision first. If it is not None and not among `_script().walk_revisions()`, log that the database is newer than this code and return without upgrade or schema compare (exit 0). Add a test that sets alembic_version to '9999' and calls migra(). State in README › Upgrades that rolling back the application without the database is supported.

*Verification:* confirmed (media); confirmed (media).

#### `infra-perf-6` — Helm: with the shipped gateway HPA, Postgres connections can reach 208-234 against the 100 the chart documents; the per-pod figure is 26, not 25

Gravità **bassa** (proposta: bassa), sforzo S. Corsia `infra-perf`.

Dove: `infrastructure/helm/tabularia/values.yaml:395-405`, `infrastructure/helm/tabularia/values.yaml:427-431`, `infrastructure/helm/tabularia/templates/hpa.yaml:30-55`, `infrastructure/helm/tabularia/templates/gateway.yaml:122-126`, `gateway/app/db/session.py:62`, `gateway/app/db/session.py:72-74`, `gateway/app/db/session.py:81`

**Description.** Each gateway or orchestrator process can open pool_size (= threads, 12) + max_overflow (10) + the autocommit read pool max(2, 12/4) = 3 + the heartbeat engine 1, which is 26 connections. The chart and both guides say 25 and tell users to multiply by 'gateway replicas'. With autoscaling enabled, the HPA targets 70% of a 250m CPU request, which is 0.175 core, or about 16 req/s per pod by the guide's figure of 91 req/s per core. Moderate load therefore drives it to maxReplicas 6. The rolling updates add maxSurge 1 for both the gateway and the orchestrator.

**Impact.** The real worst case is 6 gateways + 1 orchestrator + 2 surge pods = 9 x 26 = 234 connections (208 without the surge). That is far above the 'max_connections (100 by default)' the values file warns about, and above the 50-100 of small managed Postgres tiers. When the ceiling is hit, new requests get 'too many clients' errors (500s), during exactly the bursts the HPA reacts to.

**Evidence.** session.py:62 (pool_size=threads, max_overflow=10), session.py:72-74 (read pool max(2, threads//4), max_overflow=0), session.py:81 (heartbeat pool 1). values.yaml:401-404 (maxReplicas 6, target 70, request cpu 250m). gateway.yaml:122-126 (maxSurge 1). values.yaml:427-431 and README.md:139-142 state 25 and do not mention the HPA maximum. Live prodtest: 30 idle app connections from 5 processes, max_connections 300, so compose is fine.

**Fix.** Document the formula as (gateway maxReplicas + orchestrator replicas + 2) x 26. Set DB__MAX_OVERFLOW lower in the chart (for example 4). Raise the gateway CPU request to about 1 core so the HPA reacts to real saturation rather than 17% of a core. Recommend PgBouncer in transaction mode for managed databases with small limits.

*Verification:* confirmed (bassa); refuted (bassa); confirmed (bassa, tie-break).

#### `measure-6` — At rest, the sample ClickHouse and cAdvisor cost more CPU than three gateways serving 30 users; the sample ClickHouse holds 1.1 GB with no memory limit

Gravità **bassa** (proposta: bassa), sforzo S. Corsia `measure`.

Dove: `infrastructure/production/docker-compose.yml:578-593`, `infrastructure/production/docker-compose.yml:650-671`

**Description.** The production compose that prodtest runs (COMPOSE_PROFILES=minio,monitoring,samples) starts sampledb-clickhouse with no mem_limit and the default system log tables, and cAdvisor with default housekeeping. Both burn CPU continuously, whatever the app is doing.

**Impact.** On the 8-core / 15 GB host that ran out of memory yesterday, an idle sample database is the largest resident container: more than the 3 gateways and 2 orchestrators together (~640 MB). It is also unbounded. The monitoring and sample containers take about 0.35 core constantly, while the whole app serving 30 users took about 1.0 core.

**Evidence.** cgroup CPU counters sampled every 5 s. At 1 user the whole stack used 64% of a core, 35 points of it from sampledb-clickhouse (18.5%), cadvisor (12.2%) and victoriametrics (2.3%). At 30 users: gateways 9.8/10.2/10.0%, backend 8.1%, postgres 6.3%, preview-worker 40%, whole stack 139% of 800%. Memory: sampledb-clickhouse 1.09-1.20 GB (limit shown as the host's 15.36 GiB), cadvisor 166-190 MB, victoriametrics 215-250 MB.

**Fix.** Give sampledb-clickhouse a mem_limit and a config.d that turns off query_log, trace_log, metric_log, asynchronous_metric_log and part_log. Run cadvisor with --housekeeping_interval=15s --docker_only=true and a --disable_metrics list. Keep the samples profile out of real deployments.

*Verification:* confirmed (bassa); refuted (bassa); confirmed (bassa, tie-break).

#### `infra-perf-5` — storage-stats lists the whole bucket every 60 s on the run queue, with no expiry, so stale copies pile up in front of runs

Gravità **bassa** (proposta: bassa), sforzo S. Corsia `infra-perf`.

Dove: `backend/app/tasks/celery_app.py:90-109`, `backend/app/core/config.py:160`, `backend/app/tasks/jobs.py:460-483`, `backend/app/utils.py:214-226`

**Description.** Beat enqueues `storage_stats_task` every 60 s, plus evict-matviews every 300 s and evict-step-cache hourly. They go to the default 'celery' queue, which is served by the run worker with 2 slots. The task pages through ListObjectsV2 for raw/, datasets/, cache/ and out/, which costs one LIST call per 1000 objects per minute. The beat entries have no `expires` option, so when both run slots are busy with long runs, one stale copy per minute accumulates in front of the next queued run.

**Impact.** Small on prodtest (585 objects, 0.1 s per execution). On a cloud bucket the cost grows with the object count: at 100k objects that is about 100 LIST calls per minute (about 144k per day, billed by AWS and R2) and several seconds of a run slot per minute. After two one-hour runs, about 60 storage-stats, 12 evict-matviews and 1 evict-cache copies run back to back before the next scheduled run starts, all for a metric that is only a gauge.

**Evidence.** Run worker logs: 30 storage_stats_task executions in 30 minutes, each taking 0.10-0.14 s (585 objects on the local MinIO). celery_app.py:96-99 schedules it at settings.metrics.storage_stats_interval_seconds (default 60, config.py:160). No `expires` option exists anywhere in backend/app/tasks.

**Fix.** Add `"options": {"expires": interval - 5}` to each beat entry so stale copies are discarded. Move the maintenance tasks to the preview worker or a small 'maintenance' queue. Raise the default interval to 300-900 s, or count incrementally (on write and delete) instead of listing.

*Verification:* confirmed (bassa); confirmed (bassa).

#### `quality-architecture-7` — No automated test gate: CI publishes images without running the 1,505 tests, and the 24k-line frontend has no typecheck or tests

Gravità **bassa** (proposta: media), sforzo M. Corsia `quality-architecture`.

Dove: `.github/workflows/release-images.yml:120-127`, `frontend/package.json:5-10`, `gateway/tests/conftest.py:43-52`

**Description.** The only workflow is release-images.yml. On a tag it builds, smoke-tests and pushes multi-arch images; the only tests it runs are those of the secret scanner. The pre-push hook runs the push guard (secrets). The gateway suite (870 test functions) and the backend suite (635) run only when someone remembers to run them. The frontend (24,022 lines of .vue/.ts excluding locales) has no test runner, no lint and no typecheck dependency (no vue-tsc, eslint or vitest in node_modules/.bin), and has 117 `any` annotations; the flow node payload is `any`.

**Impact.** The kinds of defects found in this review are what a gate catches early: an editor/gateway contract drift (the S3 mirror), FK-violating deletes, missing i18n keys. With releases tagged by hand, the repository cannot show which version passed which suite.

**Evidence.** ls .github/workflows → release-images.yml only. Its 'Test the secret checks' step runs test_image_secret_scan.py and test_push_guard.py, nothing else. .git/hooks/pre-push execs .github/scripts/push_guard.py. grep -c 'def test_' gives 870 (gateway/tests) and 635 (backend/tests). No *.spec.ts or *.test.ts outside node_modules. package.json scripts: dev, build, preview, postinstall.

**Fix.** Add .github/workflows/ci.yml on push and pull_request with these jobs:
(1) gateway pytest, with FKs on once finding 3 is fixed;
(2) backend pytest on polars and duckdb, skipping the live Oracle/SQL Server/ClickHouse files;
(3) `python -m app.db.migra controlla` against a throwaway Postgres;
(4) frontend: `nuxt prepare`, then vue-tsc pinned as a devDependency and pointed at .nuxt/tsconfig.json (the command recorded in the project notes), plus vitest for composables/useFlowModel.ts using the golden fixtures from finding 1;
(5) the i18n key-parity script from the i18n finding.
Make the release job `needs:` CI on the tagged commit.

*Verification:* confirmed (bassa); confirmed (bassa).

#### `quality-architecture-4` — Run launching and reconciliation still live in routes/runs.py (1,257 lines), and services import from routes

Gravità **bassa** (proposta: media), sforzo M, già noto. Corsia `quality-architecture`.

Dove: `gateway/app/routes/runs.py:129-1062`, `gateway/app/routes/runs.py:137-494`, `gateway/app/routes/runs.py:758-915`, `gateway/app/routes/runs.py:390-400`, `gateway/app/services/orchestrator.py:62`, `gateway/app/services/scheduler.py:34`, `gateway/app/routes/datasources.py:28`, `gateway/app/services/notifier.py:32`

**Description.** Package B1 of the 2026-10-05 simplification report is still open and has grown. routes/runs.py went from 1,107 to 1,257 lines; about 930 of them are launch and reconciliation, not routes. _launch_flow_run is 358 lines and repeats the same 'get the connection → 404 → CONNECT → type check → 422' block four times (destination, S3, mirror, email). The email domain check in it (runs.py:390-400) duplicates notify_targets.ensure_deliverable with a different message.
Dependencies point the wrong way: orchestrator and scheduler import private names (_launch_flow_run, _reconcile) from routes.runs; datasources imports them from another route; notifier, contract_notifier and notify_targets import from routes.connections. Session-only token checks are a route helper (nega_se_da_token) imported inside 7 functions in 5 route files, so a new route has to remember to call it.

**Impact.** Readability, and the risk of changing it. The most important state machine in the gateway (claim, swap, publish, contract, webhooks) sits in a file of HTTP handlers, so every new background feature (flow groups being the latest) adds another inward import. The 78 imports inside functions mostly exist to dodge these import cycles, which makes the dependency order invisible.

**Evidence.** AST counts: _launch_flow_run is 358 lines and _reconcile 158. There are 78 function-level `from app.` imports in 27 gateway files; the most imported targets are app.services (18), app.models (12), app.services.permissions (9) and app.routes.api_tokens (7). Measured on the working tree: wc -l gateway/app/routes/runs.py = 1257. The 2026-10-05 report gave 1,107 and a 354-line _launch_flow_run.

**Fix.** Pure moves, no behaviour change.
- services/run_launcher.py: snapshot_key, resolve_run_engine, launch_ingest_run, and _launch_flow_run split into _resolve_publish/_resolve_destination/_resolve_mirror/_resolve_email. Each returns (payload, summary) and they share one `_connection_for(session, user, id, allowed_types, not_found_msg)` that keeps the 404→403→422 order. Add _post_to_engine, shared with launch_ingest_run.
- services/run_reconciler.py: _friendly_error, _email_not_sent, the contract helpers, close_if_queued_too_long, _reconcile, _finalize_ingest, _publish_datasource.
- services/connection_payload.py: engine_connection_payload and allowed_email_domains. The run path then uses notify_targets.ensure_deliverable.
- deps/tokens.py: a `session_only(table)` dependency replacing the 7 inline nega_se_da_token imports.
- routes/runs.py keeps the 6 handlers and run_out (about 250 lines).
Update the test imports. The existing 870 gateway tests are the safety net.

*Verification:* confirmed (bassa); refuted (bassa); confirmed (bassa, tie-break).

#### `quality-architecture-8` — The largest files keep growing, and the Explore and Datasources pages each carry their own copy of the import watcher

Gravità **bassa** (proposta: bassa), sforzo L, già noto. Corsia `quality-architecture`.

Dove: `frontend/components/FlowEditor.vue:253-560`, `frontend/components/FlowEditor.vue:1359-1755`, `frontend/components/ProjectBrowser.vue:224-563`, `frontend/components/ProjectBrowser.vue:340-377`, `frontend/pages/datasources.vue:134-215`, `frontend/components/AdminPanel.vue:409-810`, `frontend/pages/flow-group.vue:1-443`, `backend/app/engine/dbt_export.py:307-557`

**Description.** Since the 2026-10-05 report: FlowEditor.vue 1,628 → 1,919 lines (+18%), with one 1,755-line `<script setup>`; ProjectBrowser.vue 1,020 → 1,190; AdminPanel.vue 906 → 960 (4 sections extracted, users/groups/privacy/engines/AI/banners still inline); backend dbt_export.py is 1,629 lines with _compile_op grown from 162 to 250 lines (23 `if op_type ==` branches); gateway schemas/models.py is 700 lines; the new flow-group.vue is 874. There are 13 Python functions of 100+ lines. ProjectBrowser.vue:340-377 and datasources.vue:134-215 each implement the 'watch an import' poller, and they have drifted: only datasources has the `restored` guard, placeholder cleanup and screen-reader announcements. Both fetch the 50-row run history every 2.5 s to read runs[0]. Duration formatting is written four times (fmtDur, fmtDuration, fmtRuntime, duration).

**Impact.** These are the files every feature touches. Each new feature makes them harder to review, and copies like the import watcher fix a bug on one page and not the other.

**Evidence.** wc -l measurements above; AST function lengths (_launch_flow_run 358, _compile_op 250, transform_data_task 168, ai.chat 167, lineage.build_full_graph 160, _reconcile 158, build_dbt_project 149). Both pollers use `setTimeout(..., 2500)` on dsApi.listRuns(dsId) and keep `runs[0]`.

**Fix.** FlowEditor: move lines 1359-1755 into composables/useFlowRun.ts (it gets smaller once finding 1 lands); lines 253-560 into useFlowPersistence.ts (serialize/load/apply/undo snapshot/save/unsaved guard); lines 115-206 into useResizablePanes.ts. Leave the preview/columns block (1124-1320) as is, as the earlier report advised.
ProjectBrowser: extract FolderAdminDrawer.vue (grant/revoke/delete folder with its template), NewItemMenu.vue, and composables/useIngestWatch.ts, shared with datasources.vue and polling GET /runs/{id} (one row) instead of the 50-row history.
AdminPanel: move the remaining six inline sections into components/admin/*, as already done for four.
Backend dbt_export.py: split into dbt/compile.py, dbt/dialects.py (transpile, TargetClickHouse, ControlloSintassiClickHouse) and dbt/project.py (YAML writers and build_dbt_project), with _compile_op becoming a dict of per-operation functions.
schemas/models.py: one module per domain (runs, flows, catalog, admin).

*Verification:* confirmed (bassa); refuted (bassa); confirmed (bassa, tie-break).
<!-- {% endraw %} -->

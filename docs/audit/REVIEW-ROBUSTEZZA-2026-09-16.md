# Review di robustezza — le feature degli ultimi giorni — 2026-09-16

**Commit della review:** `64dad1d` · Working tree pulito. **Nessuna correzione applicata**: per decisione dell'utente questa passata produce solo il referto. Tutti i rilievi qui sotto sono **aperti**.

## Metodo e perimetro

Perimetro: `e37de86..HEAD`, le feature costruite negli ultimi giorni — materializzazione per il viewer, chiavi di ordinamento sulle datasource, tuning della lettura parquet da ClickHouse, nodo email + connessione SMTP, viste salvate.

Undici rilievi sollevati, **ognuno riverificato da me** aprendo il file prima di scriverlo qui. Uno è stato **smentito** e resta documentato come tale, perché la smentita è utile quanto il rilievo.

**Confidenza:** `ESEGUITO` = riprodotto o misurato da me; `CODICE` = certo dalla lettura; `PLAUSIBILE` = coerente col codice ma non riprodotto.

### Stato dell'ambiente

Stack locale sano, backend **531** test verdi, gateway **257**, typecheck frontend agli 11 errori di baseline. Il ClickHouse esterno (Scaleway) è stato **spento dall'utente a metà review**: da quel momento le verifiche che lo richiedevano sono state chiuse sulle misure già raccolte, non su nuove esecuzioni. Il fatto è segnalato dove pesa (R4).

## Riepilogo

| Area | ALTA | MEDIA | BASSA | Smentiti |
|---|---|---|---|---|
| Deployment / rolling update | 1 | — | — | — |
| Semantica utente (email, sort keys) | 2 | 2 | — | — |
| Correttezza / integrità | — | 4 | — | — |
| Prestazioni e igiene | — | — | 1 | 1 |

Dieci confermati, uno smentito.

**Tre rilievi mordono prima degli altri:** R1 rompe *ogni* run durante un aggiornamento progressivo, R2 fa proseguire un flusso dopo una notifica mai partita, R7 rende una datasource irreparabile per un refuso.

---

## ALTA

### R1 — [ALTA] Ogni run fallisce durante un rolling update — terza occorrenza della stessa classe

`backend/app/api/routes/tasks.py:40-48`

```python
task = transform_data_task.delay(
    bucket=..., input_key=..., operations=..., output_key=...,
    destination=request.destination,
    mirror=request.mirror,     # ← sempre inviato
    email=request.email,       # ← sempre inviato
    engine=request.engine,     # ← sempre inviato
)
```

Celery serializza i kwarg per nome. Un pod API **nuovo** che parla con un worker **vecchio** — la condizione normale di un aggiornamento progressivo, e quella di un rollback — consegna un kwarg che la firma del worker non ha, e il task muore con `TypeError` **prima di iniziare**. Non un run degradato: ogni run, per tutta la finestra del rollout.

È la **terza** volta che questa classe compare in questo repo. `mirror` è il rilievo D3 dell'audit del 13 settembre, tuttora aperto; `sort_keys` su `preview_task` è stato corretto ieri in `64dad1d` — ma la correzione ha toccato *solo* quella chiamata, lasciando intatta questa, che ne ha tre. `CODICE`

**Correzione:** costruire i kwarg condizionalmente (`if request.email is not None: kw["email"] = ...`), come già fatto per `sort_keys`. Il rimedio strutturale è una prova che fallisca quando un kwarg nuovo viene inviato incondizionatamente, altrimenti ci sarà una quarta occorrenza.

### R2 — [ALTA] `stopOnFailure` sull'email non ferma niente

`gateway/app/services/orchestrator.py:169` (il `raise`) e `:175` (l'`except` che lo mangia)

Il nodo email offre `stopOnFailure` e il commento sopra il codice ne spiega bene la ragione: «marcare "notificato" senza aver notificato è peggio che fermarsi». Ma il `raise OrchestrationError(...)` è **dentro** il `try`, e sei righe più sotto:

```python
except Exception as e:
    logger.warning("orchestrate: flusso %s, output non eseguito: %s", flow.id, e)
    errors.append(f"output: {e}")
```

L'eccezione viene catturata dallo stesso blocco, finisce nella lista degli errori, e **la sequenza prosegue**: gli output a valle e i sotto-flussi vengono eseguiti comunque. Il comportamento è l'esatto opposto di quello documentato e scelto dall'utente nel nodo.

Nessun test copre questo percorso. `CODICE`

**Correzione:** sollevare fuori dal `try` (o rilanciare in un `except OrchestrationError: raise` posto prima dell'`except Exception`), con un test che verifichi che i nodi a valle **non** vengano eseguiti.

### R3 — [ALTA] Una chiave di ordinamento con un refuso rende la datasource irreparabile

`gateway/app/schemas/models.py:475-480` (`DatasourceUpdate`), scrittura in `gateway/app/routes/datasources.py:214`

`sort_keys` si scrive **solo alla creazione**. `DatasourceUpdate` espone `name`, `description`, `project_id`, `column_descriptions` — non `sort_keys`. Nel dialogo di import il campo è **testo libero** (non una checklist: allo schema non si è ancora arrivati), quindi un refuso è la cosa più probabile che possa capitare.

Conseguenza: l'`ORDER BY` dell'ingest cita una colonna inesistente, **ogni** ingest e **ogni** refresh schedulato falliscono, e non c'è modo di correggere la chiave dall'interfaccia né dall'API. Si esce solo cancellando e ricreando la datasource — perdendo cronologia, permessi per-oggetto e ogni vista salvata che vi punta. `CODICE`

**Correzione:** aggiungere `sort_keys` a `DatasourceUpdate` e alla PATCH, con la stessa normalizzazione della creazione. In subordine, validare le chiavi contro lo schema al primo ingest riuscito.

---

## MEDIA

### R4 — [SMENTITO] Il tetto di 60 secondi sulla materializzazione

`backend/app/engine/clickhouse_engine.py:70`

Il rilievo sosteneva che `_MATERIALIZE_MAX_SECONDS = 60` sia sotto il tempo reale di copia («12M righe ≈ 2 minuti», dedotto dal log del commit), rendendo la copia impossibile da completare sopra la soglia dei 5M.

**La deduzione è sbagliata.** Le righe di log citate sono materializzazioni **distinte**, lanciate in momenti diversi; la distanza fra i loro istanti non è la durata di nessuna di esse. Misure dirette del 15 settembre, percorso completo API→Celery→worker, prima chiamata del viewer che *include* la creazione della copia: **2.991 ms** e **4.728 ms** per 12.000.001 righe. Il margine sul tetto è circa **20×**. `ESEGUITO`

La riverifica diretta programmata per oggi non è stata eseguita perché il ClickHouse esterno è stato spento nel frattempo; le misure sopra restano valide e sono state raccolte sullo stesso dataset e sullo stesso servizio.

### R5 — [MEDIA] Le sort keys non garantiscono l'ordine che promettono nel viewer

`backend/app/engine/clickhouse_engine.py:284-288`, consumo in `frontend/pages/viewer.vue:141,171,174`

Il viewer invia `sort_keys` a ogni preview e legge la copia con `SELECT * FROM {table}` **senza `ORDER BY`**. Una MergeTree è ordinata *dentro ciascuna parte*, non fra parti, e la lettura è multi-thread: l'ordine delle righe restituite non è garantito.

Effetto osservato il 15 settembre: le stesse `filtro + LIMIT 200` restituiscono **righe diverse** da `s3()` e dalla copia; con un `sort` esplicito nella catena coincidono esattamente. Il contenuto è identico (checksum `sipHash64` su tutte le colonne, 12.000.001 righe: uguale), quindi **non è un errore sui dati** — ma è la funzionalità che non mantiene la promessa: chi dichiara una chiave di ordinamento lo fa per vedere i dati in quell'ordine. `ESEGUITO`

**Correzione:** emettere un `ORDER BY` esplicito sulle sort keys quando si legge dalla copia e la catena non ne porta già uno.

### R6 — [MEDIA] L'export dbt emette un `ORDER BY` nudo sulla chiave che potrebbe non esistere

`backend/app/engine/dbt_export.py:118-122`

```python
if op_type == "sort":
    by = _as_list(_require(params, "by"))
    direction = "DESC" if params.get("descending") else "ASC"
    order = ", ".join(f"{q(c)} {direction}" for c in by)
```

Il compilatore dbt del `sort` **ignora `ignore_missing`**. Ieri il gateway ha iniziato a iniettare proprio in questo export un `sort` che porta quel flag (`gateway/app/services/dbt_export.py`), perché il modello dbt altrimenti produceva una tabella non ordinata mentre l'app la ordina. Ma il flag arriva a un compilatore che non lo legge.

Risultato: nel caso esatto per cui il flag è stato introdotto — una chiave dichiarata sull'Output e poi sparita dalla catena — l'app continua a funzionare e **il modello dbt si rompe in CI**, con un `ORDER BY "colonna_inesistente"`. Le due strade divergono proprio dove dovevano coincidere.

**Difetto introdotto da me il 15 settembre**: ho aggiunto l'iniezione nell'export senza verificare che il compilatore sapesse leggere il flag. `cols` è già in scope nella funzione, quindi il filtro è a portata di mano. `CODICE`

**Correzione:** filtrare `by` su `cols` quando `params.get("ignore_missing")` è vero, e omettere l'`ORDER BY` se non sopravvive nessuna chiave — la stessa semantica dei tre dialetti dell'engine.

### R7 — [MEDIA] `allowed_domains` non è validato: 500 a ogni lancio

`gateway/app/routes/connections.py:66-79` (`_valid_extra`), uso in `:99`

`_valid_extra` verifica soltanto che `extra` sia un **oggetto JSON**. Il commento dichiara l'intento giusto — «accettarne uno rotto sposterebbe l'errore da chi salva la connessione a chi, giorni dopo, si chiede perché l'email non parte» — ma la validazione si ferma al tipo del contenitore.

`{"allowed_domains": true}` viene accettato e salvato. Al primo lancio di un flusso che usa quella connessione, `smtp_options(conn).get("allowed_domains") or []` restituisce `True` e l'iterazione successiva solleva `TypeError`: 500, con l'errore che compare a chi lancia il flusso e non a chi ha salvato la connessione — esattamente lo scenario che il commento voleva evitare. `PLAUSIBILE` (certo alla lettura; non riprodotto end-to-end)

**Correzione:** validare la forma dei campi noti — `allowed_domains` lista di stringhe, `tls` booleano, `from_address` stringa — rifiutando con 422 al salvataggio.

### R8 — [MEDIA] Il salvataggio di una vista può sovrascrivere quella di un'altra datasource

`frontend/pages/viewer.vue:324-328`

```js
const esistente = savedViews.value.find(
  (v) => v.name === nome && v.project_id === saveProjectId.value,
)
```

Il confronto ignora `datasource_id`. Chi salva "Vendite 2024" nella cartella *Commerciale* mentre sta guardando la datasource **B**, e in quella cartella esiste già una "Vendite 2024" legata alla datasource **A**, manda una PATCH sulla vista di A con lo `spec` di B: la vista resta puntata su A ma con colonne, filtri e ordinamenti di B — cioè rotta, in silenzio, per chiunque la condivida.

L'intento (evitare un 409 incomprensibile) è giusto; il criterio di identità è incompleto. `CODICE`

**Correzione:** includere `datasource_id` nella ricerca; se il nome collide fra datasource diverse, proporre la sovrascrittura in modo esplicito invece di deciderla.

### R9 — [MEDIA] Un invio email fallito resta "in attesa" per sempre

`gateway/app/routes/runs.py:669-679`

L'esito dell'email viene scritto **solo** nel ramo `if new_status == "SUCCESS"`. Quando `stop_on_failure` rilancia, il run diventa terminale non-SUCCESS e `out["email"]` viene scartato: la colonna resta `null`, che la cronologia mostra come *in attesa*.

Un invio **definitivamente fallito** è quindi indistinguibile da uno mai tentato, proprio nel caso in cui l'utente ha più bisogno di saperlo. Il commento accanto descrive bene la ragione per cui l'esito va registrato dentro il claim atomico; manca il ramo simmetrico. `CODICE`

**Correzione:** registrare `values["email"]` anche nel ramo di fallimento.

### R10 — [MEDIA] Finestra fra scadenza e drop: la preview può fallire con "tabella inesistente"

`backend/app/engine/matview.py:151-158`

`evict` itera su `self._expired(cutoff)`, che è uno **snapshot** (`zrangebyscore` su `ATIME_ZSET`). Fra lo snapshot e il `matview_drop`, un `resolve` concorrente può superare `matview_exists`, fare `_touch` e restituire il nome della tabella: la query parte contro una tabella che l'eviction sta eliminando.

Il modulo dichiara in testa «best-effort: sempre giù su `s3()`, mai un errore all'utente». Qui l'utente vede un errore. La finestra è stretta e si apre solo quando il task di eviction gira (ogni 900 s), ma il dataset grande è proprio quello che impiega più tempo a essere letto. `CODICE`

**Correzione:** rileggere l'`atime` sotto controllo immediatamente prima del drop e saltare le voci risvegliate (o marcare la voce come "in eliminazione" e far ricadere `resolve` su `s3()`).

---

## BASSA

### R11 — [BASSA] Due strutture Redis crescono senza potatura

`backend/app/engine/matview.py:48-49`, `:240-242`, `:255-261`

`SMALL_SET` (id sotto soglia) e `FAILED_ZSET` (istante dell'ultimo fallimento) vengono ripuliti **solo** da `_forget`, che agisce sulle matview registrate. Un id finito in `SMALL_SET` non ha mai una matview, quindi non viene mai rimosso.

Ogni refresh di una datasource produce una **chiave nuova** — comportamento verificato: la stessa datasource è passata da `…193925Z-9edc1d4e` a `…203826Z-59e0995d` in poche ore — quindi un id nuovo a ogni refresh. Con refresh orari la crescita è di ~24 voci al giorno per datasource, per sempre. Non esplode, ma non si ferma.

In più, `_failed_recently` esegue `zrangebyscore(FAILED_ZSET, now - BACKOFF, "+inf")` — una **range query** il cui risultato viene poi cercato — su ogni preview, dove basterebbe uno `ZSCORE` a costo costante. `CODICE`

**Correzione:** `ZREMRANGEBYSCORE` sui fallimenti scaduti dentro `evict` (che già gira ogni 900 s), scadenza sugli id "piccoli", e `ZSCORE` al posto della range query.

---

## Verificato e risultato sano

Non tutto quanto ispezionato era difettoso. Queste parti sono state guardate espressamente e **reggono**:

- **`_all_known(strict=True)`**, lo sweep delle orfane che fallisce chiuso: un singolo errore di Valkey non fa più considerare orfane le tabelle vive.
- **`_reconcile`** e la costruzione atomica via `RENAME`: nessun residuo da un worker morto fra `CREATE` e `RENAME`.
- **Il probe dei core** che gestisce la forma `auto(N)` restituita da ClickHouse 24.8.
- **`allow_matview = not use_cache`**: l'editor non materializza più, il viewer sì. Confermato dal vivo — editor 289/331 ms senza creare copie, viewer 2.991 ms la prima volta e 245/204 ms dopo. `ESEGUITO`
- **`columnsSeq` in `FlowEditor.refreshForNode`**: la protezione contro la corsa fra risoluzioni è ora simmetrica a quella di `runPreview`.
- **La guardia del proxy sui quattro nomi di destinazione** e **l'iniezione di header SMTP** (la policy di Python solleva) e `send_message(to_addrs=…)`, che non ricava i destinatari dagli header.
- **La catena di pulizia beat → worker**: forzata la scadenza di una copia, il task schedulato l'ha eliminata (`removed: 1`). `ESEGUITO`

## Nota di processo

Nove dei dieci rilievi confermati stanno in codice scritto negli ultimi tre giorni, e due (R1 su `mirror`, e il compilatore dbt corretto ieri solo a metà) sono **riaperture di problemi già noti**: correzioni applicate a una chiamata e non alle sue sorelle. Vale più di ogni singolo rilievo: quando si corregge una classe di difetto, conviene cercarne tutte le occorrenze nello stesso giro, e lasciare una prova che fallisca se ne ricompare una.

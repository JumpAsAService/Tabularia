# Semplificazioni e pulizie: valutazione del 2026-10-05

> Fotografia del codice al 5 ottobre 2026 (ramo `main` più le modifiche non ancora
> committate del gateway replicabile). È una valutazione: **nessuna riga di codice è
> stata toccata**. Serve a decidere cosa semplificare, in che ordine e a che prezzo.

## In breve

Il progetto è cresciuto molto, ma non per copia-incolla né accumulando codice morto:
i blocchi identici sono l'1% del Python e lo 0,1% del frontend, e le funzioni senza
chiamanti si contano sulle dita. La complessità sta in quattro punti precisi:

1. **Tre motori SQL scritti tre volte.** DuckDB, chDB e BigQuery implementano le stesse
   29 operazioni in tre file con lo stesso scheletro; l'export dbt è una quarta copia
   che ha già perso una regola.
2. **Logica nel file sbagliato.** Nel gateway il lancio e la riconciliazione dei run
   (circa 900 righe) vivono in un file di rotte, e i servizi le importano da lì.
3. **Pochi file enormi.** `FlowEditor.vue` (1.628 righe), `routes/runs.py` (1.107),
   `ProjectBrowser.vue` (1.020), `AdminPanel.vue` (906), e una funzione da 354 righe.
4. **Gusci ripetuti nel frontend** (dialoghi, cicli di attesa, gestione degli errori) e
   **configurazione tenuta allineata a mano in cinque posti**.

Tutto quello che segue si può fare senza cambiare il comportamento, tranne le voci
raccolte in «Decisioni che spettano a te». La stima complessiva è di 9–12 giorni di
lavoro, divisibili in pacchetti indipendenti; la prima mezza giornata è a rischio zero.

## Come è stata fatta

- **Misure meccaniche** su tutto il codice: righe per area, file e funzioni più
  lunghi, blocchi duplicati (finestre di 8 righe uguali), definizioni senza chiamanti,
  direzione delle importazioni, inventario delle impostazioni.
- **Quattro letture indipendenti**, una per area (motore, gateway, frontend, contorno),
  con i criteri della skill `code-simplification`: duplicazioni, funzioni lunghe,
  codice morto, astrazioni inutili, confini dei moduli. Ogni lettura doveva applicare
  la «staccionata di Chesterton»: capire perché una cosa esiste prima di proporla da
  togliere.
- **Verifica.** I rilievi principali di ogni lettura sono stati ricontrollati nel
  repository; quelli segnati ✔ sono verificati, gli altri vengono dalla lettura e
  vanno riguardati prima di metterci mano. Un rilievo è stato scartato perché
  sbagliato (dava per inutilizzata l'impostazione che decide la modalità di produzione).

Limiti: non è stato eseguito nessun test e nessun refactoring di prova; le stime di
impegno sono a occhio; alcune funzioni lunghe (`lineage.build_full_graph`,
`sso.provision_and_sync`) non sono state lette per intero.

## I numeri

| Area | Righe | File |
|---|---|---|
| Frontend | 20.047 | 104 |
| Gateway (codice) | 13.206 | 88 |
| Motore (codice) | 12.488 | 61 |
| Test del gateway | 7.652 | 53 |
| Test del motore | 6.754 | 41 |
| Infrastruttura | 4.651 | 31 |

- Funzioni Python: 1.091; oltre le 80 righe 18, oltre le 150 righe 6.
- Blocchi identici di 8 righe: 1,0% del Python, 0,1% del frontend.
- Definizioni di modulo senza chiamanti nel codice applicativo: 2 (entrambe usate dai test).
- Impostazioni: 57 nel motore, 70 nel gateway; 124 chiavi in `.env.example`; 68 nel
  ConfigMap della chart.

## Cosa è già in ordine

Vale la pena dirlo, perché orienta dove **non** spendere tempo.

- **Poca duplicazione letterale e quasi niente codice morto**, in tutte le aree.
- **Frontend:** le chiamate API passano da un solo punto (una sola eccezione
  giustificata, lo streaming della chat); i tipi sono definiti una volta (un solo
  doppione); CSS globale e traduzioni non hanno voci morte (3 chiavi su circa 1.200);
  nessun componente inutilizzato su 43.
- **Chart Helm:** 247 valori, nessuno inutilizzato; i quattro worker Celery sono già
  generati da un solo modello.
- **Suite di correttezza fra motori:** è grande (47 KB) ma è l'oracolo che rende
  possibili le semplificazioni del punto 1, non una duplicazione.
- **`schemas/models.py`** del gateway (578 righe): nessuna classe morta.

---

## Pacchetto A — Pulizie a rischio zero (mezza giornata)

Nessuna cambia il comportamento. Si possono fare in un solo passaggio.

| Cosa | Dove | Evidenza |
|---|---|---|
| Sei metodi morti nel servizio di storage (circa 90 righe) | `backend/app/utils.py`: `upload_fileobj`, `download_fileobj`, `list_objects`, `delete_objects`, `get_object`, `get_presigned_url` | ✔ zero chiamanti nel codice; solo un finto nei test usa `download_fileobj` |
| Commenti che descrivono un livello TOML rimosso | `backend/app/core/config.py:9-11`, `:48`, `:395-406`; `.gitignore` | ✔ nessun lettore TOML né file `.toml`; chi scrivesse un `secrets.toml` verrebbe ignorato in silenzio |
| Importazioni dentro le funzioni che non nascondono nessun ciclo | `was_interrupted` importato in 5 funzioni del motore; `_qi` in `chdb_engine.py:95,102` | ✔ `query_tag.py` importa solo libreria standard |
| Tre nomi diversi per lo stesso modulo `time` | `backend/app/tasks/jobs.py` | dalla lettura |
| File vuoto tracciato per errore | `gateway/tests/test_observer.go_tmp` | ✔ 0 byte, nessun riferimento |
| `_get_flow` identica in due file | `gateway/app/routes/runs.py:49`, `routes/flows.py:182` | ✔ uguali byte per byte |
| Tre chiavi di traduzione e un'esportazione inutilizzate | `privacy.dismiss`, `adminPanel.lastEngineKept`, `datasources.descriptionsSavedToast`; `BANNER_LEVELS` | dalla lettura |
| Chiavi dei contatori di cache definite due volte | `engine/cache.py:47-48`, `observability/metrics.py:18-19` | dalla lettura |
| Tag delle immagini nella chart fermi a `0.1.0` | `values.yaml:28,32,38` contro la versione `1.0.0` ovunque | ✔ chi copia l'esempio costruisce immagini con il tag sbagliato |
| Una frase falsa nella guida Kubernetes | `docs/deploy/kubernetes.md:252-253` dice che disattivare un utente ha effetto alla scadenza del token | ✔ il gateway controlla `is_active` a ogni richiesta: l'effetto è immediato |

## Pacchetto B — Rimettere la logica al suo posto (1 giorno)

Spostamenti puri: il codice non cambia, cambia il file in cui sta.

### B1. Gateway: lancio e riconciliazione dei run fuori dalle rotte ✔

- **Dove:** `gateway/app/routes/runs.py` contiene `_launch_flow_run`,
  `launch_ingest_run`, `_reconcile`, `_publish_datasource`, `_finalize_ingest` e i
  loro aiutanti: circa 900 righe su 1.107.
- **Evidenza:** `services/orchestrator.py`, `services/scheduler.py` e
  `routes/datasources.py` importano da lì funzioni con il trattino basso;
  `services/notifier.py` importa da `routes/connections.py`. Nel gateway ci sono 22
  importazioni fatte dentro le funzioni, in 12 file, in buona parte per aggirare
  questi incroci.
- **Proposta:** `services/run_launcher.py` (lancio), `services/run_reconciler.py`
  (riconciliazione e pubblicazione), `services/connection_payload.py` (i costruttori
  dei payload di connessione). `routes/runs.py` resta con le sole rotte, circa 250 righe.
- **Rischio:** nessuno sul comportamento; vanno aggiornate le importazioni dei test.
- **Guadagno:** le dipendenze tornano a senso unico (rotte → servizi → modelli), e il
  file dove si nascondono i difetti dello scheduler smette di essere il più grosso.

### B2. Motore: i pezzi comuni fuori dai singoli motori ✔

- **Evidenza:** quattro motori importano `_coerce_ops` e `_columns_of` (private) da
  `polars_engine`; i tre costruttori SQL importano costanti da `operations.py`, che è
  il modulo di Polars; `clickhouse_engine` e `matview` importano `_qi` e `_lit` da
  `chdb_ops`; l'export dbt importa 10 nomi privati da `duckdb_ops`.
- **Proposta:** un modulo neutro (`engine/common.py`) con nomi pubblici per ciò che è
  di tutti. Nessun motore importa più il file di un altro motore.
- **Rischio:** nessuno. È anche il primo passo del pacchetto D.

## Pacchetto C — Spezzare le funzioni che fanno troppo (1–1,5 giorni)

| Funzione | Righe | Cosa fa insieme | Proposta |
|---|---|---|---|
| `_launch_flow_run` (gateway) | 354 | controllo della pubblicazione, destinazione S3 o database, copia su S3, email, chiamata al motore, creazione del run | quattro funzioni `_resolve_*` che tornano `(payload, riepilogo)`, più `_post_to_engine` condivisa con `launch_ingest_run` |
| `transform_data_task` (motore) | 151 | esecuzione, destinazione, copia, email | tre funzioni di consegna; resta un corpo di circa 35 righe |
| `_compile_op` (export dbt) | 162 | 17 rami `if op_type ==` | una funzione per operazione, come in tutti gli altri moduli (vedi D2) |
| `preview_task` (motore) | 103 | esecuzione e traduzione di quattro tipi di errore | estrarre la mappa errore → risposta |

Dentro `_launch_flow_run` lo stesso schema «prendi la connessione, 404, controlla il
permesso, controlla il tipo, 422» compare **quattro volte**. Rischio basso, con una
attenzione: l'ordine dei controlli (404 prima di 403, poi 422) è visibile a chi chiama
e va mantenuto identico.

**Una barriera di sicurezza scritta due volte** ✔: il controllo sui domini email
ammessi sta in `routes/runs.py:389` e in `routes/flows.py:587`, con messaggi diversi.
Va in una funzione sola: cambiandone una oggi è facile dimenticare l'altra.

## Pacchetto D — I motori SQL (2–3 giorni)

È la semplificazione che vale di più, e quella da fare con più cautela.

### D1. Un costruttore SQL comune con i dialetti come parametro ✔

- **Dove:** `duckdb_ops.py` (547 righe), `chdb_ops.py` (588), `bigquery_ops.py` (556).
- **Evidenza:** 29 operazioni hanno lo stesso nome nei tre file. chDB e BigQuery
  hanno il 56% delle righe identiche; chDB e DuckDB il 39%. Le differenze sono di
  dialetto: `EXCLUDE` contro `EXCEPT`, `<>` contro `!=`, come si citano i nomi.
  Esempio, la stessa operazione nei tre file:

  ```python
  # duckdb
  return rel.query(a, f"SELECT * FROM {a} LIMIT {int(_require(params, 'n'))}")
  # chdb e bigquery, identiche
  return f"SELECT * FROM {_sub(sql)} LIMIT {int(_require(params, 'n'))}"
  ```

- **Proposta:** un modulo `sql_common.py` con i costruttori che ricevono un piccolo
  oggetto `Dialect` (come citare, come scrivere «diverso», i modelli per
  contiene/inizia/finisce, la mappa delle aggregazioni). BigQuery passa già da
  `ctx.qi`: lo schema è mezzo pronto.
- **Cosa resta per motore, di proposito:** `cast`, `sample`, `unique`, `pivot`,
  `unpivot`, il corpo dei `join` e le liste nere di `compute` (diverse per motore e
  rilevanti per la sicurezza: si condivide la funzione, non la lista).
- **Non usare sqlglot per questo:** tradurrebbe l'SQL generato, cambiando il testo su
  cui ogni motore è messo a punto e provato. Un costruttore di stringhe con i
  parametri di dialetto lascia l'output identico al byte.
- **Rischio:** basso con la suite di correttezza fra motori come rete (824 test);
  ordine consigliato chDB e BigQuery prima, DuckDB dopo (usa un'API diversa e gli
  serve un adattatore).
- **Guadagno:** un operatore nuovo o una correzione si scrivono una volta, non tre o
  quattro.

### D2. L'export dbt è una quarta copia, e ha già perso una regola ✔

- **Dove:** `backend/app/engine/dbt_export.py:77-238`.
- **Evidenza:** riusa da DuckDB i predicati e i join, ma riscrive a mano 11
  operazioni. Il suo ordinamento emette `ORDER BY x` **senza** `NULLS LAST`, mentre i
  motori applicano la regola «i NULL sempre in fondo» (`duckdb_ops.py:198`).
- **Proposta:** dopo D1, `_compile_op` diventa un involucro di circa 60 righe sopra i
  costruttori comuni.
- **Attenzione:** correggere l'ordinamento cambia i modelli esportati: è una decisione
  (vedi sotto).

### D3. Quattro metodi di servizio ricopiati nei motori

`apply`, `build_right`, `_source_id` e `uid` sono quasi identici in chDB, ClickHouse,
BigQuery e DuckDB. Esiste già un mixin per la cache (`stepcache_defer.py`) che
dimostra il metodo: va allargato. Oggi una correzione ad `apply` si ricopia quattro
volte. Mezza giornata, rischio basso.

## Pacchetto E — Frontend (3–4 giorni)

In ordine di rapporto fra guadagno e rischio.

| # | Cosa | Evidenza | Impegno | Rischio |
|---|---|---|---|---|
| E1 | Un componente `Modal` per tutti i dialoghi | ✔ 10 file definiscono da soli sfondo e scheda; sei dialoghi ripetono le stesse 20 righe di CSS e lo stesso guscio | mezza giornata | basso |
| E2 | Aiutanti per date, durate e download | ✔ 10 funzioni di formattazione (due coppie identiche), 5 blocchi di download uguali | un'ora | nessuno |
| E3 | Un `usePoller` per i cicli di attesa | ✔ 7 cicli a mano (4 in `FlowEditor`, 2 in `ProjectBrowser`, 1 in `datasources`), ognuno col suo annullamento | mezza giornata | basso-medio: il comportamento in caso di errore è diverso da ciclo a ciclo e va preservato |
| E4 | Un `EngineMenu` e un solo catalogo dei motori | ✔ il tipo `EngineOpt` è definito due volte; il menu «nuovo flusso → scegli il motore» è in `flows.vue` e in `ProjectBrowser.vue` con stile e chiavi di traduzione già diversi | mezza giornata | basso |
| E5 | Un `useAction` per «conferma, chiama, avvisa, ricarica» | ✔ 40 blocchi `toast.error(errMessage(e))`; 12 gestori quasi identici in `AdminPanel` | un'ora, poi adozione graduale | basso |
| E6 | `AdminPanel.vue` in sei sezioni | 906 righe, sei blocchi `v-if` già indipendenti | mezza giornata | basso-medio |
| E7 | Il blocco di esecuzione fuori da `FlowEditor.vue` | circa 400 righe (esecuzione, orchestrazione, cicli) che toccano solo `busy`, stato e nodi: un `useFlowRun` | 1 giorno | medio |
| E8 | Lo stato del grafico in un solo oggetto | `ChartPanel.vue` tiene 10 `ref` sciolti; è ciò che impedisce di salvare i grafici nelle viste | mezza giornata | basso |

**Da lasciare per ultimo:** il blocco anteprime e colonne di `FlowEditor.vue`
(`columnsSeq`, `previewSeq`, 12 controlli di sequenza in una funzione). Protegge da
gare reali già misurate (anteprime accodate: 79 secondi contro 25) e va spostato
tale e quale, non riscritto.

## Pacchetto F — Configurazione, documentazione, test (1–1,5 giorni)

### F1. Un test che tenga allineate le impostazioni ✔

- **Evidenza:** ogni impostazione vive in fino a cinque posti (classe, `.env.example`,
  ConfigMap e valori della chart, tabella nella guida Kubernetes) e nessun controllo
  li confronta. Derive già presenti:
  - sei variabili del motore lette con `os.getenv` fuori dalle classi, cinque delle
    quali non documentate da nessuna parte (`CHDB_MAX_MEMORY_USAGE` e le altre tre
    `CHDB_*`, `ENGINE_MAX_CROSS_JOIN_ROWS`);
  - `APP__ROOT_PATH`, impostazione vera del gateway, assente da `.env.example` e dalla chart;
  - 42 impostazioni che dalla chart non si possono dare.
- **Proposta:** un test che scorre le classi e fallisce se un campo non è in
  `.env.example` né in una breve lista di eccezioni dichiarate; portare le sei
  `os.getenv` nelle classi; nella guida Kubernetes tenere solo le righe che parlano di
  Kubernetes e rimandare a `.env.example` per il resto.

### F2. Una sola pagina di «limiti noti»

Oggi sono raccontati in tre posti (`kubernetes.md`, README della chart, checklist di
rilascio) e due si contraddicono (vedi la frase falsa nel pacchetto A). Una pagina
sola, le altre rimandano.

### F3. I referti in `docs/audit/` sono fotografie, ma sono linkati come attuali ✔

Sei file, 1.451 righe, cinque in italiano, collegati da due documenti vivi in inglese
(`README` della chart e `engine-differences.md`). Chi li apre non sa cosa è stato
corretto dopo. Basta una riga in testa a ciascuno («fotografia del …») e, nei
documenti vivi, il rimando al singolo rilievo ancora valido invece che alla cartella.
Vale anche per questo referto.

### F4. Il compose di sviluppo ripete quattro volte il blocco del motore ✔

Stesso `build`, stessa immagine, stesso `env_file`, stessa riga della chiave Fernet
in `backend`, `worker`, `beat` e `preview-worker`. Un blocco comune con un'ancora
YAML; l'equivalenza si prova confrontando `docker compose config` prima e dopo.

### F5. Aiutanti di test ricopiati ✔

- Motore: la lista `ENGINES` e la fabbrica dei motori di prova sono in 4 file.
- Gateway: `_admin(session)` è definita uguale in 4 file, `_azioni(session)` in 3.

Vanno nei rispettivi `conftest.py`: aggiungere un sesto motore tocca un posto solo.

### F6. La versione di rilascio è scritta in sei posti

Due `pyproject.toml`, `package.json`, la configurazione del gateway, `Chart.yaml` e i
tag delle immagini (già sbagliati, vedi pacchetto A). Far derivare il tag della chart
da `appVersion` ne toglie uno; una riga nella checklist di rilascio elenca gli altri.

---

## Decisioni che spettano a te

Queste non sono pulizie: cambiano qualcosa che si vede, o sono scelte di prodotto.

1. **Explore e pagine piatte fanno le stesse cose due volte.** `/flows`,
   `/datasources` e `/connections` (976 righe in tutto) ripetono refresh, cronologia
   e dialoghi dell'Explore. O si estrae la logica comune, o si ritira una delle due
   superfici: la seconda toglie circa 900 righe ma cambia la navigazione.
2. **Operatori del filtro.** Polars accetta anche i simboli (`>`, `==`), i motori SQL
   solo le sigle (`gt`, `eq`); e senza operatore Polars dà errore mentre gli altri
   assumono «uguale». Un costruttore comune deve scegliere una regola per tutti.
3. **Ordinamento nell'export dbt.** Aggiungere `NULLS LAST` lo allinea ai motori ma
   cambia i modelli già esportati.
4. **Lingua di ciò che legge chi installa e chi usa.** Tre casi dello stesso tema:
   - i circa 132 messaggi d'errore del motore in italiano dentro un'interfaccia
     inglese (già in sospeso);
   - i messaggi di avvio in italiano (`SECURITY__FERNET_KEY non impostata`) mentre
     compose, chart e guide parlano inglese;
   - le date formattate sempre come `it-IT` in 16 punti del frontend, in
     un'interfaccia che ha cinque lingue.
5. **Permessi ricalcolati a ogni controllo.** Ogni verifica rifà circa 4 query e ce
   ne sono 61 nelle rotte: un lancio con tutte le destinazioni ne fa 25–30. Un
   contesto dei permessi per richiesta le taglierebbe senza cambiare le regole, ma è
   codice di sicurezza e va misurato prima e dopo. È anche la leva di prestazioni più
   economica rimasta sul gateway.
6. **Chiamate al database dentro funzioni asincrone.** 28 funzioni `async` fanno
   chiamate sincrone a Postgres e fermano il ciclo degli eventi mentre aspettano.
   Renderle sincrone è semplice ma cambia il comportamento sotto carico: va fatto con
   la prova di carico alla mano.
7. **Passare tutte le impostazioni dalla chart.** Un passaggio generico (`extraEnv`)
   renderebbe raggiungibili le 42 impostazioni oggi escluse.

## Cosa lasciare com'è

Sembra complicato, ma ogni voce risponde a un problema misurato o a un vincolo reale.

- **Le migrazioni scritte a mano in `db/session.py`.** Uno strumento come Alembic non
  toglierebbe la gestione del lucchetto e dell'impronta, necessarie con più repliche.
  Conviene solo se arrivano rinomine o cancellazioni di colonne.
- **`_reconcile` e la sua presa atomica.** Lungo, ma ogni ramo copre una gara vera.
  Si può dividere in funzioni; non si può semplificare la logica.
- **`a_fine_rotta`, la sessione asincrona, il ping solo alle connessioni ferme.**
  Nascono dal blocco a 60 richieste insieme.
- **`is_admin` ricalcolato e mai scritto sull'utente.** Scriverlo renderebbe
  permanente un privilegio dopo un commit.
- **Il caricamento pigro di chDB.** Evita uno stallo dei worker Celery.
- **`cast`, `pivot`, `unpivot`, `sample`, `unique` distinti per motore.** Sono davvero
  diversi; unificarli nasconderebbe la logica di dialetto.
- **`usePreviewSlots`, `useSystemMemory`, `useFlowPresence`.** Ognuno corregge un
  problema misurato; non vanno fusi in un ciclo di attesa generico.
- **`ParamForm.vue` e `NodePanel.vue`.** Grandi, ma guidati dai dati del catalogo
  delle operazioni: dividerli lo sparpaglierebbe.
- **I diagrammi HTML e le GIF in `docs/`.** Generati da sorgenti JSON e incorporati nel README.

## Ordine consigliato

| Passo | Pacchetto | Giorni | Perché in questo punto |
|---|---|---|---|
| 1 | A — pulizie a rischio zero | 0,5 | subito, non richiede decisioni |
| 2 | B — logica al suo posto | 1 | spostamenti puri; rende possibili C e D |
| 3 | F1, F3, F5 — test delle impostazioni, banner sui referti, aiutanti di test | 1 | ferma le derive prima di aggiungere altro |
| 4 | C — funzioni lunghe | 1–1,5 | dopo B, quando ogni funzione è nel suo modulo |
| 5 | E1, E2, E4, E5 — gusci del frontend | 1–1,5 | meccanico, guadagno visibile |
| 6 | D — motori SQL | 2–3 | il più prezioso; con la suite fra motori come rete |
| 7 | E3, E6, E7, E8 — cicli, AdminPanel, FlowEditor, grafico | 2–2,5 | per ultimo: tocca il codice più delicato |

Regole per quando si passa ai fatti, dalla stessa skill: una semplificazione alla
volta, test dopo ognuna, e mai nello stesso commit di una funzione nuova o di una
correzione. Se una semplificazione obbliga a modificare un test perché passi, quasi
certamente ha cambiato il comportamento.

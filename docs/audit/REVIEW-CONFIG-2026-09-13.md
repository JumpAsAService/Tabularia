# Review di configurazione, deployment e codice morto — 2026-09-13

**Commit:** `555e108` · Nessuna modifica applicata. Questo è il referto; le correzioni ai deployment vengono dopo.

## Metodo

Tre revisioni parallele su dimensioni indipendenti — variabili di configurazione, coerenza del deployment, codice e file inutilizzati — e poi **verifica personale di ogni rilievo consistente** prima di scriverlo qui. Due segnalazioni si sono rivelate più gravi di come erano state descritte, una si è rivelata un falso positivo.

Linea di partenza: gateway 157 test verdi, backend 452 verdi (96 saltati senza ClickHouse esterno), 14 container attivi, repository pulito.

**Confidenza:** `ESEGUITO` = l'ho riprodotto eseguendolo; `CODICE` = certo dalla lettura; `PLAUSIBILE` = coerente ma non riprodotto.

---

# Parte 1 — Deployment: cosa si rompe in produzione

## 1.1 [ALTA] `ENGINE__BUCKET` non esiste in nessuna superficie di configurazione — ESEGUITO

Il gateway ha un bucket **suo**, separato da quello del backend:

- `gateway/app/core/config.py:62` → `EngineSettings.bucket = "data-prep"`, col commento «deve combaciare con `STORAGE__BUCKET`»
- letto in 8 punti, fra cui `routes/proxy.py:118,140,146,148` — compreso `ensure_reads_pinned`, il controllo RBAC sui percorsi

`ENGINE__BUCKET` compare **zero volte** in `.env.example`, nel `docker-compose.yml` e nella Helm chart. Il risultato non è teorico: **è già rotto nel tuo ambiente di sviluppo adesso.**

| Servizio | Bucket effettivo |
|---|---|
| gateway | `data-prep` |
| frontend | `data-prep` |
| backend | `svc-tabularia-2` (da `.env`) |

E i dati si sono divisi di conseguenza:

| Bucket | Contenuto reale |
|---|---|
| `data-prep` | 8 oggetti, tutti `datasets/` |
| `svc-tabularia-2` | 11 oggetti: `cache/` 8, `datasets/` 2, `out/` 1 |

**La step-cache scrive nel bucket sbagliato rispetto ai dati.** Il meccanismo è in `backend/app/engine/cache.py:75` (`self.bucket = settings.storage.bucket`) e in `backend/app/tasks/jobs.py:300` per le statistiche: tutto ciò che non riceve un bucket esplicito dal gateway finisce nel bucket del backend, mentre i dataset finiscono in quello iniettato dal gateway. Eviction della cache e metriche di storage lavorano quindi su un bucket, i dati stanno nell'altro.

In Kubernetes sarebbe peggio: la chart mette `storage.bucket: tabularia` e il gateway resterebbe su `data-prep`. Per ogni utente **non** superuser `ensure_reads_pinned` risponderebbe `403 bucket non consentito`; per l'admin, che salta quel controllo, il gateway forzerebbe `data-prep` e l'engine leggerebbe da un bucket inesistente. Chi fa il deploy vedrebbe lo smoke test da admin "quasi" funzionare e tutti gli utenti reali bloccati.

## 1.2 [ALTA] L'ingress instrada `/scheduling`, il gateway serve `/schedule` — ESEGUITO

Mio errore nella chart.

- rotta reale: `gateway/app/routes/scheduling.py:48` → `@router.get("/schedule/load")`
- il frontend chiama `/schedule/load` (`composables/useApi.ts:96`)
- la chart elenca `- /scheduling` (`values.yaml:340`), prefisso che non instrada nulla

In Kubernetes `GET /schedule/load` non combacia con nessun percorso API, cade sulla regola `/` e finisce al frontend Nuxt, che risponde HTML dove il browser si aspetta JSON. La heatmap del carico degli schedule risulta rotta **solo in produzione**: in compose il browser parla direttamente col gateway e funziona.

Tutti gli altri 19 prefissi sono corretti. Il problema è che l'elenco è mantenuto a mano e va rivisto a ogni rotta nuova.

## 1.3 [ALTA] La chart ribalta `OIDC__AUTHORITATIVE` — ESEGUITO

Mio errore nella chart.

- codice: `gateway/app/core/config.py:99` → `authoritative: bool = True`
- `.env.example:124` → `true`
- chart: `values.yaml:151` → `false`, e il rendering conferma `OIDC__AUTHORITATIVE: "false"`

Chi passa da compose a Kubernetes cambia semantica senza accorgersene: con `false` l'IdP smette di revocare le appartenenze, e i gruppi tolti su Keycloak o Entra **restano** in Tabularia. È un cambio di postura di sicurezza silenzioso.

## 1.4 [ALTA] `REDIS__PASSWORD` è letta dal codice ma non esiste in nessuna superficie — ESEGUITO

- definita in `backend/app/core/config.py:26`, usata in `api/routes/healthcheck.py:32` e, via `redis.url`, in `observability/metrics.py:64` e `engine/cache.py:76`
- zero occorrenze in `.env.example`, compose e chart; `values.yaml` ha solo host, porta e database

Un Redis gestito (ElastiCache, Scaleway, Upstash) richiede quasi sempre autenticazione. L'operatore non ha modo di scoprirlo dalla chart, e i worker non si connettono.

## 1.5 [MEDIA] Opzioni ClickHouse perse nella chart — CODICE

`CLICKHOUSE_EXTERNAL__S3_NAMED_COLLECTION`, `__MAX_EXECUTION_TIME` e `__CONNECT_TIMEOUT` sono in `.env.example` e usate in `backend/app/engine/clickhouse_engine.py`, ma la chart non le rende. La *named collection* è la via consigliata per non far transitare le chiavi S3 dentro le query (e quindi nel `query_log` del server): senza, si ricade sulle credenziali inline. È perdita di postura di sicurezza, non solo di funzionalità.

## 1.6 [MEDIA] `APP__CORS_ORIGINS` dichiarata obbligatoria ma inesistente — ESEGUITO

`docs/deploy/kubernetes.md:188` la elenca come richiesta; compare **zero volte** in `.env.example`, compose e chart. Default nel codice: `localhost:3000`.

Con l'ingress di riferimento (un solo host per UI e API) le chiamate sono same-origin e non si rompe nulla. Ma `frontend.publicApiBase` è un valore libero: appena qualcuno separa `app.example.com` da `api.example.com` — topologia che la chart non vieta — ogni chiamata del browser viene bloccata dal preflight, e non esiste un valore per correggerlo senza modificare la ConfigMap.

## 1.7 [MEDIA] Il monitoraggio sparisce dalla chart senza essere dichiarato — ESEGUITO

Cinque servizi del compose (VictoriaMetrics, Grafana, cAdvisor, node-exporter, celery-exporter) non sono nella chart, e **il README della chart non li nomina affatto** — pur avendo una sezione esplicita su cosa non viene deployato. Due funzioni del prodotto ne dipendono:

- **badge RAM**: `MONITORING__NODE_EXPORTER_URL` punta a `http://node-exporter:9100/metrics`, che in Kubernetes non esiste → `/system/memory` risponde 503 stabilmente. Degrada in modo pulito (l'interfaccia nasconde il badge) ma nessuna variabile è esposta per ripuntarlo.
- **tab Monitoring**: con `publicGrafanaUrl` vuota, Nuxt ricade su `localhost:3001`. La voce resta **visibile** ai superuser e l'iframe punta al localhost di chi guarda: pagina bianca senza errore.

## 1.8 [MEDIA] Due promesse false nelle note di deploy — ESEGUITO

- **`APP__ENV_NAME`**: `docs/deploy/kubernetes.md:182` la marca valida per gateway **e** backend, dicendo che «abilita il guard di produzione». Sul backend `is_production()` e `is_development()` hanno **zero chiamanti**: la variabile è decorativa. Il guard esiste solo nel gateway (`main.py:41`).
- **`APP__TIMEZONE`**: il backend non ha nemmeno il campo. Celery usa `CELERY__TIMEZONE`, che non è documentata da nessuna parte. Impostare `APP__TIMEZONE=Europe/Rome` lascia beat su UTC. Oggi non rompe nulla perché il gateway converte i cron in UTC, ma la promessa «stesso fuso ovunque» è falsa.

## 1.9 [MEDIA] I veri tetti di memoria del motore non sono documentati — CODICE

Cinque variabili lette con `os.getenv` a livello di modulo (quindi valutate all'import, non modificabili a runtime), assenti da tutte e tre le superfici:

| Variabile | Dove | Default |
|---|---|---|
| `ENGINE_MAX_CROSS_JOIN_ROWS` | `engine/context.py:27` | 50.000.000 |
| `CHDB_MAX_MEMORY_USAGE` | `engine/chdb_engine.py:43` | ~1,5 GB |
| `CHDB_MAX_BYTES_BEFORE_EXTERNAL_GROUP_BY` | `chdb_engine.py:44` | 512 MB |
| `CHDB_MAX_BYTES_BEFORE_EXTERNAL_SORT` | `chdb_engine.py:45` | 512 MB |
| `CHDB_MAX_BYTES_IN_JOIN` | `chdb_engine.py:46` | 512 MB |

Un pod worker con `limits.memory: 8Gi` userà comunque 1,5 GB per chDB, e nulla dice come alzarlo. Il tetto di chDB è inoltre **scollegato** da `CELERY__MAX_MEMORY_*`, che si auto-derivano dal cgroup: due sistemi di limiti che non si parlano.

## 1.10 [BASSA] Rumore e falsi allarmi

- **`APP__DEBUG`**: distribuita a tutti i pod dalla ConfigMap, in `.env.example` e nel compose. Il gateway non ha nemmeno il campo; sul backend ha **zero lettori**. Chi la mette a `true` crede di aver acceso qualcosa.
- **`SCHEDULING__WORKER_CAPACITY`**: default 2, mai esposta. Con i valori di produzione d'esempio la capacità reale è 6, ma la heatmap segnala «fascia critica» da 3 schedule in poi. Falsi allarmi permanenti — su una pagina che oggi, per via del punto 1.2, non è nemmeno raggiungibile.
- **Divergenze chart ↔ codice** su valori non critici: `orchestrator.refreshWaitSeconds` 1800 contro 600, `pollIntervalSeconds` 5 contro 3, `storage.region` `fr-par` contro `us-east-1`.
- **Manopole del compose non documentate**: `BACKEND_MEM_LIMIT`, `WORKER_MEM_LIMIT`, `PREVIEW_WORKER_MEM_LIMIT`, `PREVIEW_WORKER_CONCURRENCY` non sono in `.env.example`.

---

# Parte 2 — Configurazione morta

## 2.1 [MEDIA] Il layer TOML del backend non viene mai letto — ESEGUITO

Verificato dentro il container:

```
config.py sta in     : /app/app/core/config.py
directory cercata    : /app/config      → NON esiste
directory reale      : /app/app/config  → esiste
```

`backend/app/core/config.py:294` calcola `parent.parent.parent / "config"`: un livello di troppo. `TomlConfigSettingsSource` su file mancante ritorna `{}` senza errore, quindi **`config.toml` e `secrets.toml` non vengono mai letti**, in silenzio. Le priorità documentate nel file stesso non esistono, e il `field_validator` scritto apposta per la coercizione dei valori TOML è morto con loro.

**Il punto che mi preoccupa**: esiste un file chiamato `secrets.toml` che chiunque crederebbe attivo. È correttamente escluso da git (in repository c'è solo `secrets.toml.example`), ma sul disco contiene credenziali che **non hanno alcun effetto**. È una trappola: qualcuno ci metterà una password e crederà di averla configurata.

## 2.2 [BASSA] Impostazioni senza lettori

| Impostazione | Dove | Nota |
|---|---|---|
| `EngineSettings.timeout_seconds` | gateway `config.py:59` | già rimossa dalla chart, resta nel codice |
| `AppSettings.debug` | backend `config.py:227` | vedi 1.10 |
| `AppSettings.name` / `.version` | backend `:225-226` | `api/main.py` li scrive a mano |
| `AppSettings.api_v1_prefix` | backend `:228` | i router sono inclusi senza prefisso: `/api/v1` è una promessa falsa |
| `Settings.is_production()` / `is_development()` / `project_root` | backend `:314-322` | helper morti |

## 2.3 Lette dal codice ma assenti da `.env.example`

Oltre a quelle già citate: `ENGINE__RUN_STALE_TIMEOUT_SECONDS`, `PREVIEW_TIMEOUT_SECONDS`, `DB__URL` (unica via per `sslmode` su un Postgres gestito), `JWT__ALGORITHM`, `METRICS__STORAGE_STATS_INTERVAL_SECONDS`, le cinque `CELERY__MAX_MEMORY_*`, `CELERY__BROKER_URL` e `CELERY__RESULT_BACKEND` (permettono un broker diverso da Redis), `OIDC__DISCOVERY_TTL_SECONDS`, `OIDC__TIMEOUT_SECONDS`, `OIDC__LOGIN_TX_TTL_SECONDS`.

Nessuna rompe nulla — hanno default sensati — ma chi costruisce ConfigMap e Secret partendo da `.env.example`, che è il percorso suggerito dalle note stesse, non sa che esistono.

---

# Parte 3 — Codice e file morti

## 3.1 Backend

| Cosa | Dove | Confidenza |
|---|---|---|
| `api/schemas.py`: mai importato **e non importabile** (`NameError: BaseModel` — importa solo `Field`, `Any`, `Optional` ma usa `BaseModel`). Doppione stantìo di `api/models.py` | `backend/app/api/schemas.py` | ESEGUITO |
| `core/secrets.py`: file da **0 byte**, mai importato | `backend/app/core/secrets.py` | ESEGUITO |
| Tre package **vuoti**: contengono solo `__init__.py`. Da non confondere con gli omonimi del gateway, che sono reali | `backend/app/{models,schemas,services}/` | ESEGUITO |
| Catena `test_task` irraggiungibile: task + `POST /tasks/test` + `TestTaskRequest`. L'engine non è esposto e il proxy del gateway non inoltra quel percorso | `tasks/jobs.py:13-40`, `api/routes/tasks.py:39-53` | CODICE |
| Catena `process_file_task` irraggiungibile, stessa prova | `tasks/jobs.py:42-81`, `api/routes/tasks.py:55-73` | CODICE |
| Import inutilizzati | `routes/healthcheck.py:1`, `routes/tasks.py:5,8,9` | CODICE |

**Nota anti-regressione su `process_file_task`**: `frontend/pages/queue.vue:68` mappa la *stringa* `'app.tasks.jobs.process_file_task'` a un'etichetta del pannello coda. È una mappa di visualizzazione, non un accodamento: rimuovendo il task va tolta anche quella riga e la chiave i18n `queue.taskProcessFile`.

**Da non toccare**: `transform_data_task` è vivo, e le operazioni dell'engine registrate con `@register("nome")` sono risolte a runtime per nome — sembrano non referenziate ma non lo sono.

## 3.2 Gateway

| Cosa | Dove | Confidenza |
|---|---|---|
| Rotta `/health` **definita due volte**: la seconda non viene mai servita, FastAPI risolve sulla prima | `main.py:82` e `main.py:110` | ESEGUITO |
| Import inutilizzati | `deps/permissions.py:2,5,6` | CODICE |

Il resto del gateway è pulito: 17 router su 17 registrati, nessun modulo orfano, nessun simbolo morto fra i 20 candidati esaminati.

## 3.3 Frontend

**Buona notizia, ed è la più importante**: **nessuna chiave di traduzione usata ma mancante** nel catalogo inglese. 918 chiavi, nessun buco: l'interfaccia non è rotta in nessuna lingua. Il candidato segnalato (`banners.length`) era un falso positivo — è `.length` di un array JavaScript.

Tutti i 31 componenti, i 21 composables e le 14 pagine sono usati e raggiungibili.

| Cosa | Dettaglio | Confidenza |
|---|---|---|
| 7 chiavi i18n orfane | `adminPanel.groupPlaceholder`, `datasources.colFolder/colName/colRows/colUpdated`, `flows.engineLabel`, `params.join_on` | CODICE |
| Divario di traduzione | `fr`, `de`, `es` mancano gli stessi 40 tasti che `en` e `it` hanno (blocco banner, ricerca e colonne dell'admin). Per design ricadono sull'inglese: non è rotto, ma in quelle lingue il pannello appare in inglese | CODICE |

**Da non rimuovere**: le chiavi `queue.task*` e `banners.level*` sembrano orfane ma sono raggiunte da costruzione dinamica (`` t(`queue.${key}`) `` e `` t(`banners.level${…}`) ``).

## 3.4 File di contorno

| Cosa | Prova | Confidenza |
|---|---|---|
| `frontend/image.png` — 512 KB **tracciati in git**, md5 identico a `public/logo.png`, nessun riferimento. Qualcuno l'aveva già capito: è elencato in `frontend/.dockerignore` | `addcf222…` per entrambi | ESEGUITO |
| `favicon-180.png` — md5 **identico** ad `apple-touch-icon.png`, mai referenziato | `c1c93168…` per entrambi | ESEGUITO |
| `favicon.png`, `favicon-512.png`, `favicon-48.png` — mai referenziate (`nuxt.config.ts` usa solo 32, 16, `.ico` e apple-touch) | ricerca sull'intero repo | CODICE |

**Da verificare prima di toccare**: `docs/architecture/runtime-architecture.html` (829 KB) non è referenziato da percorsi locali, ma se GitHub Pages serve la cartella `/docs` **quello è l'artefatto pubblicato** e cancellarlo romperebbe il link del README. Va deciso guardando l'impostazione di Pages, non il codice.

---

# Parte 4 — Ordine suggerito

Le prime quattro voci si correggono in pochi minuti e sono quelle che rompono davvero qualcosa:

1. **`ENGINE__BUCKET`** — chart, `.env.example` e compose. È l'unica che è già rotta *oggi*, non solo in produzione. Da decidere anche cosa fare dei blob già finiti in `svc-tabularia-2`.
2. **`/scheduling` → `/schedule`** nell'ingress.
3. **`OIDC__AUTHORITATIVE`** riallineato a `true`.
4. **`REDIS__PASSWORD`** esposta da chart e `.env.example`.
5. Named collection ClickHouse, `APP__CORS_ORIGINS`, monitoraggio dichiarato nel README della chart.
6. Correzione delle due promesse false nelle note di deploy.
7. Pulizia del morto: file, import, chiavi i18n, duplicati.
8. Layer TOML: ripararlo o rimuoverlo. La scelta va fatta, perché `secrets.toml` che non fa nulla è una trappola.

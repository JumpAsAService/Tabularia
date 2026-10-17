<!-- {% raw %} GitHub Pages: no Liquid in this page -->
# Pronti per la produzione? Regressione e sicurezza — 2026-10-08

**Verdetto: non ancora.** La regressione è verde ovunque e l'aggiornamento da `v1.0.0` è sicuro, ma ci sono **quattro rilievi ALTA**:

- **Due nell'export dbt del commit `80a5b0d`,** che non è stato pushato. Vanno corretti prima del push.
- **Due nella chart Helm,** e bloccano chi installa su Kubernetes.

Il percorso con il compose di produzione su una sola macchina regge meglio. Per quello, prima di andare in produzione, bastano le correzioni dell'export dbt e di OpenLineage e l'aggiornamento delle dipendenze.

**Perimetro:** i 22 commit da `899bbef` (ultimo audit, 2026-09-30) a `80a5b0d`: data contracts, OpenLineage, export dbt, gateway in più copie con il ruolo orchestratore, connessioni a Postgres per richiesta, prefisso di pubblicazione e cancello del monitoraggio, annulla/ripeti nell'editor. In più: la chart Helm, il compose di produzione (cartella locale `infrastructure/production/`), le immagini, le dipendenze, l'aggiornamento dello schema, e i rilievi lasciati aperti dagli audit precedenti.

**Stato dell'albero:** `main` a `80a5b0d`, 6 commit avanti rispetto a `origin/main`. Restano fuori dai commit solo i rimandi locali al compose di produzione.

**Metodo:** tre revisioni del codice in parallelo, ciascuna su una corsia: funzionalità nuove sui dati, nucleo del gateway, distribuzione. Poi strumenti automatici: pip-audit, npm audit, Trivy sulle tre immagini, gitleaks su tutta la storia. Poi prove dal vivo mie sullo stack di scarto, che gira con il compose di produzione (3 gateway, 2 orchestratori, Caddy davanti). Infine la regressione completa. **Ogni rilievo ALTA e MEDIA dei revisori l'ho riletto nel codice o riprodotto io** prima di metterlo qui.

**Confidenza:** `ESEGUITO` = riprodotto da me sullo stack o nei container; `CODICE` = certo dalla lettura del codice; `PLAUSIBILE` = coerente col codice, non riprodotto.

**Nessun file del prodotto è stato modificato.** Sono cambiati solo gli strumenti di prova in `~/tabularia-carico/`. Cartelle, flussi, utenti e datasource creati durante le prove sono stati eliminati. Il database di prova per l'aggiornamento è stato tolto.

## Decisioni dell'utente (2026-10-10)

| Rilievo | Decisione |
|---|---|
| A1 — export dbt senza permessi | **Corretto restringendo l'export agli amministratori** (flag personale o gruppo admin; il team data lo riceve con un gruppo). «Il dbt è una cosa da team data o da admin, non da utente comune.» Un admin legge già tutto, quindi lo zip non gli dà niente in più. Verificato dal vivo: il lettore riceve 403 e il dialogo gli offre solo OpenLineage. |
| A2 — Jinja nel progetto dbt | **Accettato**: per farlo girare servono le proprie credenziali del database, quindi chi lo lancia ne risponde. |
| A3, A4 — Kubernetes | **Rimandati** al lavoro su Kubernetes. Il motore va chiuso dove non serve: nel compose lo è già. |
| M1 — OpenLineage | **Accettato così**: escono solo metadati. |
| M2 — «Esegui ora» | **Da sistemare.** |
| M6 — PyJWT e Vue | **Da aggiornare**, con attenzione alle regressioni. |

## Regressione

| Verifica | Esito |
|---|---|
| Suite del motore sui quattro motori (`chtest` ricreato) | 923 verdi, 67 saltati (BigQuery dal vivo) |
| Suite del gateway | 700 verdi |
| Typecheck del frontend | 0 errori |
| Giro dal vivo (96 rotte, RBAC positiva e negativa, run su 4 motori) | 152/152 |
| Contratti dal vivo / avvisi email / browser (5 stati × 4 viste) | 28/28 · 13/13 · 69/69 |
| Editor nel browser (annulla, ripeti, copia, incolla) | 39/39 |
| OpenLineage: prova veloce / giro completo / bordi (collector finto, irraggiungibile, schedulato) | 16/16 · 20/20 · 12/12 |
| Export dbt con dbt vero (seed, run, test e confronto con Tabularia), sviluppo e scarto | 41/41 |
| ODCS: 4 documenti + 1500 ostili riletti e validati sullo schema ufficiale v3.0.2 | tutti validi |
| Aggiornamento dello schema da `v1.0.0` (righe vecchie, doppio avvio, lettura di 26 tabelle, scrittura delle colonne nuove) | nessun errore |

Due strumenti avevano verifiche sbagliate e li ho corretti:

- **Il giro dal vivo.** «Secondo refresh → 409» era una corsa: il primo refresh finisce in 0,1 s.
- **Il giro OpenLineage.** Pretendeva `run` anche dai JobEvent statici.

Nessuno dei due casi era un difetto del prodotto.

## Riepilogo

| Area | ALTA | MEDIA | BASSA |
|---|---|---|---|
| Export dbt | 2 | 0 | 3 |
| OpenLineage | 0 | 1 | 0 |
| Gateway: coda, orchestratori, motore | 0 | 2 | 3 |
| Chart Helm | 2 | 4 | 3 |
| Frontend e bordo | 0 | 1 | 1 |
| Dipendenze | 0 | 1 | 0 |
| Data contracts | 0 | 0 | 3 |

## ALTA

### A1 — L'export dbt consegna dati e definizioni di cartelle che l'utente non può leggere · `ESEGUITO`

`GET /flows/{id}/export/dbt` controlla solo VIEW sulla cartella del flusso esportato (`gateway/app/routes/flows.py:266`). Il payload si costruisce senza conoscere l'utente: `build_export_payload` in `gateway/app/services/dbt_export.py`, che risolve ogni `datasourceId` senza alcun controllo. Il progetto può quindi contenere:

- **i dati** di una datasource rimasta senza origine (succede quando si cancella il flusso che l'ha pubblicata, `flows.py:510`) o di un file: fino a 50 000 righe in CSV come seed;
- **i modelli dei flussi a monte**, anche di cartelle private, con le loro espressioni, query e filtri;
- **il testo delle query** delle datasource da query;
- host, database e utente delle connessioni.

Un run normale degli stessi oggetti verrebbe fermato da `ensure_reads_pinned` e `ensure_can_read_keys` (`gateway/app/routes/runs.py:188-189`). L'export non li chiama.

**Riprodotto** (`~/tabularia-carico/sicurezza_export.py`):

- **Montaggio.** Un utente con VIEW sulla sola cartella «condivisa» esporta un flusso di quella cartella che legge due datasource di una cartella «privata».
- **Controllo.** Scaricare direttamente la datasource privata gli dà 403.
- **Esito.** L'export gli dà 20 righe di quei dati nel seed e il testo della query privata nel modello.

**Seconda strada, senza flussi altrui.** Con EDIT su una cartella qualsiasi si salva un flusso con `{"datasourceId": N}`, perché al salvataggio si controllano solo le chiavi di storage (`objects.py:76-93`). Poi lo si esporta.

**Correzione:** passare l'utente al costruttore del payload. Poi chiamare `ensure_reads_pinned` e `ensure_can_read_keys` su ogni chiave risolta (radici, rami destri, seed, flussi a monte) e chiedere VIEW sulla cartella di ogni flusso a monte e di ogni datasource di cui si emettono metadati. In più, al salvataggio di un flusso, chiedere VIEW sulla cartella di ogni `datasourceId`. Il difetto nasce con `80a5b0d`: va corretto **prima del push**.

### A2 — Il progetto dbt esportato esegue il Jinja scritto nel flusso · `ESEGUITO`

dbt rende come Jinja il SQL dei modelli, le `config()` e le descrizioni. Senza alcun filtro (`backend/app/engine/dbt_export.py`) ci finiscono:

- le espressioni dei compute;
- i valori dei filtri;
- le query dei nodi SQL;
- il nome della tabella di uscita, dentro `config(alias='…')`;
- le descrizioni.

**Riprodotto:** un compute `{{ env_var('TABULARIA_DB_1_PASSWORD') }}` e un'uscita chiamata `t') }}{{ log('iniettato') }}{{ config(a='` arrivano tali e quali nel progetto. Dal nome della tabella si esce dalla `config`.

**Chi paga.** Chi lancia `dbt run` (tipicamente la CI, con le credenziali del warehouse) esegue codice scritto da chiunque abbia EDIT sul flusso: legge i segreti con `env_var`, lancia SQL arbitrario con `run_query`. Così si aggira anche la lista bianca del nodo SQL.

**Correzione:** rifiutare `{{`, `{%`, `{#` nei testi dell'utente che entrano nel progetto, o racchiuderli in un blocco `raw` di Jinja. Validare i nomi di tabella e schema con un'espressione da identificatore. Nel README del progetto avvisare di rileggerlo prima di lanciarlo.

### A3 — Helm: l'ingress manda al gateway pagine del frontend; ricaricarle e l'accesso SSO si rompono · `CODICE`

`infrastructure/helm/tabularia/values.yaml:488-512` instrada alla radice del dominio, verso il gateway, i prefissi `/auth`, `/flows`, `/runs`, `/datasources`, `/connections`, `/audit`, `/lineage` e `/queue`. Sono anche pagine di Nuxt (`frontend/pages/*.vue`). Ricaricare o aprire da segnalibro una di quelle pagine restituisce il JSON del gateway. Il ritorno dall'SSO, `https://host/auth/callback`, finisce sul gateway, quindi **l'accesso SSO su Kubernetes non funziona**. Manca anche `/privacy-notice`: l'informativa nella pagina di login va nel 404 di Nuxt.

Il compose evita tutto questo con il prefisso `/api`.

**Correzione:** fare come il compose. Gateway sotto `/api` nell'ingress, `APP__ROOT_PATH=/api`, `frontend.publicApiBase=https://host/api`.

### A4 — Helm: l'API del motore non ha autenticazione e l'isolamento di rete è spento di default · `CODICE`

`backend/app/api/main.py` non autentica nulla: chi raggiunge il motore legge, esporta e cancella (`DELETE /files/object`) qualunque dataset con le credenziali del bucket. Nella chart `networkPolicy.enabled` vale `false` (`values.yaml:517-518`). Con i default, qualunque pod del cluster può farlo.

Il compose lo protegge con le reti: il motore sta solo su `interna` e non pubblica porte.

**Correzione:** `networkPolicy.enabled: true` di default, documentando che serve una CNI che le applichi. Poi un token condiviso fra gateway e motore come seconda barriera.

## MEDIA

### M1 — OpenLineage: metadati di cartelle private nell'export, e invio al collector con il solo VIEW · `ESEGUITO`

`GET /flows/{id}/openlineage` (`gateway/app/routes/lineage.py:56`) restituisce di ogni sorgente e uscita il percorso della cartella, il nome, le colonne, la descrizione, la chiave dello snapshot e l'host della connessione, senza filtrare le cartelle non leggibili. La pagina Lineage invece le maschera come «restricted» (`services/lineage.py:72-85`). **Riprodotto:** il lettore della sola cartella condivisa vede nomi e cartella degli oggetti privati.

`POST` manda gli stessi eventi al catalogo del cliente con VIEW. **Riprodotto:** accettato al lettore. Un flusso costruito apposta può anche dichiarare di scrivere dataset veri, sporcando il catalogo.

**Correzione:** mascherare gli oggetti fuori da `readable_project_ids` come fa la pagina Lineage, e chiedere EDIT per il POST.

### M2 — «Esegui ora» non rifiuta un flusso già in coda o in esecuzione: la coda si può intasare · `ESEGUITO` (l'intasamento è `PLAUSIBILE`)

`POST /flows/{id}/run-now` inserisce sempre una riga in coda (`gateway/app/routes/flows.py:522-553`). **Riprodotto:** dieci richieste di fila danno dieci 202, una sola esecuzione e nove righe chiuse come «flusso già in esecuzione». Non c'è doppia esecuzione, ma ogni richiesta costa una riga permanente in `runs` e un giro dell'orchestratore. La coda è letta in ordine d'arrivo (`orchestrator.py:347-353`): chi ha RUN su un solo flusso può spingere in avanti le esecuzioni di tutti, fino a farle chiudere dopo 900 s come «rimaste in coda troppo a lungo». Per quelle schedulate parte anche l'email di fallimento.

**Correzione:** 409 se il flusso ha già un'esecuzione in coda o in corso. In più, l'orchestratore salta i flussi già in corso invece di leggere strettamente in ordine.

### M3 — Una chiamata al motore che non torna tiene un'esecuzione «viva» per sempre · `CODICE`

Il client verso il motore ha `timeout=None` (`gateway/app/core/engine_client.py:19`), per gli upload grandi, e lo usano anche il lancio dei run e `_reconcile`. Il battito (`orchestrator.py:431-454`) rinfresca qualunque esecuzione il cui processo è vivo, che stia avanzando o no. Basta un motore inchiodato o una connessione TCP mezza aperta: l'esecuzione resta STARTED con un battito fresco, l'indice unico blocca quel flusso, le schedulazioni vengono saltate, fino al riavvio. Lo stesso vale per il tick dello scheduler.

**Correzione:** timeout sulle chiamate di controllo (es. 30 s, connessione 5 s) e `None` solo per upload ed export in streaming. In più, una scadenza assoluta oltre la quale il battito si ferma.

### M4 — Helm: chiunque può bloccare il login di un account, anche dell'admin · `CODICE`

La chart lancia il gateway con il comando dell'immagine (`gateway/Dockerfile:75`), senza `--forwarded-allow-ips`. Dietro l'ingress, tutti i client hanno l'indirizzo del controller, e il freno sui login, indicizzato per (indirizzo, email), diventa per sola email. Cinque password sbagliate per l'email dell'admin lo tengono fuori per 5 minuti, e il ciclo si ripete.

Nel compose il problema non c'è. **Verificato dal vivo:** 25 login sbagliati con un `X-Forwarded-For` falsificato diverso ogni volta fanno scattare il freno al sesto, perché Caddy sovrascrive l'intestazione, e la riga del freno porta l'indirizzo vero.

**Correzione:** `FORWARDED_ALLOW_IPS` con il CIDR dell'ingress, e `--timeout-keep-alive 75` come nel compose, che evita anche dei 502 sporadici.

### M5 — Helm: ogni pod riceve i segreti del gateway · `CODICE`

`_helpers.tpl:67-72` passa a tutti, worker compresi, `JWT__SECRET`, `DB__PASSWORD` e `AUTH__ADMIN_PASSWORD`. I worker eseguono SQL e connessioni degli utenti: chi esegue codice lì può firmarsi token da admin. Il compose li svuota per ruolo (`docker-compose.yml:95-101`).

**Correzione:** sovrascriverli vuoti in `env` per motore, worker e beat.

### M6 — Dipendenze con vulnerabilità note e correzione disponibile · strumenti

| Dove | Pacchetto | Da → a | Peso per Tabularia |
|---|---|---|---|
| gateway | PyJWT 2.13.0 (14 avvisi) | 2.15.0 | Il token interno usa un solo algoritmo HS256 e l'SSO solo algoritmi asimmetrici (`sso.py:51`), quindi le confusioni HMAC/asimmetrico non si applicano. Restano quelle di `PyJWKClient` (richieste illimitate per `kid`, redirect seguiti) quando l'SSO è acceso. |
| gateway, motore | anyio 4.14.x | 4.14.2 | Funzioni di processo e TLS su domini IDN che non usiamo, ma Trivy la dà critica. |
| gateway, motore | cryptography 49.0.0 | 50.0.0 | Decifrazione PKCS#7, che non usiamo. |
| motore | urllib3 2.7.0 | 2.8.0 | Proxy HTTPS e decompressione in streaming (boto3). |
| motore | pydantic-settings 2.14.1 | 2.14.2 | Link simbolici in `secrets_dir`, che non usiamo. |
| frontend | `vue` / `@vue/server-renderer` 3.5.39 | 3.5.42 | **Il frontend di produzione rende lato server** (`node .output/server`): l'XSS del renderer e i DoS di `devalue`/`seroval` girano davvero. |
| immagine frontend | `npm` dell'immagine base (`pacote`, `sigstore`, `ip-address`…) | — | In produzione `npm` non serve: si può togliere dall'immagine. |

Gli strati di sistema delle tre immagini non hanno vulnerabilità alte o critiche correggibili. Il resto di `npm audit` (devtools, simple-git, tar, postcss, svgo) sta solo nella build.

**Correzione:** `uv lock --upgrade-package` sui cinque pacchetti Python e aggiornamento di `nuxt`/`vue`, poi regressione.

### M7 — Token di sessione leggibile da JavaScript e non `Secure`; nessuna Content-Security-Policy · `CODICE` + `ESEGUITO`

Il cookie `tab_token` (`frontend/composables/useAuth.ts:17`) ha `sameSite: 'lax'` ma non è `Secure` né `HttpOnly`: un XSS qualsiasi si porta via il token. **Verificato:** le risposte di Caddy hanno HSTS, `nosniff`, `X-Frame-Options` e `Referrer-Policy`, ma nessuna `Content-Security-Policy`. L'ingress Helm non mette né CSP né `X-Frame-Options`.

**Correzione:** `secure: true` subito, una CSP in Caddy e nell'ingress. Per l'`HttpOnly` va spostata la lettura del token fuori dal JavaScript, quindi è un lavoro più grande.

### M8 — Helm: il freno d'esempio vale per tutto il sito; il CORS resta quello di sviluppo · `CODICE`

`values-production.example.yaml:123` mette `limit-rps: "5"` sull'intero ingress, pagine e asset compresi, per indirizzo. Un ufficio dietro un solo NAT prende dei 503. Con `corsOrigins` vuoto la chart lascia il default di sviluppo, cioè `localhost:3000` con credenziali (`configmap.yaml:37`, `config.py:230`). Il compose mette `[]`.

**Correzione:** il freno solo su `/auth/login`, e `APP__CORS_ORIGINS: "[]"` di default.

## BASSA

| # | Rilievo | Dove | Confidenza |
|---|---|---|---|
| B1 | `flows.notify_connection_id` ha la chiave esterna sulle installazioni nuove ma non su quelle aggiornate. Su una nuova, cancellare la connessione SMTP usata dagli avvisi di un flusso dà 500. | `models/flow.py:45`, `db/session.py:190`, `routes/connections.py:344` | CODICE |
| B2 | Durante un aggiornamento a versioni miste, un motore vecchio ignora il campo `contract` e il gateway nuovo pubblica senza contratto. I task `contract_jobs` finiscono a worker vecchi che non li conoscono. | `routes/runs.py:786` | CODICE |
| B3 | In produzione si accetta qualunque `JWT__SECRET` diverso dal default (la chart rende con `a`). Servirebbe un minimo di 32 caratteri. | `config.py:498-516` | CODICE |
| B4 | Helm: nessun PodDisruptionBudget, gateway a 1 replica di default, niente liveness su worker e beat, token del service account montato. | `values.yaml:310` | CODICE |
| B5 | Compose: il profilo `monitoring` è acceso di default e avvia un cAdvisor privilegiato con `/var/run` montato. Grafana è fermo a 11.2.0. | `production/.env.example:40`, `docker-compose.yml:526,556` | CODICE |
| B6 | Il primo aggiornamento dalla chart 1.0.x con RollingUpdate può chiudere come orfane, e rilanciare, esecuzioni che il pod vecchio sta ancora facendo. Il README chiede di scalare a zero prima, ma la chart non lo impone. | `db/session.py:199-207`, `gateway.yaml:34-38` | CODICE |
| B7 | Con due orchestratori il controllo di freschezza dei contratti può mandare l'avviso due volte: lo stato «cambiato» si decide su una lettura non condizionale. | `services/contracts.py:352`, `scheduler.py:243` | PLAUSIBILE |
| B8 | Il battito confronta gli orologi dei processi, non quello del database: con 50 s di sfasamento un'esecuzione viva viene chiusa. | `orchestrator.py:96-98,451,482` | CODICE |
| B9 | Gli avvisi dei contratti non ricontrollano i domini ammessi al momento dell'invio. | `contract_notifier.py:103-110` | CODICE |
| B10 | ODCS: il `name` di una regola `expression` non è validato; un oggetto con una chiave contenente un a-capo aggiunge chiavi YAML. Serve EDIT. | `contracts.py:38`, `odcs.py:255-257` | CODICE |
| B11 | Export dbt: host, database, utente, schema e tabella non sono tra virgolette in `profiles.yml`/`sources.yml`; nessun tetto in byte né sul numero di seed e modelli. | `backend/app/engine/dbt_export.py:408-486`, `api/routes/dbt.py:117` | CODICE |
| B12 | `/api/docs`, `/api/redoc` e `/api/openapi.json` sono pubblici. Il repo è pubblico, quindi non rivelano niente di nuovo, ma in produzione si possono spegnere. | — | ESEGUITO |

## Rilievi dei referti precedenti

| Rilievo | Stato |
|---|---|
| Spoofing di `X-Forwarded-For` sul freno ai login | **Chiuso nel compose** (verificato dal vivo). Su Helm è diventato M4. Audit e `last_seen_ip` leggono ancora il primo valore dell'intestazione: sicuro dietro Caddy, falsificabile dietro proxy che accodano. |
| Export CSV senza tetto (il browser accumula tutto in un Blob) | **Aperto.** `limit` è facoltativo e il proxy non ne aggiunge uno. |
| Password admin di default | **Invariato.** In produzione si rifiuta solo la stringa `admin` nella variabile. Un admin già seminato con `admin` resta tale, perché `seed_admin` non tocca chi esiste. |
| Politica dei motori non applicata a preview ed export | **Aperto.** `disabled_engines` vale solo per la scelta del motore di un flusso. |
| IP raccolti e mai cancellati | **Parziale.** Non si raccolgono più per gli osservatori, e la scadenza dell'audit esiste (`AUDIT__RETENTION_MINUTES`) ma vale 0 di default, non è impostata né nel compose né nella chart, e non tocca `users.last_seen_ip`. |

## Cosa regge

- **Recinto normale dei dati.** Un utente senza permesso che scarica una datasource riceve 403 (dal vivo).
- **Freno ai login nel compose.** Gli indirizzi falsificati sono ignorati e il freno è per coppia (indirizzo vero, email): un estraneo da un altro indirizzo non blocca la vittima (dal vivo).
- **Bordo del compose.** È pubblicato solo il proxy. Grafana risponde 401 senza login, e anche il cancello del monitoraggio senza token dà 401. Ci sono HSTS, `nosniff`, `X-Frame-Options` e `Referrer-Policy` (dal vivo).
- **Regola `expression` dei contratti.** Polars rifiuta funzioni di lettura file e sottoquery, anche `read_parquet` dentro una sottoquery, in tutti i contesti del valutatore (dal vivo). Una lista bianca con sqlglot resterebbe una seconda difesa utile.
- **Aggiornamento da `v1.0.0`.** Ogni colonna nuova ha il suo `ALTER` idempotente con default. Nessuna colonna `NOT NULL` è aggiunta senza default, nessuna è rinominata o tolta. Lucchetto consultivo e timeout sul lock (dal vivo).
- **Segreti.** gitleaks su 262 commit: niente. `infrastructure/gcp_keys.json` non è mai stato committato. Le password delle connessioni nei progetti dbt sono sempre `env_var`.
- **Immagini.** Tutte e tre girano con un utente non root (10001), sono multi-stage, senza `--reload` né dipendenze di sviluppo in produzione, e gli strati di sistema sono puliti. La chart ha root filesystem in sola lettura, capability tolte e seccomp.
- **Più orchestratori.** Un solo vincitore su ogni presa e su ogni chiusura (coda, finalizzazione, orfane, slot dello scheduler, avvisi dei contratti), quindi una sola email per chiusura. **Dal vivo:** dieci «Esegui ora» danno una sola esecuzione.
- **Identità per richiesta.** L'utente è riletto a ogni richiesta, senza cache fra richieste: attivo, admin e osservatore sono sempre freschi.
- **Data contracts.** VIEW per leggere, EDIT per scrivere, CONNECT e domini ammessi per scegliere i destinatari. Gli osservatori non scrivono.
- **Export dbt, il resto.** I nomi dei file nello zip passano tutti da `_slug`, quindi non c'è traversal. Il raddoppio degli apici nelle query federate è corretto.

## Ordine delle correzioni

**Prima del push di `80a5b0d`** (mezza giornata):

1. **A1:** permessi su tutto ciò che l'export risolve, più il controllo dei `datasourceId` al salvataggio di un flusso.
2. **A2:** niente Jinja dall'utente nel progetto; identificatori validati.
3. **M1:** OpenLineage mascherato fuori dalle cartelle leggibili, POST con EDIT.

Tutte e tre hanno già una prova dal vivo che oggi fallisce: `sicurezza_export.py` (7 su 12).

**Prima della produzione con il compose** (una giornata):

4. **M6:** dipendenze aggiornate, più regressione.
5. **M2 e M3:** 409 sul doppio «Esegui ora» e timeout sulle chiamate di controllo al motore.
6. **M7:** cookie `Secure` e una CSP in Caddy.
7. **B3:** segreto JWT di almeno 32 caratteri.
8. **Aperti dai referti precedenti:** tetto sul CSV, e scadenza dell'audit impostata nel compose.

**Prima della produzione su Kubernetes** (una giornata in più, e una prova su un cluster vero, mai fatta finora):

9. **A3:** gateway sotto `/api` nell'ingress.
10. **A4:** NetworkPolicy accesa di default, e un token fra gateway e motore.
11. **M4, M5, M8:** indirizzo vero del client, segreti per ruolo, freno solo sul login, CORS chiuso.
12. **B4, B6:** PodDisruptionBudget, repliche, aggiornamento dalla 1.0.x guidato dalla chart.

Il resto delle BASSA si può fare dopo.
<!-- {% endraw %} -->

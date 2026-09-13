# Audit del frontend — 2026-09-13

**Commit:** `a072c17` · Nessuna modifica applicata. Questo è il referto; le correzioni vengono dopo.

## Metodo e limiti

Audit tecnico condotto con la skill **impeccable** (comando `audit`, modalità **Operate**: la UI serve il compito — editor, tabelle dense, pannelli amministrativi — quindi scansionabilità e coerenza contano più dell'espressione). Cinque dimensioni, punteggio 0-4 ciascuna.

Perimetro: `frontend/` — 45 file `.vue` (10.933 righe), 2 fogli di stile, 20 composable.

**Limite dichiarato:** su questa macchina non c'è Chrome, quindi la scansione del sito in esecuzione (`detect http://localhost:3000`) e ogni misura da browser reale **non sono state eseguite**. Tutti i rilievi qui sotto sono a livello di codice o calcolati; nessuno proviene dall'osservazione della pagina renderizzata. I contrasti sono calcolati con la formula WCAG sui token dichiarati, non campionati a schermo.

**Confidenza:** `ESEGUITO` = misurato o calcolato da me; `CODICE` = certo dalla lettura; `PLAUSIBILE` = coerente col codice ma non riprodotto.

**Due sospetti si sono rivelati falsi positivi** e non sono stati riportati come difetti:

- `v-for` senza `:key` — una prima ricerca ne segnalava 8. La scansione multi-riga mostra **80 `v-for` e 80 `:key`**, nessuno scoperto: la chiave sta sulla riga successiva. `ESEGUITO`
- Grafici che non seguono il tema — i colori esadecimali nei grafici sembravano deriva dai token. In realtà `readUi()` legge le variabili CSS vive con `getComputedStyle` e gli hex sono solo il fallback per il render lato server, con `watch(theme, …)` a rinfrescare. Vale per 3 componenti su 5; l'eccezione reale è al rilievo F8. `ESEGUITO`

## Punteggio

| # | Dimensione | Voto | Rilievo principale |
|---|---|---|---|
| 1 | Accessibilità | 2/4 | I dialoghi non si annunciano e non gestiscono il focus |
| 2 | Performance | 3/4 | Nessun problema sostanziale da riparare |
| 3 | Tema | 3/4 | Due grafici su cinque ignorano il tema |
| 4 | Responsive | 1/4 | Due breakpoint in tutta l'applicazione |
| 5 | Integrità implementativa | 4/4 | Sistema coerente; i 5 rilievi del detector sono falsi positivi in contesto |
| **Totale** | | **13/20** | **Accettabile — lavoro significativo necessario** |

## Verdetto di integrità implementativa — si parte da qui

**Esito: superato.** L'interfaccia esprime un sistema coerente e specifico del prodotto, non un assemblaggio generico. Prove: 470 usi di `var(--…)` su un sistema di token completo per quattro temi; vocabolario dei componenti uniforme tra le schermate; 153 elementi `<button>` reali contro 5 `div`/`span` cliccabili; densità e affordance tipiche di uno strumento di preparazione dati, non di un template.

Il detector incluso nella skill segnala **5 anti-pattern**. Li ho verificati uno per uno e **li considero tutti falsi positivi in questo contesto**:

- `overused-font` su Montserrat (×2, `assets/main.css:4` e `:145`) — la scelta è deliberata e documentata nel codice come alternativa libera a Gotham, con il woff2 auto-ospitato da 38K e nessuna CDN a runtime. È identità, non convergenza.
- `side-tab` su `.node` (`main.css:321`) — il bordo sinistro di 3px non è decorazione: codifica il tipo di nodo tramite `--node-accent` (le sorgenti usano `--accent-2`). È informazione.
- `side-tab` su `BannerBar.vue:50` — è la convenzione standard degli avvisi, coerente con l'uso.
- `side-tab` su `lineage.vue:613` — marcatore di legenda.

Nessuna azione richiesta su questa dimensione.

## Sintesi

- Punteggio: **13/20** (Accettabile)
- Rilievi: **0 P0 · 7 P1 · 4 P2 · 1 P3**
- I tre problemi che pesano di più:
  1. I nove componenti-dialogo non hanno semantica di dialogo né gestione del focus, e il tasto Esc è agganciato dove non può funzionare (F1).
  2. Cinquantasei bottoni di sola icona non hanno un nome accessibile (F2).
  3. L'applicazione è di fatto solo desktop: due breakpoint in tutto, dialoghi a larghezza fissa, bersagli tattili sotto il minimo (F6, F7).

## Rilievi per gravità

### F1 · [P1] I dialoghi non si annunciano e non trattengono il focus

- **Dove:** 9 componenti con overlay — `ConnectionDialog.vue`, `RunDialog.vue`, `DbDatasourceDialog.vue`, `ScheduleDialog.vue`, `Select.vue`, `ProjectBrowser.vue`, `flows.vue`, `AppShell.vue` (menù), `ToastHost.vue`
- **Categoria:** Accessibilità · **Confidenza:** `ESEGUITO` (conteggi) + `CODICE` (lettura di `ConnectionDialog`)
- **Prove:** in tutto il frontend `role="dialog"` compare 0 volte, `aria-modal` 0, `<dialog>` 0, `inert` 0, `autofocus` 0. Le uniche due chiamate a `.focus()` sono in `Select.vue:109` e `CommentNode.vue:22`, entrambe non legate all'apertura di un dialogo.
- **Difetto aggiuntivo verificato:** in `ConnectionDialog.vue:110` il gestore `@keydown.esc` è su un `div` privo di `tabindex`. Un div non focalizzabile non riceve eventi da tastiera: **il tasto Esc non chiude quel dialogo**, a meno che il fuoco sia già su un campo interno. `tabindex` compare 0 volte nel progetto.
- **Impatto:** chi naviga da tastiera o con screen reader non viene informato dell'apertura, continua a tabulare tra gli elementi della pagina sottostante e, alla chiusura, perde il punto in cui si trovava.
- **Standard:** WCAG 4.1.2 (Nome, ruolo, valore), 2.4.3 (Ordine del focus)
- **Correzione:** portare i dialoghi su `<dialog>` nativo con `showModal()` — ottiene focus trap, Esc e `inert` sullo sfondo senza codice — oppure aggiungere `role="dialog"`, `aria-modal="true"`, `aria-labelledby` sul titolo, focus iniziale e ritorno del focus all'elemento che ha aperto.
- **Comando:** `/impeccable harden`

### F2 · [P1] Cinquantasei bottoni di sola icona senza nome accessibile

- **Dove:** diffuso; concentrato in toolbar, tabelle-elenco e intestazioni dei dialoghi (es. la `.cd-x` di chiusura)
- **Categoria:** Accessibilità · **Confidenza:** `ESEGUITO`
- **Prove:** 56 bottoni contengono solo un'icona. Nel progetto `aria-label` compare **1 volta** (`BannerBar.vue:34`) e `aria-hidden` **1 volta** (`MemoryGauge.vue:29`), a fronte di **278** istanze di icona. Come surrogato sono usati 88 attributi `title=`.
- **Impatto:** lo screen reader annuncia "pulsante" e nient'altro. `title` non è un sostituto affidabile: non compare su dispositivi tattili e non è esposto in modo uniforme.
- **Standard:** WCAG 4.1.2
- **Correzione:** `aria-label` esplicita su ogni bottone di sola icona e `aria-hidden="true"` sulle icone decorative accanto a un testo già presente.
- **Comando:** `/impeccable harden`

### F3 · [P1] Testo secondario sotto AA nel tema Dracula

- **Dove:** token `--muted` in `assets/main.css:86`
- **Categoria:** Accessibilità · **Confidenza:** `ESEGUITO` (calcolo WCAG)
- **Prove:** `#7b82ab` dà **3,16:1** sulle intestazioni di tabella (`--panel-2`), **3,81:1** sui pannelli, **4,38:1** sullo sfondo. Il minimo AA per testo normale è 4,5:1. Negli altri temi lo stesso ruolo passa (scuro 5,71 · chiaro 5,38 · monokai 5,78).
- **Impatto:** descrizioni, etichette dei campi e intestazioni di colonna diventano faticose per chi ha vista ridotta o schermo poco contrastato.
- **Standard:** WCAG 1.4.3
- **Correzione:** schiarire `--muted` nel solo tema Dracula fino ad almeno 4,5:1 su `--panel-2`, restando nella palette ufficiale.
- **Comando:** `/impeccable polish`

### F4 · [P1] Altri due colori semantici sotto AA

- **Dove:** `--accent-2` nel tema chiaro (`main.css:57`), `--danger` in Monokai (`main.css:120`)
- **Categoria:** Accessibilità · **Confidenza:** `ESEGUITO`
- **Prove:** successo `#0e9f6e` su bianco = **3,39:1**; errore `#f92672` su `#272822` = **3,93:1**. Entrambi sotto 4,5:1.
- **Impatto:** riguarda proprio i messaggi di esito e di errore, cioè il testo che l'utente deve leggere quando qualcosa va storto (`.okline`, `.koline`, `.cd-test`, `.cd-err`).
- **Standard:** WCAG 1.4.3
- **Correzione:** scurire il verde del tema chiaro, schiarire il rosso di Monokai.
- **Comando:** `/impeccable polish`

### F5 · [P1] Il bordo dei campi è quasi invisibile in tutti e quattro i temi

- **Dove:** token `--border` applicato a `input`, `select`, `textarea` in `main.css:186-196`
- **Categoria:** Accessibilità · **Confidenza:** `ESEGUITO`
- **Prove:** contrasto del bordo contro il pannello: scuro **1,29:1** · chiaro **1,36:1** · Dracula **1,56:1** · Monokai **1,61:1**. Il minimo per i confini dei componenti di interfaccia è 3:1.
- **Impatto:** il perimetro del campo compilabile non si distingue dallo sfondo; si capisce dove scrivere solo per posizione o al focus.
- **Standard:** WCAG 1.4.11 (Contrasto degli elementi non testuali)
- **Correzione:** un token dedicato al bordo dei controlli interattivi, più contrastato di quello usato per i separatori decorativi. Sono due ruoli diversi che oggi condividono un valore.
- **Comando:** `/impeccable polish`

### F6 · [P1] L'applicazione è solo desktop

- **Dove:** tutto il frontend
- **Categoria:** Responsive · **Confidenza:** `ESEGUITO` + `CODICE`
- **Prove:** in 45 file `.vue` e 2 CSS esistono **due soli** breakpoint di layout — `datasources.vue:295` (720px) e `AdminPanel.vue:586` (900px) — più uno per `prefers-reduced-motion`. L'editor usa una griglia fissa `200px minmax(0,1fr) 320px` con `height: 100vh` (`main.css:271-276`). I dialoghi hanno `width: 520px` **senza `max-width`** (`ConnectionDialog.vue:214` e analoghi): sotto i 520px di viewport escono dallo schermo. Il campo di ricerca è fissato a `width: 220px` in 7 punti.
- **Contraddizione:** `nuxt.config.ts` dichiara `viewport width=device-width`, quindi l'app si presenta come adattiva pur non essendolo.
- **Impatto:** su tablet e telefono i dialoghi vengono tagliati e l'editor è inutilizzabile. Da valutare se sia un problema reale: se è uno strumento da scrivania, la scelta è legittima ma va resa esplicita.
- **Correzione:** come minimo `max-width: min(520px, calc(100vw - 32px))` sui dialoghi; poi decidere consapevolmente quali schermate devono funzionare sotto i 900px.
- **Comando:** `/impeccable adapt`

### F7 · [P1] Bersagli tattili sotto il minimo

- **Dove:** `main.css:157-170` (bottone base), `listpage.css:90` (`.mini`)
- **Categoria:** Responsive/Accessibilità · **Confidenza:** `ESEGUITO`
- **Prove:** il bottone base è `padding: 6px 12px` su un font da 13,5px, circa **30px** di altezza. La variante `.mini` è `padding: 3px 8px`, circa **22px**, ed è usata in **65** punti (le azioni di riga delle tabelle). La `.cd-x` di chiusura dei dialoghi è `padding: 3px 7px`.
- **Impatto:** le azioni distruttive di riga (elimina) sono tra i bersagli più piccoli dell'interfaccia.
- **Standard:** WCAG 2.5.8 richiede 24×24px come minimo AA — `.mini` a ~22px è **sotto anche quel minimo**; 2.5.5 (AAA) chiede 44×44px.
- **Correzione:** portare `.mini` ad almeno 24px effettivi, con area cliccabile estesa oltre il riquadro visibile se si vuole conservare la compattezza.
- **Comando:** `/impeccable adapt`

### F8 · [P2] Due grafici su cinque non seguono il tema

- **Dove:** `components/ui/RunCalendar.vue`, `components/ui/RunGantt.vue`
- **Categoria:** Tema · **Confidenza:** `ESEGUITO`
- **Prove:** `ChartPanel.vue`, `ScheduleLoad.vue` e `pages/audit.vue` contengono tutti e tre `useTheme`, `getComputedStyle` e `watch(theme, …)`. `RunCalendar` e `RunGantt` ne hanno **zero** e portano 20 colori esadecimali fissi (`#8b97ad` per gli assi, `#34d399`/`#ef4444`/`#facc15` per gli stati).
- **Impatto:** passando al tema chiaro, assi ed etichette di quei due grafici restano tarati sul fondo scuro.
- **Correzione:** applicare lo stesso schema `readUi()` già collaudato negli altri tre. È una simmetria mancante, non un problema di progettazione.
- **Comando:** `/impeccable extract`

### F9 · [P2] Nessun indicatore di focus sui bottoni

- **Dove:** `main.css:157-180`
- **Categoria:** Accessibilità · **Confidenza:** `CODICE`
- **Prove:** i campi di input hanno un anello di focus curato (`main.css:197-201`), ma per `button` sono definiti solo `:hover`, `:active` e `:disabled`. In tutto il progetto `focus-visible` compare **1 volta**, e in un reset di Vue Flow.
- **Impatto:** navigando da tastiera non si vede dove ci si trova tra 153 bottoni. Il browser applica il suo anello di default, che su questi fondi scuri è poco visibile.
- **Standard:** WCAG 2.4.7
- **Correzione:** una regola `button:focus-visible` coerente con quella già usata sugli input.
- **Comando:** `/impeccable harden`

### F10 · [P2] Nessuna pagina autenticata ha un `<h1>`

- **Dove:** tutte le pagine tranne `login.vue`
- **Categoria:** Accessibilità · **Confidenza:** `ESEGUITO`
- **Prove:** l'unico `<h1>` del progetto è `pages/login.vue:42`. Le nove pagine principali aprono con `<h2>` (il `.page-head h2` di `listpage.css`), seguito da 12 `<h3>` e 8 `<h4>`.
- **Impatto:** la navigazione per intestazioni, che è il modo principale con cui uno screen reader esplora una pagina, parte da un livello che non esiste.
- **Standard:** WCAG 1.3.1
- **Correzione:** promuovere a `<h1>` il titolo di pagina in `.page-head`; è una modifica di un solo selettore condiviso.
- **Comando:** `/impeccable harden`

### F11 · [P2] Cinque elementi cliccabili non sono bottoni

- **Categoria:** Accessibilità · **Confidenza:** `ESEGUITO`
- **Prove:** 5 `div`/`span` con `@click` contro 153 `<button>` reali. Proporzione ottima, ma quei cinque non sono raggiungibili da tastiera.
- **Correzione:** convertirli in `<button>` o dotarli di `tabindex="0"`, `role="button"` e gestione di Invio/Spazio.
- **Comando:** `/impeccable harden`

### F12 · [P3] Sfocatura di sfondo su due barre fisse

- **Dove:** `AppShell.vue:183` e `main.css:288`, `backdrop-filter: blur(8px)`
- **Categoria:** Performance · **Confidenza:** `PLAUSIBILE`
- **Prove:** sei usi di `backdrop-filter` in totale: due su barre `sticky` a 8px, quattro sui fondali dei dialoghi a 2px.
- **Impatto:** la sfocatura su un elemento fisso viene ricalcolata a ogni scorrimento. Su tabelle lunghe e su macchine modeste può costare frame. Non misurato: senza browser non ho potuto profilare.
- **Correzione:** da valutare solo se emerge uno scorrimento a scatti; non intervenire alla cieca.
- **Comando:** `/impeccable optimize`

## Schemi ricorrenti

**L'accessibilità è stata trattata come rifinitura dei componenti nuovi, non come regola del sistema.** Tutti e quattro gli attributi ARIA presenti nel progetto stanno nei componenti più recenti — `aria-busy` sugli scheletri di caricamento, `aria-live` sui toast, `aria-label` sul banner, `aria-hidden` sull'indicatore di memoria. Sono scelte giuste e consapevoli. Ma i dialoghi, i bottoni-icona e le intestazioni, cioè la struttura portante, non sono mai stati passati al setaccio. La correzione efficace è centralizzata: un componente dialogo condiviso e una regola sui bottoni risolvono la maggior parte dei rilievi P1 senza toccare 45 file.

**Il responsive è stato affrontato solo dove è stato chiesto esplicitamente.** I due unici breakpoint sono nelle due schermate su cui abbiamo lavorato di recente su richiesta. Altrove non è mai stato posto il problema.

**I token sono il punto forte e la deriva è marginale.** 470 usi di `var(--…)` contro pochissimi valori cablati davvero fuori posto: due `#fff` in `listpage.css:46` e `:92`, più i due grafici di F8. Tutto il resto degli esadecimali sono palette di serie per ECharts, che non legge le variabili CSS: legittimi.

## Quello che funziona e va conservato

- **Bottoni veri:** 153 `<button>` contro 5 sostituti. È raro e rende gratis metà dell'accessibilità da tastiera.
- **Chiavi di lista complete:** 80 `v-for`, 80 `:key`.
- **Disciplina sulle animazioni:** zero transizioni su proprietà che provocano riflusso (larghezza, altezza, margini), zero `will-change` sparsi. Solo `transform`, `opacity`, colori e ombre.
- **ECharts importato in modo modulare** in tutti e cinque i componenti (`echarts/core` più i soli grafici usati): il bundle porta solo ciò che serve.
- **Sistema di token maturo:** quattro temi completi, con ruoli semantici derivati (`--scrim`, `--row-hover`, `--tint-accent`, `--shimmer`) invece di valori ripetuti nei componenti.
- **Tre grafici su cinque leggono i token vivi** e si ridipingono al cambio tema.
- **`prefers-reduced-motion`** disattiva lo shimmer degli scheletri (`main.css:261`) — presente e corretto.
- **Anti-lampeggio del tema:** uno script inline applica il tema salvato prima del primo disegno (`nuxt.config.ts`).
- **Font auto-ospitato** da 38K, sottoinsieme latino, nessuna CDN a runtime. Asset complessivi trascurabili.
- **Stati di caricamento con scheletri**, non rotelle al centro del contenuto: è esattamente ciò che una UI di tipo Operate dovrebbe fare.

## Azioni consigliate, in ordine

1. **[P1] `/impeccable harden`** — dialoghi (semantica, focus trap, Esc funzionante, ritorno del focus), nomi accessibili per i 56 bottoni-icona, anello di focus sui bottoni, `<h1>` di pagina, i 5 cliccabili non bottoni. Copre F1, F2, F9, F10, F11: è l'intervento con il rapporto resa/rischio migliore, e in buona parte si concentra su due file condivisi.
2. **[P1] `/impeccable adapt`** — `max-width` sui dialoghi, bersagli tattili di `.mini` ad almeno 24px, e una decisione esplicita su quali schermate devono reggere sotto i 900px. Copre F6, F7.
3. **[P2] `/impeccable extract`** — portare `RunCalendar` e `RunGantt` sullo schema `readUi()` degli altri tre grafici. Copre F8.
4. **[P1] `/impeccable polish`** — passata finale sui token di colore: `--muted` in Dracula, `--accent-2` nel chiaro, `--danger` in Monokai, e separazione del bordo dei controlli da quello dei separatori. Copre F3, F4, F5.

Nessun `/impeccable optimize`: la dimensione performance non ha rilievi che giustifichino un intervento, e senza browser non potrei misurarne l'effetto.

## Nota di contesto

Il progetto non ha `PRODUCT.md` né `DESIGN.md`. Non è un ostacolo per un audit su codice esistente — sono stati usati il codice e i token come fonte di verità — ma `/impeccable init` catturerebbe il contesto di prodotto in modo stabile, utile se in futuro si tocca l'identità visiva.

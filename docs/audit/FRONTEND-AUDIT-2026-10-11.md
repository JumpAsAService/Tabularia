# Frontend: audit tecnico e critique UX — 2026-10-11

Secondo giro `impeccable` sul frontend (il primo: [FRONTEND-AUDIT-2026-09-13.md](FRONTEND-AUDIT-2026-09-13.md)).
Questa volta due passate: l'**audit** tecnico (accessibilità, prestazioni, responsive, temi, coerenza) e la
**critique** UX (euristiche di Nielsen, carico cognitivo, persone).

## Metodo e limiti

- Quattro agenti isolati, in parallelo, ognuno con il proprio Firefox headless (Marionette):
  - **A**, revisione di design: isolata dal rilevatore, come vuole la critique;
  - **B**, rilevatore `impeccable detect` da riga di comando e iniettato nella pagina viva;
  - **C**, accessibilità e responsive;
  - **D**, temi, prestazioni e coerenza col design system.
- Le affermazioni più pesanti sono state riverificate da me nel codice o dal vivo. La barra superiore è stata
  misurata a sei larghezze; il contrasto dei bottoni primari l'ho calcolato.
- Limiti:
  - Firefox headless non scende sotto 500 px di finestra: i «telefoni» sono misurati a 500 px, e a 390 reali va peggio.
  - Lo zoom del solo testo al 200% non è stato misurato.
  - L'editor ha mostrato un viewport iniziale sbagliato **in headless**: va confermato in un browser vero.
  - Il ClickHouse esterno non rispondeva, quindi niente anteprime nell'editor; la griglia dati è stata giudicata
    nel Viewer su DuckDB.
  - Nessun import era in corso: la riga di avanzamento è valutata dal codice.
  - I riquadri del rilevatore iniettato non sono visibili a nessuno (browser headless).
- Niente è stato modificato nel repo né nei dati dell'app.

## Punteggi

### Audit tecnico: **13/20, Accettabile** (il 2026-09-13: 17/20 dopo le correzioni)

| # | Dimensione | Voto | Rilievo principale |
|---|---|---|---|
| 1 | Accessibilità | 2 | Campi senza etichetta associata (89 label orfane), Select senza semantica (67 usi), `<html>` senza `lang`, overlay e menu senza tastiera |
| 2 | Prestazioni | 3 | Viewer: un `Intl.NumberFormat` nuovo per cella a ogni render (430 ms su 7.500 contro 6,8 ms riusandolo); polling di 50 run per leggerne uno |
| 3 | Responsive | 2 | La barra superiore schiaccia la navigazione: a 1440 px Runs, Connections e Administration sono fuori vista; sotto i 600 px la nav è larga 0 px |
| 4 | Temi | 3 | Bianco sull'accento dei bottoni primari a 3,22:1 nel tema predefinito; colori letterali usati come testo nel tema chiaro (fino a 1,43:1) |
| 5 | Integrità implementativa | 3 | Sistema coerente (834 `var(--…)`), ma stili «condivisi» copiati pagina per pagina e un guscio dei dialoghi adottato da 1 dialogo su 8 |

**Perché si scende da 17 a 13.** Due cause diverse:
- **codice arrivato dopo:** l'overlay di export di Flows (07/10), la ricerca globale e l'indicatore di memoria nella
  barra superiore, la riga di avanzamento di oggi;
- **controlli che il primo giro non aveva fatto:** etichette dei moduli, semantica del Select, `lang`, colori
  letterali nel tema chiaro, contrasto del primario nel tema scuro.

### Critique UX: **22/40, Accettabile** (primo giro, nessun andamento)

| # | Euristica | Voto | Problema chiave |
|---|---|---|---|
| 1 | Visibilità dello stato | 3 | Buoni avanzamento e scheletri. Però l'editor non dice «non salvato», e il calendario dei run è verde anche nei giorni di soli fallimenti |
| 2 | Corrispondenza col mondo reale | 2 | Id grezzi (nodi `group_by`, `FAILURE`, voce `sample`); numeri e date it-IT dentro l'interfaccia inglese |
| 3 | Controllo e libertà | 2 | Nessuna guardia all'uscita dall'editor; eliminazioni con `confirm()` nativo, senza annulla |
| 4 | Coerenza | 2 | Due griglie dati diverse, tre formati data, quattro stili di badge, `<select>` nativi accanto a `Select.vue` |
| 5 | Prevenzione degli errori | 2 | Cestini rossi sempre visibili; segnaposto che sembrano valori; lavoro dell'editor non protetto |
| 6 | Riconoscere invece di ricordare | 2 | Azioni di riga solo icona (5 in Flows, 4 in Admin); nodi che non dicono cosa fanno |
| 7 | Flessibilità ed efficienza | 2 | Ci sono annulla, ripeti, copia e Ctrl+Enter. Mancano Ctrl+S, una scorciatoia per la ricerca e le azioni multiple |
| 8 | Estetica e minimalismo | 3 | Sistema disciplinato; rumore dall'indicatore di memoria, dalla colonna di cestini e dai grafici in cima a Flows |
| 9 | Recupero dagli errori | 2 | Login ottimo. Su Runs invece: errori in italiano, con l'host in testa, troncati prima della causa e doppi |
| 10 | Aiuto e documentazione | 2 | Buoni aiuti in linea; nessun accesso a una guida dall'app |

**Carico cognitivo: alto**, 4 fallimenti su 8 voci della lista:
- **attenzione singola:** su Flows i grafici spingono la lista sotto la piega;
- **raggruppamento a blocchi di 4:** non rispettato;
- **scelte minime:** la palette dell'editor ha circa 17 voci in ordine alfabetico, il tipo di connessione è una
  lista piatta di 10 voci;
- **memoria di lavoro:** bisogna cliccare ogni nodo per sapere cosa fa, e non c'è un link dalla datasource ai suoi dati.

## Verdetto di specificità e integrità — si parte da qui

**Scritto per Tabularia nel guscio, intercambiabile proprio dove sta il dato.**

**Cosa è specifico del prodotto:**
- il login, col campo che ordina valori sporchi in una tabella;
- quattro temi da un solo vocabolario di ruoli;
- la codifica per categoria dei nodi;
- la voce sobria dei testi.

**Dove si perde.** Il login promette valori «allineati a destra in celle pulite», ma:
- il Viewer centra i numeri, li scrive in formato italiano dentro l'interfaccia inglese e raggruppa periodi e id
  come migliaia («202.607»);
- il canvas chiama i nodi col loro id grezzo («compute — 1 parameters»).

**Rilevatore** (`impeccable detect`):
- **da riga di comando, 2 rilievi `side-tab`:**
  - `BannerBar.vue:50` reale: il lato spesso non aggiunge informazione al bordo colorato e all'icona;
  - `lineage.vue:613` falso positivo: codifica il tipo di nodo, come `.node`.
- **nel browser:**
  - contrasto dei bottoni primari a 3,2:1 (Sign in, Run, New);
  - 45 tag a 10 px su Datasources;
  - lo stato della toolbar dell'editor tagliato senza puntini (`text-overflow` su `inline-flex`);
  - due «bordo sottile + ombra larga» solo consultivi (toast e card di login).
- **falsi positivi:** `side-tab` sui nodi dell'editor (il server del rilevatore girava senza la config del progetto),
  `ai-color-palette` sulle icone delle operazioni, il ritaglio voluto di Vue Flow e le sovrapposizioni generate dalle
  etichette del rilevatore stesso.

Il rilevatore ha trovato il contrasto del primario, che la revisione di design non aveva visto; la revisione ha trovato
tutto il resto, che il rilevatore non vede.

## Rilievi per gravità

**0 P0 · 9 P1 · 12 P2 · circa 20 P3.** Le lettere fra parentesi dicono quale agente l'ha trovato.

### F1 · [P1] La barra superiore schiaccia la navigazione (A, C, misurato)

- **Dove:** `AppShell.vue:182-183, 257-330, 431-436`; `MemoryGauge.vue:44` (210 px, `nowrap`); `GlobalSearch.vue:252`.
  Marchio, ricerca globale, indicatore di memoria e ingranaggio non si restringono: la nav prende solo quello che avanza.
- **Misura** (Firefox, interfaccia inglese, utente admin):

  | Larghezza | Voci della nav fuori vista |
  |---|---|
  | 1920 | nessuna |
  | 1600 | Administration |
  | 1440 | Connections, Runs, Administration |
  | 1280 | anche Assistant |
  | 800 | resta solo Explore |
  | 500 | nav larga 0 px; l'ingranaggio (tema, lingua, esci) a 584 px su 500, e la pagina scorre di lato |

- **Impatto:** Runs è la pagina del controllo mattutino e alla larghezza più comune non si vede. Sul telefono l'app non
  è navigabile.
- **Regressione:** la scelta «la nav scorre su mobile» del 13/09 non funziona più da quando ci sono la ricerca globale e
  l'indicatore di memoria.
- **Correzione:**
  - la nav ha la precedenza (`min-width`, gli altri `flex-shrink`);
  - l'indicatore di memoria va in Admin o diventa solo icona;
  - la ricerca diventa un'icona con ⌘K sotto i 1500 px.
- **Comandi:** `/impeccable layout`, `/impeccable adapt`.

### F2 · [P1] Numeri e date: formato italiano fisso e griglia centrata (A, D)

- **Formato fisso:** `toLocaleString('it-IT')` / `Intl.*('it-IT')` è fisso in 16 punti di 13 file: `viewer.vue:204`,
  `datasources.vue:91-92, 244`, `ProjectBrowser.vue:150-152`, `runs.vue:31`, `flows.vue:214`, `audit.vue:148`,
  `lineage.vue:229`, `performance.vue:62,72`, `ChartPanel.vue:180`, `RunCalendar.vue:100`, `RunGantt.vue:34`,
  `AdminPanel.vue:86`. Altri 7 punti usano la lingua del browser e 3 quella dell'interfaccia: tre regimi diversi.
- **Viewer:** `.dt td` è `text-align: center` (`viewer.vue:631`), con decimali variabili per cella e raggruppamento su
  id e periodi.
- **Stessa riga, due formati:** la riga di avanzamento di oggi scrive «1,250,000 rows received» nella lingua
  dell'interfaccia, il conteggio finale «1.250.000».
- **Date ambigue:** «11/10/26, 10:27» un americano lo legge 10 novembre.
- **Prestazioni:** `viewer.vue:204` costruisce un formatter per cella a ogni render (430 ms su 7.500 celle). Ogni tasto
  nei filtri ridisegna fino a 500 righe.
- **Correzione:**
  - un `useFormat()` legato alla lingua di vue-i18n, con i formatter in cache;
  - numeri allineati a destra con `tabular-nums` e decimali uniformi per colonna;
  - nessun raggruppamento su id e periodi;
  - un formato data unico e non ambiguo.
- **Comandi:** `/impeccable harden`, `/impeccable typeset`, `/impeccable optimize`.

### F3 · [P1] Il lavoro nell'editor non è protetto (A)

- **Cosa manca:**
  - un indicatore «non salvato»;
  - una guardia all'uscita (nessun `beforeunload` né `onBeforeRouteLeave` in tutto il frontend, verificato);
  - Ctrl/Cmd+S: `FlowEditor.vue:973-1011` gestisce solo z/y/c/x/v/d, e Ctrl+S apre il «Salva pagina» del browser.
- **Barra:** «Projects» e «Log out» stanno accanto a «Run» (`Toolbar.vue:135-136`).
- **Correzione:** stato «sporco» da `useFlowHistory`, Ctrl/Cmd+S, guardia all'uscita con un dialogo dell'app.
- **Comando:** `/impeccable harden`.

### F4 · [P1] Il canvas non si legge a colpo d'occhio (A)

- **Titolo del nodo:** è `data.opType` grezzo (`OperationNode.vue:37`), mentre la palette usa le etichette tradotte.
- **Corpo del nodo:** «{n} parameters» senza plurale (`en.gen.ts:732`), quindi «1 parameters».
- **In headless, da confermare:** viewport iniziale a `scale(2)` con 0 nodi visibili; «Adatta» con lo zoom minimo di
  0,5 mostra 1 nodo su 32.
- **Correzione:**
  - titolo dall'etichetta tradotta, o un nome scelto dall'utente;
  - corpo con un riassunto di una riga («by categoria · 3 aggregati»);
  - `minZoom` circa 0,2, `fitView` a nodi misurati;
  - minimappa sopra i 15 nodi;
  - palette raggruppata per categoria colore.
- **Comandi:** `/impeccable clarify`, `/impeccable layout`.

### F5 · [P1] Moduli: etichette, Select e lingua (C)

- **Etichette:**
  - 89 `<label>` senza `for` né campo dentro, 17 campi senza alcun nome, 73 nominati solo dal segnaposto;
  - dal vivo: Username, Password e Database del dialogo connessione hanno nome vuoto;
  - i 9 campi «descrizione colonna» di Datasources hanno tutti lo stesso nome.
- **`Select.vue:175-224`** (67 usi): senza `aria-haspopup`/`aria-expanded`, `role="listbox"`/`option` né
  `aria-activedescendant`; 62 su 67 senza etichetta.
- **`<html>` senza `lang`** dal server (`useLocale.ts:18` lo imposta solo al cambio lingua).
- **Bene:** i due dialoghi nuovi (export dbt, contratti) sono corretti.
- **WCAG:** 1.3.1, 3.3.2, 4.1.2, 3.1.1.
- **Comando:** `/impeccable harden`.

### F6 · [P1] Overlay, menu e righe solo col mouse (C, D)

- **L'overlay «Export flow» di Flows** (`flows.vue:381-396`, quello con dbt e OpenLineage) non è un dialogo:
  - senza `role` né `aria-modal`;
  - il fuoco resta dietro e Esc non chiude;
  - z-index 200 invece di 2000.
- **Menu e cassetti senza tastiera:**
  - menu Amministrazione e impostazioni senza Esc;
  - popover «New» e «Save view» senza tastiera;
  - il cassetto della cartella in Explore è `role=dialog` ma il fuoco non ci entra.
- **Riga dei gruppi in Admin:** un `<tr @click>` (`AdminPanel.vue:514-520`), è il vecchio F11 mai corretto. Senza
  mouse non si gestiscono i membri di un gruppo.
- **Esc dentro un Select chiude l'intero dialogo** (`useDialogA11y.ts:42-46`, listener in cattura): i dati inseriti si
  perdono.
- **Correzione:** passare tutto su `Modal.vue` / `useDialogA11y`; il Select ferma l'Esc quando è aperto.
- **Comando:** `/impeccable harden`.

### F7 · [P1] Contrasto: primario, informativa, tema chiaro (B, D, calcolato)

- **Bottoni primari nel tema scuro predefinito:** bianco su `#4f8cff` = **3,22:1**, su `#7c6cff` = 3,86, sotto i 4,5
  per testo a 13 px. Tocca *tutte* le azioni primarie (Sign in, Run, Save, New). Il tema chiaro passa (4,63), dracula e
  monokai usano testo scuro e passano.
- **`PrivacyNotice.vue:86`:** `#fff` su `var(--accent)`: 1,65 monokai, 2,41 dracula. Sulla prima schermata che vede un
  esterno.
- **Tema chiaro, colori tarati sullo scuro usati come testo:**
  - pillole dei run 1,73 (`runs.vue:130-131`);
  - badge del motore 1,43-2,27 (`Toolbar.vue:162-165`);
  - campione del nodo sorgente 1,67;
  - titolo del commento 1,47;
  - etichette del lineage 1,5-3,4.
- **Bianco su `--danger` all'hover:** 2,78.
- **Correzione:**
  - per il primario serve una decisione: un riempimento più scuro nel tema scuro (`#3b6fe0` dà 4,63) oppure testo scuro
    sul blu (6,00:1);
  - `--on-accent` nell'informativa;
  - varianti testuali dei colori di codifica per tema;
  - un token `--on-danger`.
- **Comando:** `/impeccable polish`.

### F8 · [P1] La riga di avanzamento di oggi annuncia ogni secondo (A, C, D) — regressione mia

- **Accessibilità:** `.dprog` ha `role="status"` dentro il bottone della riga (`datasources.vue:232`), e il testo cambia
  ogni secondo. Uno screen reader annuncia per minuti, per ogni import.
- **Prestazioni:** durante un import il ticchettio ridisegna tutta la pagina Datasources e ricostruisce tutte le righe
  di Explore (`ProjectBrowser.vue:164-167`).
- **Bene:** il contrasto passa in tutti i temi.
- **Correzione:**
  - testo che ticchetta senza ruolo;
  - una sola regione nascosta, fuori dal bottone, che annuncia solo i cambi di fase e la fine;
  - un componente `<RunProgressText>` che possiede il timer, acceso solo quando c'è un avanzamento.
- **Comandi:** `/impeccable harden`, `/impeccable optimize`.

### F9 · [P1] Explore e Flows sugli schermi medi e stretti (C)

- **Explore:** `.rmeta` è `nowrap` senza limite (`ProjectBrowser.vue:967`). A 768 px la tabella è larga 1.123 px e le
  azioni di riga escono dallo schermo: vale da 721 a circa 1.170 px, cioè tablet o un 1440 con zoom al 125-150%.
- **Flows:** le azioni di riga (`flows.vue:416`, `overflow: hidden`) vengono tagliate su schermi stretti.
- **Altre pagine:** il Viewer mette il selettore della datasource fuori schermo a 500 px; Connections, Runs e Audit
  traboccano.
- **Comando:** `/impeccable adapt`.

### P2

- **F10 · Ricerca globale che porta a pagina 1.** Manda a `/datasources?ds=` e `/connections?conn=` (`useSearch.ts:35-36`),
  ma nessuna delle due pagine legge il parametro (verificato). E da una datasource non c'è strada verso il Viewer.
  `/impeccable layout`.
- **F11 · Righe dominate dalle azioni distruttive.**
  - 11 cestini rossi sempre visibili alla radice di Explore;
  - `confirm()` nativo in 17 punti, senza dire cosa si perde e senza annulla;
  - Flows ha 5 icone per riga.
  
  `/impeccable distill`, `/impeccable harden`.
- **F12 · Bottoni di sola icona: 16 senza nome, 30 solo col `title`.** **Si chiude la decisione aperta del 13/09 senza
  chiavi nuove:** `contracts.close` per le X, `queue.removeLabel` e `adminPanel.addButton` per rimuovi e aggiungi, la
  chiave del `title` come `aria-label`. Elenco completo nella nota C. `/impeccable harden`.
- **F13 · Campi di ricerca disegnati 5 volte.**
  - 3 aspetti diversi;
  - `--border` invece di `--control-border` (1,3:1);
  - fuoco quasi invisibile (alone al 18%).
  
  `/impeccable polish`.
- **F14 · Stringhe fuori dall'i18n.**
  - tooltip di MemoryGauge in italiano in tutte le lingue;
  - «Accedi con Keycloak» sul login in inglese (`OIDC__BUTTON_LABEL` monolingua);
  - stati grezzi SUCCESS e FAILURE;
  - «Flows» e il tag «db» scritti a mano;
  - segnaposto d'esempio in italiano.
  
  `/impeccable harden`.
- **F15 · Annunci e movimento.**
  - toast che spariscono in 3,5 o 6,5 s, e spesso sono l'unico posto dell'errore;
  - la chat intera è una live region, tabelle comprese;
  - archi animati del canvas, pallino «live» e due shimmer ignorano `prefers-reduced-motion`.
  
  `/impeccable harden`, `/impeccable animate`.
- **F16 · Liste che si svuotano e polling pesante.**
  - Explore al ritorno sulla scheda e Datasources a fine import rimettono lo scheletro: la lista sparisce e un campo
    descrizione aperto perde il fuoco;
  - il polling chiede 50 run con il traceback ogni 2,5 s per datasource, per leggerne uno.
  
  `/impeccable optimize`.
- **F17 · Componenti di sistema copiati.**
  - `.mini` in 8 varianti;
  - `.page-head h1` definito 7 volte (il titolo scende di 13 px fra Datasources e Runs);
  - `Modal.vue` usato da 1 dialogo su 8;
  - 12 `<select>` nativi;
  - 3 scheletri diversi.
  
  `/impeccable polish`.
- **F18 · «In corso» in tre colori, verde decorativo.**
  - «in corso» è blu, verde o giallo a seconda della pagina;
  - il verde, che è il colore dell'esito positivo, colora anche le cartelle e il trigger «schedule»;
  - il calendario dei run è verde nei giorni di soli fallimenti (`RunCalendar.vue:139`).
  
  `/impeccable polish`.
- **F19 · Titoli.** Explore e Monitoring senza `h1`; Admin salta da `h1` a `h4`; il titolo di Performance è a circa
  27 px. `/impeccable harden`.
- **F20 · Testo piccolo.**
  - tag a 10 px maiuscoli: 45 su Datasources, segnalati dal rilevatore;
  - 9 px in 3 punti;
  - `--muted` sbiadito con `opacity` sotto AA in 5 punti.
  
  `/impeccable typeset`.
- **F21 · Runs: errori in italiano, con l'host in testa, troncati prima della causa, e ogni fallimento due volte.**
  Gli errori del motore in italiano sono il lavoro già segnato (132 EngineError); il doppio e il troncamento sono
  dell'interfaccia. `/impeccable clarify`.

### P3, rifiniture

- **Rilevatore e toolbar:**
  - `BannerBar.vue:50` `side-tab` reale;
  - lo stato della toolbar non mostra i puntini (`Toolbar.vue:170-179`).
- **Colori fissi o variabili inesistenti:**
  - puntini del canvas col colore fisso `#2a2f3a` (`FlowEditor.vue:1725`);
  - bagliori dell'accento fissi sul blu del tema scuro;
  - `var(--op-rows)` e `var(--mono)` mai definite;
  - nessun `accent-color` sulle checkbox.
- **Datasources e grafici:**
  - colonne di Datasources non allineate fra righe con 2 e 4 azioni, e nessuna intestazione;
  - legenda di Schedule load sopra l'asse.
- **Bundle e documentazione:**
  - le 5 lingue sono tutte nel bundle d'ingresso;
  - DESIGN.md non documenta `--warning`, la tabella `.dt` del Viewer né `Modal.vue`.
- **Monospace:**
  - lo stack è ancora aperto: 24 dichiarazioni, 4 varianti;
  - 17 `<code>`/`<pre>`/`<kbd>` restano sul font del browser.
- **Testi e piccoli componenti:**
  - `AiDisclosure` senza «rinuncia»;
  - il lineage «Whole graph» è illeggibile;
  - titoli Grafana in italiano;
  - «0 online workers» mostrato come numero neutro.
- **ConnectionDialog e barre:**
  - nel dialogo connessione: segnaposto che sembrano valori (ORCLPDB1, HR, dbo), la porta prima dell'host,
    «Not encrypted» senza riga di conseguenza, tipo di connessione in 10 voci piatte (il Select supporta i gruppi);
  - nel Viewer il selettore della datasource sta all'estrema destra pur essendo la prima scelta.

## Persone

- **Alex, utente esperto:**
  - niente Ctrl+S né scorciatoia per la ricerca;
  - nessuna azione multipla (i flussi si spostano uno a uno);
  - a 1440 px Runs è fuori vista;
  - palette di 17 voci alfabetiche, con `sample` non tradotta.
- **Sam, accessibilità:**
  - campi senza nome nel dialogo connessione;
  - Select muto;
  - avanzamento annunciato ogni secondo;
  - export che non trattiene il fuoco;
  - gruppi Admin solo col mouse;
  - Esc in un Select che chiude il dialogo;
  - `lang` assente;
  - toast che spariscono.
- **Giulia, l'analista di ogni mattina:**
  1. atterra su cartelle e cestini, senza recenti né run falliti;
  2. Runs non è in vista;
  3. gli errori sono troncati;
  4. il flusso si apre con lo zoom sbagliato e i nodi «compute — 1 parameters»;
  5. dalla datasource non c'è strada verso i dati;
  6. il Viewer centra «47.088.587,779»;
  7. esce dall'editor e perde le modifiche senza avviso.
- **L'esterno di un'azienda cliente, all'accesso:**
  - impressione forte;
  - ma «Accedi con Keycloak» sotto «Sign in» (lingua mista e gergo infrastrutturale);
  - un secondo bottone blu, «Details», compete col primario e passa il contrasto solo nel tema chiaro;
  - nessuna indicazione su chi contattare dopo un errore.

## Cosa regge e va conservato

- **Dialoghi:** `useDialogA11y` funziona (fuoco, Esc, ritorno); dialoghi larghi al massimo lo schermo meno 32 px.
- **Bottoni:** l'anello di fuoco da 2 px; `.mini` a 24 px.
- **Grafici:** i sei componenti ECharts sono tutti su `useChartTheme`, quindi il F8 del 13/09 è risolto.
- **Contrasto dei token:** AA sul pannello in tutti i temi, e le correzioni del 13/09 reggono.
- **Codice nuovo fatto bene:**
  - `ContractBadge` dice lo stato con forma, colore e parole;
  - i dialoghi export dbt e contratti hanno le etichette giuste;
  - ConnectionDialog (Oracle, SQL Server) usa `Select` e ha le chiavi nelle 5 lingue;
  - la riga di avanzamento passa il contrasto in tutti i temi.
- **Igiene delle prestazioni:**
  - nessun `will-change`;
  - transizioni solo su transform, opacity e colori;
  - timer chiusi allo smontaggio;
  - presenza e indicatore di memoria in pausa a scheda nascosta;
  - liste paginate a 50;
  - font ospitato in casa.
- **Il login:** è l'unica superficie con un'identità vera, rispetta il movimento ridotto e ha le uniche etichette
  associate dell'app.

## Azioni consigliate, in ordine

1. **[P1] `/impeccable harden`** — F8 (subito: è di oggi), F5, F6, F3, poi F12, F14, F15, F19.
2. **[P1] `/impeccable layout` + `/impeccable adapt`** — F1 (nav), F9, F10.
3. **[P1] `/impeccable polish`** — F7 (dopo la decisione sul primario), F13, F18, F17.
4. **[P1] `/impeccable typeset` + `/impeccable optimize`** — F2 (`useFormat`, griglia), F16, il Viewer.
5. **[P1] `/impeccable clarify`** — F4 (nodi), F21.
6. **[P2] `/impeccable distill`** — F11.
7. **[P3] `/impeccable document`** — allineare DESIGN.md.
8. Ultima passata: **`/impeccable polish`**.

## Dove sono le prove

Screenshot, JSON del rilevatore e misure dal vivo sono rimasti sulla macchina dove si è fatto il giro, fuori dal
repo; tutti i numeri citati sopra sono riportati qui.

## Correzioni dello stesso giorno: i nove P1

Scelte del proprietario:
- partire da orientamento e dati;
- primario del tema scuro in un blu più scuro, testo bianco;
- tutti i P1, a blocchi.

Ogni blocco è stato provato nel browser con uno script suo (avanzamento, blocco 1, contrasto, blocco 3, blocco 4;
script locali, fuori dal repo) e seguito dalle suite del browser.

| Rilievo | Stato | Cosa è cambiato | Prova |
|---|---|---|---|
| F8 avanzamento che parla ogni secondo | chiuso | `RunProgressText` possiede l'orologio (solo mentre c'è un avanzamento); il testo che ticchetta è `aria-hidden`; resta un «importing…» stabile; la pagina annuncia inizio e fine una volta | 11/11 |
| F1 navigazione schiacciata | chiuso | la navigazione ha la precedenza: indicatore di memoria a sole tacche sotto i 1800px (numeri nel tooltip, tradotto) e nascosto sul telefono; ricerca stretta sotto i 1600px che si allarga mentre la si usa e sul telefono è una lente; marchio senza nome sotto i 1100px | a 1440px 9 voci su 9 in vista (prima 6); a 500px nav larga 333px (prima 0) |
| F2 numeri e date | chiuso | `composables/useFormat.ts` sulla lingua dell'interfaccia, formatter in cache; i 16 `it-IT` fissi tolti; date col mese in lettere; Viewer coi numeri a destra, decimali uguali per colonna, interi senza separatore; anteprima dell'editor coi numeri a destra ma valori grezzi | 15/15 |
| F9 schermi medi e stretti | chiuso | `.rmeta` di Explore tagliato a 36ch col testo intero nel tooltip; righe di Flows con solo nome e azioni sotto i 760px | Explore a 768px non trabocca; Flows sul telefono tiene le azioni |
| F10 ricerca globale | chiuso (era P2, dentro l'area scelta) | il collegamento porta anche il nome (`?q=…&ds=…`), quindi la voce si trova oltre la prima pagina; la datasource si apre ed entra in vista, la connessione è evidenziata | 2/2 |
| F7 contrasto | chiuso | gradiente dei primari del tema scuro `#3b6fe0 → #6a5cff`; hover che scurisce (0,92) con bagliore dal tema; `--danger-fill`/`--on-danger`; `--enc-text-mix` per tema (100/40/80/85%) sui colori di codifica usati come testo; informativa privacy: «Details» neutro, il collegamento coi token del primario | 16/16 misure sopra 4,5 nei 4 temi |
| F5 moduli | chiuso | direttiva `v-label-fields` (shell, editor, dialoghi) che collega ogni etichetta al campo che la segue; Select come combobox/listbox; `<html lang>` dal server; nomi ai campi col solo segnaposto | campi senza nome: 0 su Viewer, Lineage, Admin, Datasources, editor |
| F6 overlay e menù | chiuso | scelta dell'export su `Modal.vue`; menù Amministrazione con frecce, Home/End, Esc; `usePopover` (impostazioni, «New», «Save view»); cassetto della cartella modale; gruppi Admin da tastiera; Esc nel Select chiude solo il Select | 17/17 |
| F3 editor senza protezione | chiuso | «non salvato» accanto al nome; uscita (Projects, logout, link) con «resta / esci senza salvare / salva ed esci»; avviso del browser alla chiusura; Ctrl/Cmd+S; il logout chiede PRIMA di togliere la sessione | 14/14, su una copia di un flusso da 32 nodi |
| F4 canvas | chiuso | titolo = nome tradotto dell'operazione; riassunto di una riga dai parametri (`composables/opSummary.ts`, monospace); plurale vero; `sample` col suo nome; vista adattata quando i nodi hanno le misure (`maxZoom: 1`), `minZoom` 0,2 | il flusso da 32 nodi entra tutto all'apertura |

**Restano aperti:**
- i dodici P2 e i P3, che il proprietario ha lasciato per dopo;
- la minimappa del canvas: serve `@vue-flow/minimap`, una dipendenza nuova da decidere;
- un dettaglio visto nella prova: i controlli dello zoom si sovrappongono un poco alla prima colonna di nodi quando un flusso molto largo viene adattato.

Il punteggio non è stato rimisurato: il prossimo `/impeccable audit` lo dirà.

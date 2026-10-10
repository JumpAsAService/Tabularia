"""Gli errori dell'export dbt nella lingua di chi li legge.

Gateway ed engine sollevano `DbtExportError` con un CODICE e i suoi parametri (e un
testo italiano, che resta nei log); qui il codice diventa una frase nella lingua
della richiesta. Un codice sconosciuto, o un parametro che manca, non rompe la
risposta: si torna al testo originale."""
from __future__ import annotations

from app.core.lingua import DEFAULT

# codice → lingua → frase. I parametri fra graffe.
MESSAGGI: dict[str, dict[str, str]] = {
    "internal": {
        "en": "The dbt export failed: {detail}",
        "it": "L'export dbt non è riuscito: {detail}",
        "de": "Der dbt-Export ist fehlgeschlagen: {detail}",
        "es": "La exportación dbt ha fallado: {detail}",
        "fr": "L'export dbt a échoué : {detail}",
    },
    "flow_resolve": {
        "en": "The flow cannot be read: {detail}",
        "it": "Il flusso non si legge: {detail}",
        "de": "Der Flow kann nicht gelesen werden: {detail}",
        "es": "El flujo no se puede leer: {detail}",
        "fr": "Le flux ne peut pas être lu : {detail}",
    },
    "source_no_connection": {
        "en": "The source «{source}» has no valid connection.",
        "it": "La sorgente «{source}» non ha una connessione valida.",
        "de": "Die Quelle «{source}» hat keine gültige Verbindung.",
        "es": "La fuente «{source}» no tiene una conexión válida.",
        "fr": "La source «{source}» n'a pas de connexion valide.",
    },
    "not_federable": {
        "en": "The connection «{connection}» is {db_type}: DuckDB cannot attach it (it attaches PostgreSQL, MySQL and MariaDB). Use the ClickHouse or the native target.",
        "it": "La connessione «{connection}» è {db_type}: DuckDB non la collega (collega PostgreSQL, MySQL e MariaDB). Usa il target ClickHouse o quello nativo.",
        "de": "Die Verbindung «{connection}» ist {db_type}: DuckDB kann sie nicht anbinden (es bindet PostgreSQL, MySQL und MariaDB an). Verwenden Sie das ClickHouse- oder das native Ziel.",
        "es": "La conexión «{connection}» es {db_type}: DuckDB no puede conectarla (conecta PostgreSQL, MySQL y MariaDB). Usa el destino ClickHouse o el nativo.",
        "fr": "La connexion «{connection}» est {db_type} : DuckDB ne peut pas l'attacher (il attache PostgreSQL, MySQL et MariaDB). Utilisez la cible ClickHouse ou la cible native.",
    },
    "upstream_missing": {
        "en": "The source «{source}» is the output of a flow that no longer exists.",
        "it": "La sorgente «{source}» è l'uscita di un flusso che non esiste più.",
        "de": "Die Quelle «{source}» ist die Ausgabe eines Flows, den es nicht mehr gibt.",
        "es": "La fuente «{source}» es la salida de un flujo que ya no existe.",
        "fr": "La source «{source}» est la sortie d'un flux qui n'existe plus.",
    },
    "upstream_too_deep": {
        "en": "Too many upstream flows in a chain (more than {max}): the source «{source}» cannot be included.",
        "it": "Troppi flussi a monte in catena (oltre {max}): la sorgente «{source}» non si può includere.",
        "de": "Zu viele vorgelagerte Flows in einer Kette (mehr als {max}): die Quelle «{source}» kann nicht aufgenommen werden.",
        "es": "Demasiados flujos anteriores en cadena (más de {max}): la fuente «{source}» no se puede incluir.",
        "fr": "Trop de flux en amont en chaîne (plus de {max}) : la source «{source}» ne peut pas être incluse.",
    },
    "flow_cycle": {
        "en": "Cycle between flows: «{flow}» depends on itself through «{source}».",
        "it": "Ciclo fra flussi: «{flow}» dipende da sé stesso attraverso «{source}».",
        "de": "Zyklus zwischen Flows: «{flow}» hängt über «{source}» von sich selbst ab.",
        "es": "Ciclo entre flujos: «{flow}» depende de sí mismo a través de «{source}».",
        "fr": "Cycle entre flux : «{flow}» dépend de lui-même via «{source}».",
    },
    "upstream_no_output": {
        "en": "The flow «{flow}» has no output that publishes «{source}»: run it again to regenerate the datasource.",
        "it": "Il flusso «{flow}» non ha un'uscita che pubblica «{source}»: rilancialo per rigenerare la datasource.",
        "de": "Der Flow «{flow}» hat keine Ausgabe, die «{source}» veröffentlicht: führen Sie ihn erneut aus, um die Datenquelle neu zu erzeugen.",
        "es": "El flujo «{flow}» no tiene una salida que publique «{source}»: ejecútalo de nuevo para regenerar la fuente de datos.",
        "fr": "Le flux «{flow}» n'a pas de sortie qui publie «{source}» : relancez-le pour régénérer la source de données.",
    },
    "no_outputs": {
        "en": "The flow «{flow}» has no output that can be exported (only emails?).",
        "it": "Il flusso «{flow}» non ha uscite esportabili (solo email?).",
        "de": "Der Flow «{flow}» hat keine exportierbare Ausgabe (nur E-Mails?).",
        "es": "El flujo «{flow}» no tiene salidas exportables (¿solo emails?).",
        "fr": "Le flux «{flow}» n'a pas de sortie exportable (seulement des e-mails ?).",
    },
    "materialization_not_allowed": {
        "en": "The output «{model}» cannot be «{choice}»: choose among {allowed}.",
        "it": "L'uscita «{model}» non può essere «{choice}»: si può scegliere fra {allowed}.",
        "de": "Die Ausgabe «{model}» kann nicht «{choice}» sein: wählen Sie zwischen {allowed}.",
        "es": "La salida «{model}» no puede ser «{choice}»: elige entre {allowed}.",
        "fr": "La sortie «{model}» ne peut pas être «{choice}» : choisissez parmi {allowed}.",
    },
    "unknown_target": {
        "en": "Unknown target: {target}.",
        "it": "Target sconosciuto: {target}.",
        "de": "Unbekanntes Ziel: {target}.",
        "es": "Destino desconocido: {target}.",
        "fr": "Cible inconnue : {target}.",
    },
    "pivot_not_portable": {
        "en": "A «pivot» node makes columns decided by the data: it exports only with the federated target (dbt-duckdb), or put in its place a sql node that lists the columns.",
        "it": "Il nodo «pivot» produce colonne decise dai dati: si esporta solo col target federato (dbt-duckdb), oppure al suo posto un nodo sql che elenchi le colonne.",
        "de": "Ein «pivot»-Knoten erzeugt Spalten, die von den Daten abhängen: er lässt sich nur mit dem föderierten Ziel (dbt-duckdb) exportieren, oder ersetzen Sie ihn durch einen sql-Knoten, der die Spalten aufzählt.",
        "es": "Un nodo «pivot» genera columnas que dependen de los datos: solo se exporta con el destino federado (dbt-duckdb), o pon en su lugar un nodo sql que enumere las columnas.",
        "fr": "Un nœud «pivot» produit des colonnes qui dépendent des données : il ne s'exporte qu'avec la cible fédérée (dbt-duckdb), ou mettez à sa place un nœud sql qui liste les colonnes.",
    },
    "foreach_not_exportable": {
        "en": "A «foreach» node cannot be exported: dbt has no loop at run time. Leave it out of the flow, or export the branches separately.",
        "it": "Il nodo «foreach» non si esporta: dbt non ha un ciclo a runtime. Escludilo dal flusso o esporta i rami separatamente.",
        "de": "Ein «foreach»-Knoten lässt sich nicht exportieren: dbt hat keine Schleife zur Laufzeit. Lassen Sie ihn weg oder exportieren Sie die Zweige einzeln.",
        "es": "Un nodo «foreach» no se puede exportar: dbt no tiene un bucle en tiempo de ejecución. Déjalo fuera del flujo o exporta las ramas por separado.",
        "fr": "Un nœud «foreach» ne s'exporte pas : dbt n'a pas de boucle à l'exécution. Retirez-le du flux ou exportez les branches séparément.",
    },
    "native_one_connection": {
        "en": "The native target needs every source on the SAME connection (one warehouse), and this flow uses more than one: use the ClickHouse or the federated target.",
        "it": "Il target nativo vuole tutte le sorgenti sulla STESSA connessione (un solo warehouse), e questo flusso ne usa più di una: usa il target ClickHouse o quello federato.",
        "de": "Das native Ziel braucht alle Quellen auf DERSELBEN Verbindung (ein Warehouse), und dieser Flow nutzt mehrere: verwenden Sie das ClickHouse- oder das föderierte Ziel.",
        "es": "El destino nativo necesita todas las fuentes en la MISMA conexión (un solo warehouse), y este flujo usa más de una: usa el destino ClickHouse o el federado.",
        "fr": "La cible native exige toutes les sources sur la MÊME connexion (un seul warehouse), et ce flux en utilise plusieurs : utilisez la cible ClickHouse ou la cible fédérée.",
    },
    "native_needs_database": {
        "en": "The native target needs at least one source from a database: this flow reads only files.",
        "it": "Il target nativo vuole almeno una sorgente da database: questo flusso legge solo file.",
        "de": "Das native Ziel braucht mindestens eine Quelle aus einer Datenbank: dieser Flow liest nur Dateien.",
        "es": "El destino nativo necesita al menos una fuente de una base de datos: este flujo solo lee archivos.",
        "fr": "La cible native exige au moins une source issue d'une base de données : ce flux ne lit que des fichiers.",
    },
    "no_native_adapter": {
        "en": "There is no native dbt adapter for {db_type}.",
        "it": "Non c'è un adapter dbt nativo per {db_type}.",
        "de": "Es gibt keinen nativen dbt-Adapter für {db_type}.",
        "es": "No hay un adaptador dbt nativo para {db_type}.",
        "fr": "Il n'existe pas d'adaptateur dbt natif pour {db_type}.",
    },
    "sql_unreadable": {
        "en": "The query of a sql node cannot be read ({detail}).",
        "it": "La query di un nodo sql non si legge ({detail}).",
        "de": "Die Abfrage eines sql-Knotens kann nicht gelesen werden ({detail}).",
        "es": "La consulta de un nodo sql no se puede leer ({detail}).",
        "fr": "La requête d'un nœud sql ne peut pas être lue ({detail}).",
    },
    "columns_unknown_after_sql": {
        "en": "{step}: the columns are not known at this point of the flow (after a sql node), and this target needs them. Use the federated target, or have the sql node list its columns.",
        "it": "{step}: le colonne non sono note a questo punto del flusso (dopo un nodo sql), e questo target le vuole. Usa il target federato, oppure fai elencare le colonne al nodo sql.",
        "de": "{step}: die Spalten sind an dieser Stelle des Flows unbekannt (nach einem sql-Knoten), und dieses Ziel braucht sie. Verwenden Sie das föderierte Ziel oder lassen Sie den sql-Knoten seine Spalten aufzählen.",
        "es": "{step}: las columnas no se conocen en este punto del flujo (después de un nodo sql), y este destino las necesita. Usa el destino federado, o haz que el nodo sql enumere sus columnas.",
        "fr": "{step} : les colonnes ne sont pas connues à ce point du flux (après un nœud sql), et cette cible en a besoin. Utilisez la cible fédérée, ou faites lister ses colonnes au nœud sql.",
    },
    "cast_unsupported_type": {
        "en": "cast: unsupported type «{type}».",
        "it": "cast: tipo non supportato «{type}».",
        "de": "cast: nicht unterstützter Typ «{type}».",
        "es": "cast: tipo no soportado «{type}».",
        "fr": "cast : type non pris en charge «{type}».",
    },
    "drop_nulls_columns_unknown": {
        "en": "drop nulls: the columns are not known at this point of the flow (after a sql node or a pivot): list them in the node.",
        "it": "drop nulls: le colonne non sono note a questo punto del flusso (dopo un nodo sql o un pivot): indicale nel nodo.",
        "de": "drop nulls: die Spalten sind an dieser Stelle des Flows unbekannt (nach einem sql-Knoten oder einem Pivot): geben Sie sie im Knoten an.",
        "es": "drop nulls: las columnas no se conocen en este punto del flujo (después de un nodo sql o un pivot): indícalas en el nodo.",
        "fr": "drop nulls : les colonnes ne sont pas connues à ce point du flux (après un nœud sql ou un pivot) : indiquez-les dans le nœud.",
    },
    "groupby_unsupported_func": {
        "en": "aggregate: unsupported function «{func}».",
        "it": "aggregazione: funzione non supportata «{func}».",
        "de": "Aggregation: nicht unterstützte Funktion «{func}».",
        "es": "agregación: función no soportada «{func}».",
        "fr": "agrégation : fonction non prise en charge «{func}».",
    },
    "compute_missing": {
        "en": "compute: the name and the expression are both required.",
        "it": "compute: nome ed espressione sono obbligatori.",
        "de": "compute: Name und Ausdruck sind beide erforderlich.",
        "es": "compute: el nombre y la expresión son obligatorios.",
        "fr": "compute : le nom et l'expression sont tous deux obligatoires.",
    },
    "union_columns_unknown": {
        "en": "union by name: the columns of one branch are not known (after a sql node or a pivot), and this target needs them.",
        "it": "unione per nome: le colonne di uno dei due rami non sono note (dopo un nodo sql o un pivot), e questo target le vuole.",
        "de": "Vereinigung nach Name: die Spalten eines Zweigs sind unbekannt (nach einem sql-Knoten oder einem Pivot), und dieses Ziel braucht sie.",
        "es": "unión por nombre: las columnas de una de las ramas no se conocen (después de un nodo sql o un pivot), y este destino las necesita.",
        "fr": "union par nom : les colonnes d'une des branches ne sont pas connues (après un nœud sql ou un pivot), et cette cible en a besoin.",
    },
    "join_unsupported": {
        "en": "join: unsupported type «{how}».",
        "it": "join: tipo non supportato «{how}».",
        "de": "Join: nicht unterstützter Typ «{how}».",
        "es": "join: tipo no soportado «{how}».",
        "fr": "jointure : type non pris en charge «{how}».",
    },
    "pivot_unsupported_func": {
        "en": "pivot: unsupported function «{func}».",
        "it": "pivot: funzione non supportata «{func}».",
        "de": "Pivot: nicht unterstützte Funktion «{func}».",
        "es": "pivot: función no soportada «{func}».",
        "fr": "pivot : fonction non prise en charge «{func}».",
    },
    "unpivot_nothing": {
        "en": "unpivot: no column to turn into rows.",
        "it": "unpivot: nessuna colonna da trasformare in righe.",
        "de": "Unpivot: keine Spalte, die in Zeilen umgewandelt werden kann.",
        "es": "unpivot: ninguna columna para convertir en filas.",
        "fr": "unpivot : aucune colonne à transformer en lignes.",
    },
    "op_not_exportable": {
        "en": "The operation «{op}» cannot be exported to dbt.",
        "it": "L'operazione «{op}» non si esporta in dbt.",
        "de": "Die Operation «{op}» lässt sich nicht nach dbt exportieren.",
        "es": "La operación «{op}» no se puede exportar a dbt.",
        "fr": "L'opération «{op}» ne s'exporte pas vers dbt.",
    },
    "contract_rule_invalid": {
        "en": "The contract rule «{rule}» is incomplete: it cannot become a dbt test.",
        "it": "La regola del contratto «{rule}» è incompleta: non può diventare un test dbt.",
        "de": "Die Vertragsregel «{rule}» ist unvollständig: sie kann kein dbt-Test werden.",
        "es": "La regla del contrato «{rule}» está incompleta: no puede convertirse en un test dbt.",
        "fr": "La règle du contrat «{rule}» est incomplète : elle ne peut pas devenir un test dbt.",
    },
    "rule_no_equivalent": {
        "en": "The contract rule «{kind}» has no dbt equivalent.",
        "it": "La regola del contratto «{kind}» non ha un equivalente dbt.",
        "de": "Die Vertragsregel «{kind}» hat keine Entsprechung in dbt.",
        "es": "La regla del contrato «{kind}» no tiene equivalente en dbt.",
        "fr": "La règle du contrat «{kind}» n'a pas d'équivalent dbt.",
    },
    "translation_failed": {
        "en": "The translation to {dialect} failed ({detail}): the flow is too complex for this target, use the federated target (dbt-duckdb).",
        "it": "La traduzione in {dialect} non è riuscita ({detail}): il flusso è troppo complesso per questo target, usa quello federato (dbt-duckdb).",
        "de": "Die Übersetzung nach {dialect} ist fehlgeschlagen ({detail}): der Flow ist für dieses Ziel zu komplex, verwenden Sie das föderierte Ziel (dbt-duckdb).",
        "es": "La traducción a {dialect} ha fallado ({detail}): el flujo es demasiado complejo para este destino, usa el federado (dbt-duckdb).",
        "fr": "La traduction vers {dialect} a échoué ({detail}) : le flux est trop complexe pour cette cible, utilisez la cible fédérée (dbt-duckdb).",
    },
    "other_clickhouse": {
        "en": "The connection «{connection}» is ANOTHER ClickHouse ({host}): the ClickHouse target reads the Postgres/MySQL sources and the tables of its own server.",
        "it": "La connessione «{connection}» è un ALTRO ClickHouse ({host}): il target ClickHouse legge le sorgenti Postgres/MySQL e le tabelle del suo stesso server.",
        "de": "Die Verbindung «{connection}» ist ein ANDERES ClickHouse ({host}): das ClickHouse-Ziel liest die Postgres/MySQL-Quellen und die Tabellen seines eigenen Servers.",
        "es": "La conexión «{connection}» es OTRO ClickHouse ({host}): el destino ClickHouse lee las fuentes Postgres/MySQL y las tablas de su propio servidor.",
        "fr": "La connexion «{connection}» est un AUTRE ClickHouse ({host}) : la cible ClickHouse lit les sources Postgres/MySQL et les tables de son propre serveur.",
    },
    "clickhouse_cannot_read": {
        "en": "The connection «{connection}» is {db_type}: ClickHouse reads only PostgreSQL, MySQL and MariaDB. Use another target for this flow.",
        "it": "La connessione «{connection}» è {db_type}: ClickHouse legge solo PostgreSQL, MySQL e MariaDB. Usa un altro target per questo flusso.",
        "de": "Die Verbindung «{connection}» ist {db_type}: ClickHouse liest nur PostgreSQL, MySQL und MariaDB. Verwenden Sie für diesen Flow ein anderes Ziel.",
        "es": "La conexión «{connection}» es {db_type}: ClickHouse solo lee PostgreSQL, MySQL y MariaDB. Usa otro destino para este flujo.",
        "fr": "La connexion «{connection}» est {db_type} : ClickHouse ne lit que PostgreSQL, MySQL et MariaDB. Utilisez une autre cible pour ce flux.",
    },
    "query_not_translatable": {
        "en": "The query of the datasource «{datasource}» does not translate to ClickHouse ({detail}): use the federated target.",
        "it": "La query della datasource «{datasource}» non si traduce in ClickHouse ({detail}): usa il target federato.",
        "de": "Die Abfrage der Datenquelle «{datasource}» lässt sich nicht nach ClickHouse übersetzen ({detail}): verwenden Sie das föderierte Ziel.",
        "es": "La consulta de la fuente de datos «{datasource}» no se traduce a ClickHouse ({detail}): usa el destino federado.",
        "fr": "La requête de la source de données «{datasource}» ne se traduit pas en ClickHouse ({detail}) : utilisez la cible fédérée.",
    },
    "query_not_single_select": {
        "en": "The query of the datasource «{datasource}» is not a single SELECT: it does not translate to ClickHouse.",
        "it": "La query della datasource «{datasource}» non è una sola SELECT: non si traduce in ClickHouse.",
        "de": "Die Abfrage der Datenquelle «{datasource}» ist kein einzelnes SELECT: sie lässt sich nicht nach ClickHouse übersetzen.",
        "es": "La consulta de la fuente de datos «{datasource}» no es un único SELECT: no se traduce a ClickHouse.",
        "fr": "La requête de la source de données «{datasource}» n'est pas un SELECT unique : elle ne se traduit pas en ClickHouse.",
    },
    "macro_value_invalid": {
        "en": "The value «{value}» has characters the dbt project cannot write in a macro.",
        "it": "Il valore «{value}» contiene caratteri che il progetto dbt non può scrivere in una macro.",
        "de": "Der Wert «{value}» enthält Zeichen, die das dbt-Projekt nicht in ein Makro schreiben kann.",
        "es": "El valor «{value}» contiene caracteres que el proyecto dbt no puede escribir en una macro.",
        "fr": "La valeur «{value}» contient des caractères que le projet dbt ne peut pas écrire dans une macro.",
    },
    "duckdb_federation_unavailable": {
        "en": "DuckDB cannot attach {db_type}.",
        "it": "DuckDB non collega {db_type}.",
        "de": "DuckDB kann {db_type} nicht anbinden.",
        "es": "DuckDB no puede conectar {db_type}.",
        "fr": "DuckDB ne peut pas attacher {db_type}.",
    },
    "folder_invalid": {
        "en": "Invalid folder name: «{folder}».",
        "it": "Nome di cartella non valido: «{folder}».",
        "de": "Ungültiger Ordnername: «{folder}».",
        "es": "Nombre de carpeta no válido: «{folder}».",
        "fr": "Nom de dossier invalide : «{folder}».",
    },
    "seed_too_large": {
        "en": "The source «{source}» has {rows} rows: too many for a dbt seed (at most {max}). Import it into a database and use that as the source.",
        "it": "La sorgente «{source}» ha {rows} righe: troppe per un seed dbt (al più {max}). Importala in un database e usa quella come sorgente.",
        "de": "Die Quelle «{source}» hat {rows} Zeilen: zu viele für einen dbt-Seed (höchstens {max}). Importieren Sie sie in eine Datenbank und nutzen Sie diese als Quelle.",
        "es": "La fuente «{source}» tiene {rows} filas: demasiadas para un seed dbt (como máximo {max}). Impórtala en una base de datos y úsala como fuente.",
        "fr": "La source «{source}» a {rows} lignes : trop pour un seed dbt (au plus {max}). Importez-la dans une base de données et utilisez celle-ci comme source.",
    },
    "ai_unavailable": {
        "en": "The AI is not available: the assistant is not configured, or no model is enabled.",
        "it": "L'AI non è disponibile: l'assistente non è configurato, o nessun modello è abilitato.",
        "de": "Die KI ist nicht verfügbar: der Assistent ist nicht konfiguriert oder kein Modell ist aktiviert.",
        "es": "La IA no está disponible: el asistente no está configurado o no hay ningún modelo habilitado.",
        "fr": "L'IA n'est pas disponible : l'assistant n'est pas configuré, ou aucun modèle n'est activé.",
    },
    "ai_cap_reached": {
        "en": "The AI has reached today's spending cap: export without AI, or try again tomorrow.",
        "it": "L'AI ha raggiunto il tetto di spesa di oggi: esporta senza AI, o riprova domani.",
        "de": "Die KI hat das heutige Ausgabenlimit erreicht: exportieren Sie ohne KI oder versuchen Sie es morgen erneut.",
        "es": "La IA ha alcanzado el límite de gasto de hoy: exporta sin IA, o vuelve a intentarlo mañana.",
        "fr": "L'IA a atteint le plafond de dépenses du jour : exportez sans IA, ou réessayez demain.",
    },
    "ai_failed": {
        "en": "The AI did not answer ({detail}): try again, or export without AI.",
        "it": "L'AI non ha risposto ({detail}): riprova, o esporta senza AI.",
        "de": "Die KI hat nicht geantwortet ({detail}): versuchen Sie es erneut oder exportieren Sie ohne KI.",
        "es": "La IA no ha respondido ({detail}): vuelve a intentarlo, o exporta sin IA.",
        "fr": "L'IA n'a pas répondu ({detail}) : réessayez, ou exportez sans IA.",
    },
    "ai_translation_invalid": {
        "en": "The AI's translation of «{model}» did not pass the checks: {reason}. Export with another target, or without the AI translation.",
        "it": "La traduzione dell'AI di «{model}» non passa i controlli: {reason}. Esporta con un altro target, o senza la traduzione dell'AI.",
        "de": "Die KI-Übersetzung von «{model}» besteht die Prüfungen nicht: {reason}. Exportieren Sie mit einem anderen Ziel oder ohne KI-Übersetzung.",
        "es": "La traducción de la IA de «{model}» no pasa los controles: {reason}. Exporta con otro destino, o sin la traducción de la IA.",
        "fr": "La traduction de l'IA pour «{model}» ne passe pas les contrôles : {reason}. Exportez avec une autre cible, ou sans la traduction de l'IA.",
    },
    "seed_unreadable": {
        "en": "Seed «{seed}»: the file cannot be read ({detail}).",
        "it": "Seed «{seed}»: il file non si legge ({detail}).",
        "de": "Seed «{seed}»: die Datei kann nicht gelesen werden ({detail}).",
        "es": "Seed «{seed}»: el archivo no se puede leer ({detail}).",
        "fr": "Seed «{seed}» : le fichier ne peut pas être lu ({detail}).",
    },
}


# perché una traduzione proposta dall'AI non passa i controlli dell'engine
MOTIVI_AI: dict[str, dict[str, str]] = {
    "unparsable": {"en": "it does not parse", "it": "non si legge", "de": "sie ist nicht lesbar", "es": "no se puede leer", "fr": "elle ne se lit pas"},
    "not_select": {"en": "it is not a single SELECT", "it": "non è una sola SELECT", "de": "sie ist kein einzelnes SELECT",
                   "es": "no es un único SELECT", "fr": "ce n'est pas un SELECT unique"},
    "writes": {"en": "it contains statements that write", "it": "contiene istruzioni che scrivono", "de": "sie enthält schreibende Anweisungen",
               "es": "contiene instrucciones que escriben", "fr": "elle contient des instructions qui écrivent"},
    "table_function": {"en": "it reads a table function", "it": "legge una table function", "de": "sie liest eine Tabellenfunktion",
                       "es": "lee una función de tabla", "fr": "elle lit une fonction de table"},
    "unknown_table": {"en": "it reads a table the model does not read ({table})", "it": "legge una tabella che il modello non legge ({table})",
                      "de": "sie liest eine Tabelle, die das Modell nicht liest ({table})", "es": "lee una tabla que el modelo no lee ({table})",
                      "fr": "elle lit une table que le modèle ne lit pas ({table})"},
    "star": {"en": "it uses * instead of listing the columns", "it": "usa * invece di elencare le colonne", "de": "sie nutzt * statt die Spalten aufzuzählen",
             "es": "usa * en lugar de enumerar las columnas", "fr": "elle utilise * au lieu de lister les colonnes"},
    "syntax": {"en": "ClickHouse does not read it ({detail})", "it": "ClickHouse non la legge ({detail})",
               "de": "ClickHouse kann sie nicht lesen ({detail})", "es": "ClickHouse no la lee ({detail})", "fr": "ClickHouse ne la lit pas ({detail})"},
    "columns": {"en": "its columns are {got} instead of {expected}", "it": "le sue colonne sono {got} invece di {expected}",
                "de": "ihre Spalten sind {got} statt {expected}", "es": "sus columnas son {got} en lugar de {expected}",
                "fr": "ses colonnes sont {got} au lieu de {expected}"},
}


def traduci(code: str | None, params: dict | None, lingua: str, testo_originale: str = "") -> str:
    """La frase del codice nella lingua chiesta (o in inglese); senza codice, o con
    un parametro che manca, il testo originale."""
    modelli = MESSAGGI.get(code or "")
    if not modelli:
        return testo_originale or code or ""
    p = dict(params or {})
    if code == "internal" and "detail" not in p:
        p["detail"] = testo_originale
    if code == "ai_translation_invalid" and p.get("reason") in MOTIVI_AI:
        motivo = MOTIVI_AI[p["reason"]]
        try:
            p["reason"] = (motivo.get(lingua) or motivo[DEFAULT]).format(**p)
        except (KeyError, IndexError, ValueError):
            p["reason"] = motivo[DEFAULT]
    try:
        return (modelli.get(lingua) or modelli[DEFAULT]).format(**p)
    except (KeyError, IndexError, ValueError):
        return testo_originale or modelli[DEFAULT]


def messaggio_dell_errore(e: Exception, lingua: str) -> str:
    """Un `DbtExportError` (del gateway) nella lingua chiesta."""
    return traduci(getattr(e, "code", None), getattr(e, "params", None), lingua, str(e))


def messaggio_dell_engine(detail, lingua: str) -> str:
    """Il `detail` di un 422 dell'engine: {code, params, message} o, da un engine
    vecchio, un testo."""
    if isinstance(detail, dict):
        return traduci(detail.get("code"), detail.get("params"), lingua, str(detail.get("message") or ""))
    return str(detail)

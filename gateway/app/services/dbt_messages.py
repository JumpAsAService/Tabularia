"""The dbt export's errors in the language of whoever reads them.

Gateway and engine raise `DbtExportError` with a CODE and its parameters (and an
English text, which stays in the logs); here the code becomes a sentence in the
request's language. An unknown code, or a missing parameter, does not break the
answer: it falls back to the original text."""
from __future__ import annotations

from app.core.language import DEFAULT

# code → language → sentence. The parameters in braces.
MESSAGES: dict[str, dict[str, str]] = {
    "internal": {
        "en": "The dbt export failed: {detail}",
        "it": "L'export dbt non è riuscito: {detail}",
        "de": "Der dbt-Export ist fehlgeschlagen: {detail}",
        "es": "La exportación dbt ha fallado: {detail}",
        "fr": "L'export dbt a échoué : {detail}",
    },
    "engine_timeout": {
        "en": "The engine did not build the dbt project within {seconds} s: try again in a moment. If it happens again, split the flow, or raise ENGINE__DBT_EXPORT_TIMEOUT_SECONDS (together with the ingress' timeout).",
        "it": "Il motore non ha costruito il progetto dbt entro {seconds} s: riprova fra poco. Se succede ancora, dividi il flusso, o alza ENGINE__DBT_EXPORT_TIMEOUT_SECONDS (insieme al timeout dell'ingress).",
        "de": "Die Engine hat das dbt-Projekt nicht innerhalb von {seconds} s erstellt: versuchen Sie es gleich noch einmal. Wenn es wieder passiert, teilen Sie den Flow auf, oder erhöhen Sie ENGINE__DBT_EXPORT_TIMEOUT_SECONDS (zusammen mit dem Timeout des Ingress).",
        "es": "El motor no ha construido el proyecto dbt en {seconds} s: vuelve a intentarlo en un momento. Si vuelve a pasar, divide el flujo, o aumenta ENGINE__DBT_EXPORT_TIMEOUT_SECONDS (junto con el timeout del ingress).",
        "fr": "Le moteur n'a pas construit le projet dbt en {seconds} s : réessayez dans un instant. Si cela se reproduit, divisez le flux, ou augmentez ENGINE__DBT_EXPORT_TIMEOUT_SECONDS (avec le délai de l'ingress).",
    },
    "engine_unreachable": {
        "en": "The engine cannot be reached ({detail}): try again in a moment; if it happens again, check that the engine service is running.",
        "it": "Il motore non è raggiungibile ({detail}): riprova fra poco; se succede ancora, controlla che il servizio del motore sia attivo.",
        "de": "Die Engine ist nicht erreichbar ({detail}): versuchen Sie es gleich noch einmal; wenn es wieder passiert, prüfen Sie, ob der Engine-Dienst läuft.",
        "es": "No se puede contactar con el motor ({detail}): vuelve a intentarlo en un momento; si vuelve a pasar, comprueba que el servicio del motor está en marcha.",
        "fr": "Le moteur est injoignable ({detail}) : réessayez dans un instant ; si cela se reproduit, vérifiez que le service du moteur est en marche.",
    },
    "flow_resolve": {
        "en": "The flow «{flow}» cannot be read: {detail}",
        "it": "Il flusso «{flow}» non si legge: {detail}",
        "de": "Der Flow «{flow}» kann nicht gelesen werden: {detail}",
        "es": "El flujo «{flow}» no se puede leer: {detail}",
        "fr": "Le flux «{flow}» ne peut pas être lu : {detail}",
    },
    "output_no_source": {
        "en": "In the flow «{flow}», the Output «{output}» has no source with data upstream (or its source datasource has no snapshot): connect it to a source, or remove it.",
        "it": "Nel flusso «{flow}», l'Output «{output}» non ha a monte una sorgente con dati (o la sua datasource sorgente non ha uno snapshot): collegalo a una sorgente, o eliminalo.",
        "de": "Im Flow «{flow}» hat der Output «{output}» keine vorgelagerte Quelle mit Daten (oder seine Quelldatenquelle hat keinen Snapshot): verbinden Sie ihn mit einer Quelle oder entfernen Sie ihn.",
        "es": "En el flujo «{flow}», el Output «{output}» no tiene antes una fuente con datos (o su fuente de datos de origen no tiene snapshot): conéctalo a una fuente o elimínalo.",
        "fr": "Dans le flux «{flow}», l'Output «{output}» n'a pas de source avec des données en amont (ou sa source de données d'origine n'a pas de snapshot) : reliez-le à une source, ou supprimez-le.",
    },
    "flow_loop": {
        "en": "The flow «{flow}» has a loop through the node «{node}»: remove the edge that closes it.",
        "it": "Il flusso «{flow}» ha un ciclo che passa per il nodo «{node}»: elimina l'arco che lo chiude.",
        "de": "Der Flow «{flow}» hat eine Schleife über den Knoten «{node}»: entfernen Sie die Kante, die sie schließt.",
        "es": "El flujo «{flow}» tiene un bucle que pasa por el nodo «{node}»: elimina la conexión que lo cierra.",
        "fr": "Le flux «{flow}» a une boucle qui passe par le nœud «{node}» : supprimez le lien qui la ferme.",
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
    "source_not_exportable": {
        "en": "The connection «{connection}» is {db_type}, which no dbt target reads: dbt-duckdb attaches PostgreSQL, MySQL and MariaDB, ClickHouse reads those, and dbt has no native adapter for {db_type}. Copy the data into a PostgreSQL or MySQL table and have the flow read it from there.",
        "it": "La connessione «{connection}» è {db_type}, che nessun target dbt legge: dbt-duckdb collega PostgreSQL, MySQL e MariaDB, ClickHouse legge quelli, e dbt non ha un adapter nativo per {db_type}. Copia i dati in una tabella PostgreSQL o MySQL e fai leggere al flusso quella.",
        "de": "Die Verbindung «{connection}» ist {db_type}, und kein dbt-Ziel liest sie: dbt-duckdb bindet PostgreSQL, MySQL und MariaDB an, ClickHouse liest diese, und dbt hat keinen nativen Adapter für {db_type}. Kopieren Sie die Daten in eine PostgreSQL- oder MySQL-Tabelle und lassen Sie den Flow sie dort lesen.",
        "es": "La conexión «{connection}» es {db_type}, y ningún destino dbt la lee: dbt-duckdb conecta PostgreSQL, MySQL y MariaDB, ClickHouse lee esas, y dbt no tiene un adaptador nativo para {db_type}. Copia los datos en una tabla PostgreSQL o MySQL y haz que el flujo los lea de allí.",
        "fr": "La connexion «{connection}» est {db_type}, qu'aucune cible dbt ne lit : dbt-duckdb attache PostgreSQL, MySQL et MariaDB, ClickHouse lit ceux-là, et dbt n'a pas d'adaptateur natif pour {db_type}. Copiez les données dans une table PostgreSQL ou MySQL et faites-les lire au flux depuis celle-ci.",
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
        "en": "The flow «{flow}» has no output that publishes «{source}» (its outputs publish {published}): the datasource was renamed or moved after the flow published it, or the flow's Output changed. Point the Output of «{flow}» at «{source}» again (same name and folder), or have this flow read the datasource «{flow}» publishes now.",
        "it": "Il flusso «{flow}» non ha un'uscita che pubblica «{source}» (le sue uscite pubblicano {published}): la datasource è stata rinominata o spostata dopo che il flusso l'ha pubblicata, oppure l'Output del flusso è cambiato. Riallinea l'Output di «{flow}» a «{source}» (stesso nome e cartella), o fai leggere a questo flusso la datasource che «{flow}» pubblica ora.",
        "de": "Der Flow «{flow}» hat keine Ausgabe, die «{source}» veröffentlicht (seine Ausgaben veröffentlichen {published}): die Datenquelle wurde umbenannt oder verschoben, nachdem der Flow sie veröffentlicht hat, oder der Output des Flows hat sich geändert. Richten Sie den Output von «{flow}» wieder auf «{source}» aus (gleicher Name und Ordner), oder lassen Sie diesen Flow die Datenquelle lesen, die «{flow}» jetzt veröffentlicht.",
        "es": "El flujo «{flow}» no tiene una salida que publique «{source}» (sus salidas publican {published}): la fuente de datos se renombró o se movió después de que el flujo la publicara, o el Output del flujo cambió. Vuelve a apuntar el Output de «{flow}» a «{source}» (mismo nombre y carpeta), o haz que este flujo lea la fuente de datos que «{flow}» publica ahora.",
        "fr": "Le flux «{flow}» n'a pas de sortie qui publie «{source}» (ses sorties publient {published}) : la source de données a été renommée ou déplacée après que le flux l'a publiée, ou l'Output du flux a changé. Réalignez l'Output de «{flow}» sur «{source}» (même nom et même dossier), ou faites lire à ce flux la source de données que «{flow}» publie maintenant.",
    },
    "upstream_output_ambiguous": {
        "en": "In the flow «{flow}» more than one Output publishes «{source}»: which one wrote it last depends on the order they ran in, so the export cannot tell which steps made it. Keep one Output for «{source}» in «{flow}» (remove the others, or give them another name).",
        "it": "Nel flusso «{flow}» più di un Output pubblica «{source}»: quale l'ha scritta per ultimo dipende dall'ordine in cui sono girati, quindi l'export non può sapere quali passi l'hanno prodotta. Tieni un solo Output per «{source}» in «{flow}» (elimina gli altri, o dagli un altro nome).",
        "de": "Im Flow «{flow}» veröffentlicht mehr als ein Output «{source}»: welcher sie zuletzt geschrieben hat, hängt von der Reihenfolge ab, in der sie liefen, daher kann der Export nicht wissen, welche Schritte sie erzeugt haben. Behalten Sie in «{flow}» nur einen Output für «{source}» (entfernen Sie die anderen oder geben Sie ihnen einen anderen Namen).",
        "es": "En el flujo «{flow}» más de un Output publica «{source}»: cuál la escribió por última vez depende del orden en que se ejecutaron, así que la exportación no puede saber qué pasos la produjeron. Deja un solo Output para «{source}» en «{flow}» (elimina los demás o dales otro nombre).",
        "fr": "Dans le flux «{flow}», plus d'un Output publie «{source}» : celui qui l'a écrite en dernier dépend de l'ordre dans lequel ils ont tourné, l'export ne peut donc pas savoir quelles étapes l'ont produite. Gardez un seul Output pour «{source}» dans «{flow}» (supprimez les autres, ou donnez-leur un autre nom).",
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
    "script_not_exportable": {
        "en": "The output «{output}» of the flow «{flow}» reads what the Script node «{script}» writes: Python or R code runs on Tabularia's cluster, not in dbt, so it cannot be exported. Export the outputs before the script, or write its logic with sql nodes.",
        "it": "L'output «{output}» del flusso «{flow}» legge quello che scrive il nodo Script «{script}»: il codice Python o R gira sul cluster di Tabularia, non in dbt, quindi non si può esportare. Esporta gli output prima dello script, o scrivi la sua logica con nodi sql.",
        "de": "Die Ausgabe «{output}» des Flows «{flow}» liest, was der Skript-Knoten «{script}» schreibt: Python- oder R-Code läuft im Cluster von Tabularia, nicht in dbt, und lässt sich daher nicht exportieren. Exportieren Sie die Ausgaben vor dem Skript, oder schreiben Sie seine Logik mit sql-Knoten.",
        "es": "La salida «{output}» del flujo «{flow}» lee lo que escribe el nodo Script «{script}»: el código Python o R se ejecuta en el clúster de Tabularia, no en dbt, así que no se puede exportar. Exporta las salidas anteriores al script, o escribe su lógica con nodos sql.",
        "fr": "La sortie «{output}» du flux «{flow}» lit ce qu'écrit le nœud Script «{script}» : le code Python ou R s'exécute sur le cluster de Tabularia, pas dans dbt, il ne peut donc pas être exporté. Exportez les sorties avant le script, ou écrivez sa logique avec des nœuds sql.",
    },
    "container_not_exportable": {
        "en": "The output «{output}» of the flow «{flow}» reads what the Container node «{container}» writes: an image runs on Tabularia's cluster, not in dbt, so it cannot be exported. Export the outputs before the container, or write its logic with sql nodes.",
        "it": "L'output «{output}» del flusso «{flow}» legge quello che scrive il nodo Container «{container}»: un'immagine gira sul cluster di Tabularia, non in dbt, quindi non si può esportare. Esporta gli output prima del container, o scrivi la sua logica con nodi sql.",
        "de": "Die Ausgabe «{output}» des Flows «{flow}» liest, was der Container-Knoten «{container}» schreibt: ein Image läuft im Cluster von Tabularia, nicht in dbt, und lässt sich daher nicht exportieren. Exportieren Sie die Ausgaben vor dem Container, oder schreiben Sie seine Logik mit sql-Knoten.",
        "es": "La salida «{output}» del flujo «{flow}» lee lo que escribe el nodo Container «{container}»: una imagen se ejecuta en el clúster de Tabularia, no en dbt, así que no se puede exportar. Exporta las salidas anteriores al contenedor, o escribe su lógica con nodos sql.",
        "fr": "La sortie «{output}» du flux «{flow}» lit ce qu'écrit le nœud Container «{container}» : une image s'exécute sur le cluster de Tabularia, pas dans dbt, elle ne peut donc pas être exportée. Exportez les sorties avant le conteneur, ou écrivez sa logique avec des nœuds sql.",
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
        "en": "There is no native dbt adapter for {db_type}. The federated target (dbt-duckdb) and the ClickHouse target read PostgreSQL, MySQL and MariaDB sources live.",
        "it": "Non c'è un adapter dbt nativo per {db_type}. Il target federato (dbt-duckdb) e il target ClickHouse leggono dal vivo le sorgenti PostgreSQL, MySQL e MariaDB.",
        "de": "Es gibt keinen nativen dbt-Adapter für {db_type}. Das föderierte Ziel (dbt-duckdb) und das ClickHouse-Ziel lesen PostgreSQL-, MySQL- und MariaDB-Quellen live.",
        "es": "No hay un adaptador dbt nativo para {db_type}. El destino federado (dbt-duckdb) y el destino ClickHouse leen en vivo las fuentes PostgreSQL, MySQL y MariaDB.",
        "fr": "Il n'existe pas d'adaptateur dbt natif pour {db_type}. La cible fédérée (dbt-duckdb) et la cible ClickHouse lisent en direct les sources PostgreSQL, MySQL et MariaDB.",
    },
    "source_name_duplicate": {
        "en": "Two sources are both named «{name}» and both have a table «{table}» ({sources}): dbt cannot tell them apart. Give each one its own name.",
        "it": "Due sorgenti si chiamano entrambe «{name}» e hanno entrambe una tabella «{table}» ({sources}): dbt non le distingue. Dai a ciascuna un nome diverso.",
        "de": "Zwei Quellen heißen beide «{name}» und haben beide eine Tabelle «{table}» ({sources}): dbt kann sie nicht unterscheiden. Geben Sie jeder einen eigenen Namen.",
        "es": "Dos fuentes se llaman «{name}» y ambas tienen una tabla «{table}» ({sources}): dbt no puede distinguirlas. Da a cada una su propio nombre.",
        "fr": "Deux sources s'appellent toutes deux «{name}» et ont toutes deux une table «{table}» ({sources}) : dbt ne peut pas les distinguer. Donnez à chacune son propre nom.",
    },
    "name_not_quotable": {
        "en": "The name «{name}» has the character {character}, which {adapter} cannot quote in a table name: dbt would write SQL the database rejects. Use another target, or rename it.",
        "it": "Il nome «{name}» contiene il carattere {character}, che {adapter} non sa quotare nel nome di una tabella: dbt scriverebbe un SQL che il database rifiuta. Usa un altro target, o rinominalo.",
        "de": "Der Name «{name}» enthält das Zeichen {character}, das {adapter} in einem Tabellennamen nicht quoten kann: dbt würde SQL schreiben, das die Datenbank ablehnt. Verwenden Sie ein anderes Ziel oder benennen Sie ihn um.",
        "es": "El nombre «{name}» contiene el carácter {character}, que {adapter} no sabe entrecomillar en el nombre de una tabla: dbt escribiría un SQL que la base de datos rechaza. Usa otro destino, o renómbralo.",
        "fr": "Le nom «{name}» contient le caractère {character}, que {adapter} ne sait pas mettre entre guillemets dans un nom de table : dbt écrirait un SQL que la base de données refuse. Utilisez une autre cible, ou renommez-le.",
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
    "text_not_translatable": {
        # followed by the advice for the text's dialect (DIALECT_ADVICE)
        "en": "The step «{step}» is written for the {dialect} engine and cannot be translated for this target ({detail}).",
        "it": "Il passo «{step}» è scritto per il motore {dialect} e non si può tradurre per questo target ({detail}).",
        "de": "Der Schritt «{step}» ist für die Engine {dialect} geschrieben und kann für dieses Ziel nicht übersetzt werden ({detail}).",
        "es": "El paso «{step}» está escrito para el motor {dialect} y no se puede traducir para este destino ({detail}).",
        "fr": "L'étape «{step}» est écrite pour le moteur {dialect} et ne peut pas être traduite pour cette cible ({detail}).",
    },
    "columns_differ_only_by_case": {
        "en": "The columns {columns} differ only by case (step «{step}»): DuckDB, which the federated target runs on, reads them as one column. Rename one of them in the flow, or use the native or the ClickHouse target.",
        "it": "Le colonne {columns} differiscono solo per maiuscole e minuscole (passo «{step}»): DuckDB, su cui gira il target federato, le legge come una colonna sola. Rinominane una nel flusso, o usa il target nativo o ClickHouse.",
        "de": "Die Spalten {columns} unterscheiden sich nur in Groß- und Kleinschreibung (Schritt «{step}»): DuckDB, auf dem das föderierte Ziel läuft, liest sie als eine Spalte. Benennen Sie eine davon im Flow um, oder verwenden Sie das native oder das ClickHouse-Ziel.",
        "es": "Las columnas {columns} solo se diferencian en mayúsculas y minúsculas (paso «{step}»): DuckDB, sobre el que corre el destino federado, las lee como una sola columna. Renombra una de ellas en el flujo, o usa el destino nativo o ClickHouse.",
        "fr": "Les colonnes {columns} ne diffèrent que par la casse (étape «{step}») : DuckDB, sur lequel tourne la cible fédérée, les lit comme une seule colonne. Renommez l'une d'elles dans le flux, ou utilisez la cible native ou ClickHouse.",
    },
    "column_names_too_long": {
        "en": "The columns {columns} are the same column in Postgres, which keeps only the first {limit} bytes of a name. Shorten one of them in the flow, or use the federated or the ClickHouse target.",
        "it": "Le colonne {columns} sono la stessa colonna in Postgres, che tiene solo i primi {limit} byte di un nome. Accorciane una nel flusso, o usa il target federato o ClickHouse.",
        "de": "Die Spalten {columns} sind in Postgres dieselbe Spalte, da Postgres nur die ersten {limit} Bytes eines Namens behält. Kürzen Sie eine davon im Flow, oder verwenden Sie das föderierte oder das ClickHouse-Ziel.",
        "es": "Las columnas {columns} son la misma columna en Postgres, que solo conserva los primeros {limit} bytes de un nombre. Acorta una de ellas en el flujo, o usa el destino federado o ClickHouse.",
        "fr": "Les colonnes {columns} sont la même colonne dans Postgres, qui ne garde que les {limit} premiers octets d'un nom. Raccourcissez l'une d'elles dans le flux, ou utilisez la cible fédérée ou ClickHouse.",
    },
    "cast_unsupported_type": {
        "en": "cast: unsupported type «{type}».",
        "it": "cast: tipo non supportato «{type}».",
        "de": "cast: nicht unterstützter Typ «{type}».",
        "es": "cast: tipo no soportado «{type}».",
        "fr": "cast : type non pris en charge «{type}».",
    },
    "cast_type_unknown": {
        "en": "cast: the type of the column «{column}» is not known at this point of the flow, so the cast cannot be written as Tabularia does it. Cast it from a column of a known type, or use a compute.",
        "it": "cast: il tipo della colonna «{column}» non è noto a questo punto del flusso, quindi il cast non si può scrivere come lo fa Tabularia. Fai il cast da una colonna di tipo noto, o usa un compute.",
        "de": "cast: der Typ der Spalte «{column}» ist an dieser Stelle des Flows unbekannt, daher kann der Cast nicht so geschrieben werden, wie Tabularia ihn ausführt. Casten Sie von einer Spalte bekannten Typs oder verwenden Sie ein compute.",
        "es": "cast: el tipo de la columna «{column}» no se conoce en este punto del flujo, así que el cast no se puede escribir como lo hace Tabularia. Haz el cast desde una columna de tipo conocido, o usa un compute.",
        "fr": "cast : le type de la colonne «{column}» n'est pas connu à ce point du flux, le cast ne peut donc pas être écrit comme le fait Tabularia. Faites le cast depuis une colonne de type connu, ou utilisez un compute.",
    },
    "filter_value_missing": {
        "en": "The filter on «{column}» has no value for the operator «{operator}» (between: two values). Tabularia refuses it too: complete the node in the flow.",
        "it": "Il filtro su «{column}» non ha un valore per l'operatore «{operator}» (between: due valori). Anche Tabularia lo rifiuta: completa il nodo nel flusso.",
        "de": "Der Filter auf «{column}» hat keinen Wert für den Operator «{operator}» (between: zwei Werte). Auch Tabularia lehnt ihn ab: vervollständigen Sie den Knoten im Flow.",
        "es": "El filtro sobre «{column}» no tiene valor para el operador «{operator}» (between: dos valores). Tabularia también lo rechaza: completa el nodo en el flujo.",
        "fr": "Le filtre sur «{column}» n'a pas de valeur pour l'opérateur «{operator}» (between : deux valeurs). Tabularia le refuse aussi : complétez le nœud dans le flux.",
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
    "step_invalid": {
        # not «Tabularia cannot run it either»: the Polars engine runs a drop of no columns, the exporter does not
        "en": "The step «{step}» of the flow is incomplete or not valid ({detail}): complete it in the editor, or remove it.",
        "it": "Il passo «{step}» del flusso è incompleto o non valido ({detail}): completalo nell'editor, o eliminalo.",
        "de": "Der Schritt «{step}» des Flows ist unvollständig oder ungültig ({detail}): vervollständigen Sie ihn im Editor oder entfernen Sie ihn.",
        "es": "El paso «{step}» del flujo está incompleto o no es válido ({detail}): complétalo en el editor o elimínalo.",
        "fr": "L'étape «{step}» du flux est incomplète ou non valide ({detail}) : complétez-la dans l'éditeur, ou supprimez-la.",
    },
    "op_not_exportable": {
        "en": "The operation «{op}» cannot be exported to dbt.",
        "it": "L'operazione «{op}» non si esporta in dbt.",
        "de": "Die Operation «{op}» lässt sich nicht nach dbt exportieren.",
        "es": "La operación «{op}» no se puede exportar a dbt.",
        "fr": "L'opération «{op}» ne s'exporte pas vers dbt.",
    },
    "contract_rule_invalid": {
        "en": "The contract rule «{rule}» is incomplete, or Tabularia's own check cannot evaluate it (accepted values on a boolean column: true, false, 1 or 0): it cannot become a dbt test.",
        "it": "La regola del contratto «{rule}» è incompleta, o neanche il controllo di Tabularia la può valutare (valori ammessi su una colonna booleana: true, false, 1 o 0): non può diventare un test dbt.",
        "de": "Die Vertragsregel «{rule}» ist unvollständig, oder auch die Prüfung von Tabularia kann sie nicht auswerten (zulässige Werte einer booleschen Spalte: true, false, 1 oder 0): sie kann kein dbt-Test werden.",
        "es": "La regla del contrato «{rule}» está incompleta, o tampoco la comprobación de Tabularia puede evaluarla (valores aceptados en una columna booleana: true, false, 1 o 0): no puede convertirse en un test dbt.",
        "fr": "La règle du contrat «{rule}» est incomplète, ou le contrôle de Tabularia ne peut pas l'évaluer non plus (valeurs acceptées sur une colonne booléenne : true, false, 1 ou 0) : elle ne peut pas devenir un test dbt.",
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
    "model_too_large": {
        "en": "The model «{model}» is {size} bytes of SQL together with the ephemeral models dbt puts inside it: ClickHouse reads at most {max} (max_query_size). Split the flow in two: a flow that reads another flow's output exports it as a model of its own.",
        "it": "Il modello «{model}» è di {size} byte di SQL insieme ai modelli effimeri che dbt ci mette dentro: ClickHouse ne legge al più {max} (max_query_size). Dividi il flusso in due: un flusso che legge l'uscita di un altro la esporta come un modello a sé.",
        "de": "Das Modell «{model}» umfasst zusammen mit den ephemeren Modellen, die dbt darin einsetzt, {size} Byte SQL: ClickHouse liest höchstens {max} (max_query_size). Teilen Sie den Flow in zwei: Ein Flow, der die Ausgabe eines anderen liest, exportiert sie als eigenes Modell.",
        "es": "El modelo «{model}» ocupa {size} bytes de SQL junto con los modelos efímeros que dbt le inserta: ClickHouse lee como máximo {max} (max_query_size). Divide el flujo en dos: un flujo que lee la salida de otro la exporta como un modelo propio.",
        "fr": "Le modèle «{model}» fait {size} octets de SQL avec les modèles éphémères que dbt y insère : ClickHouse en lit au plus {max} (max_query_size). Divisez le flux en deux : un flux qui lit la sortie d'un autre l'exporte comme un modèle à part.",
    },
    "column_name_unsupported": {
        # dbt-clickhouse writes a table's column list as "…" without escaping: a view or an ephemeral model builds
        "en": "The column «{column}» of the model «{model}» has the character {character}, which {adapter} cannot write in a table's column list: rename the column in the flow, or materialize the model as a view.",
        "it": "La colonna «{column}» del modello «{model}» contiene il carattere {character}, che {adapter} non sa scrivere nell'elenco delle colonne di una tabella: rinomina la colonna nel flusso, o materializza il modello come vista.",
        "de": "Die Spalte «{column}» des Modells «{model}» enthält das Zeichen {character}, das {adapter} in der Spaltenliste einer Tabelle nicht schreiben kann: Benennen Sie die Spalte im Flow um, oder materialisieren Sie das Modell als View.",
        "es": "La columna «{column}» del modelo «{model}» contiene el carácter {character}, que {adapter} no sabe escribir en la lista de columnas de una tabla: renombra la columna en el flujo, o materializa el modelo como vista.",
        "fr": "La colonne «{column}» du modèle «{model}» contient le caractère {character}, que {adapter} ne sait pas écrire dans la liste des colonnes d'une table : renommez la colonne dans le flux, ou matérialisez le modèle en vue.",
    },
    "other_clickhouse": {
        "en": "The connection «{connection}» is ANOTHER ClickHouse ({host}): the ClickHouse target reads the Postgres/MySQL sources and the tables of its own server.",
        "it": "La connessione «{connection}» è un ALTRO ClickHouse ({host}): il target ClickHouse legge le sorgenti Postgres/MySQL e le tabelle del suo stesso server.",
        "de": "Die Verbindung «{connection}» ist ein ANDERES ClickHouse ({host}): das ClickHouse-Ziel liest die Postgres/MySQL-Quellen und die Tabellen seines eigenen Servers.",
        "es": "La conexión «{connection}» es OTRO ClickHouse ({host}): el destino ClickHouse lee las fuentes Postgres/MySQL y las tablas de su propio servidor.",
        "fr": "La connexion «{connection}» est un AUTRE ClickHouse ({host}) : la cible ClickHouse lit les sources Postgres/MySQL et les tables de son propre serveur.",
    },
    "clickhouse_cannot_read": {
        "en": "The connection «{connection}» is {db_type}: ClickHouse reads only PostgreSQL, MySQL and MariaDB, and no other dbt target reads {db_type}. The ClickHouse target can still add this flow to an existing dbt project that already declares these tables as a source: choose «Folder for an existing project» and mark the source «Already declared». Otherwise copy the data into a PostgreSQL or MySQL table and have the flow read it from there.",
        "it": "La connessione «{connection}» è {db_type}: ClickHouse legge solo PostgreSQL, MySQL e MariaDB, e nessun altro target dbt legge {db_type}. Il target ClickHouse può comunque aggiungere questo flusso a un progetto dbt esistente che dichiara già queste tabelle come sorgente: scegli «Cartella per un progetto esistente» e segna la sorgente come «Già dichiarata». Altrimenti copia i dati in una tabella PostgreSQL o MySQL e fai leggere al flusso quella.",
        "de": "Die Verbindung «{connection}» ist {db_type}: ClickHouse liest nur PostgreSQL, MySQL und MariaDB, und kein anderes dbt-Ziel liest {db_type}. Das ClickHouse-Ziel kann diesen Flow dennoch einem bestehenden dbt-Projekt hinzufügen, das diese Tabellen bereits als Quelle deklariert: wählen Sie «Ordner für ein bestehendes Projekt» und markieren Sie die Quelle als «Bereits deklariert». Andernfalls kopieren Sie die Daten in eine PostgreSQL- oder MySQL-Tabelle und lassen Sie den Flow sie dort lesen.",
        "es": "La conexión «{connection}» es {db_type}: ClickHouse solo lee PostgreSQL, MySQL y MariaDB, y ningún otro destino dbt lee {db_type}. El destino ClickHouse puede aun así añadir este flujo a un proyecto dbt existente que ya declara estas tablas como fuente: elige «Carpeta para un proyecto existente» y marca la fuente como «Ya declarada». Si no, copia los datos en una tabla PostgreSQL o MySQL y haz que el flujo los lea de allí.",
        "fr": "La connexion «{connection}» est {db_type} : ClickHouse ne lit que PostgreSQL, MySQL et MariaDB, et aucune autre cible dbt ne lit {db_type}. La cible ClickHouse peut tout de même ajouter ce flux à un projet dbt existant qui déclare déjà ces tables comme source : choisissez «Dossier pour un projet existant» et marquez la source comme «Déjà déclarée». Sinon, copiez les données dans une table PostgreSQL ou MySQL et faites-les lire au flux depuis celle-ci.",
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
    "ai_query_too_long": {
        "en": "The model «{model}» does not translate to {dialect} on its own ({detail}), and its SQL is too long to ask the AI for a "
              "translation: {size} characters, at most {max}. Export with another target, or split the flow so that this model has "
              "fewer columns or steps.",
        "it": "Il modello «{model}» non si traduce da solo in {dialect} ({detail}), e il suo SQL è troppo lungo per chiederne la "
              "traduzione all'AI: {size} caratteri, al più {max}. Esporta con un altro target, o dividi il flusso perché questo "
              "modello abbia meno colonne o passi.",
        "de": "Das Modell «{model}» lässt sich nicht von selbst nach {dialect} übersetzen ({detail}), und sein SQL ist zu lang, um "
              "die KI um eine Übersetzung zu bitten: {size} Zeichen, höchstens {max}. Exportieren Sie mit einem anderen Ziel, oder "
              "teilen Sie den Flow so auf, dass dieses Modell weniger Spalten oder Schritte hat.",
        "es": "El modelo «{model}» no se traduce por sí solo a {dialect} ({detail}), y su SQL es demasiado largo para pedir a la IA "
              "una traducción: {size} caracteres, como máximo {max}. Exporta con otro destino, o divide el flujo para que este "
              "modelo tenga menos columnas o pasos.",
        "fr": "Le modèle «{model}» ne se traduit pas tout seul en {dialect} ({detail}), et son SQL est trop long pour demander une "
              "traduction à l'IA : {size} caractères, au plus {max}. Exportez avec une autre cible, ou divisez le flux pour que ce "
              "modèle ait moins de colonnes ou d'étapes.",
    },
    "seed_unreadable": {
        "en": "Seed «{seed}»: the file cannot be read ({detail}).",
        "it": "Seed «{seed}»: il file non si legge ({detail}).",
        "de": "Seed «{seed}»: die Datei kann nicht gelesen werden ({detail}).",
        "es": "Seed «{seed}»: el archivo no se puede leer ({detail}).",
        "fr": "Seed «{seed}» : le fichier ne peut pas être lu ({detail}).",
    },
}


# why a translation the AI proposed does not pass the engine's checks
AI_REASONS: dict[str, dict[str, str]] = {
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
    "function_not_allowed": {"en": "it calls {function}, which reads data or settings outside the model",
                             "it": "chiama {function}, che legge dati o impostazioni fuori dal modello",
                             "de": "sie ruft {function} auf, das Daten oder Einstellungen außerhalb des Modells liest",
                             "es": "llama a {function}, que lee datos o ajustes fuera del modelo",
                             "fr": "elle appelle {function}, qui lit des données ou des réglages hors du modèle"},
    "settings": {"en": "it changes the server's settings (SETTINGS)", "it": "cambia le impostazioni del server (SETTINGS)",
                 "de": "sie ändert die Einstellungen des Servers (SETTINGS)", "es": "cambia los ajustes del servidor (SETTINGS)",
                 "fr": "elle modifie les réglages du serveur (SETTINGS)"},
}


# text_not_translatable: what to do depends on the dialect the text is written in. Only
# ClickHouse text has a target that runs it as it is; DuckDB, BigQuery and Polars text
# goes through the same translation on every target.
DIALECT_ADVICE: dict[str, dict[str, str]] = {
    "clickhouse": {
        "en": "Use the ClickHouse target, which runs ClickHouse text as it is, or rewrite the expression.",
        "it": "Usa il target ClickHouse, che esegue il testo ClickHouse così com'è, o riscrivi l'espressione.",
        "de": "Verwenden Sie das ClickHouse-Ziel, das ClickHouse-Text unverändert ausführt, oder schreiben Sie den Ausdruck um.",
        "es": "Usa el destino ClickHouse, que ejecuta el texto ClickHouse tal cual, o reescribe la expresión.",
        "fr": "Utilisez la cible ClickHouse, qui exécute le texte ClickHouse tel quel, ou réécrivez l'expression.",
    },
    "": {
        "en": "Every target translates it the same way: rewrite the expression with functions common to SQL dialects.",
        "it": "Ogni target lo traduce allo stesso modo: riscrivi l'espressione con funzioni comuni ai dialetti SQL.",
        "de": "Jedes Ziel übersetzt ihn gleich: schreiben Sie den Ausdruck mit Funktionen um, die SQL-Dialekte gemeinsam haben.",
        "es": "Todos los destinos lo traducen igual: reescribe la expresión con funciones comunes a los dialectos SQL.",
        "fr": "Chaque cible le traduit de la même façon : réécrivez l'expression avec des fonctions communes aux dialectes SQL.",
    },
}


def translate(code: str | None, params: dict | None, language: str, original_text: str = "") -> str:
    """The code's sentence in the requested language (or in English); without a code, or with
    a missing parameter, the original text."""
    models = MESSAGES.get(code or "")
    if not models:
        return original_text or code or ""
    p = dict(params or {})
    if code == "internal" and "detail" not in p:
        p["detail"] = original_text
    if code == "ai_translation_invalid" and p.get("reason") in AI_REASONS:
        reason = AI_REASONS[p["reason"]]
        try:
            p["reason"] = (reason.get(language) or reason[DEFAULT]).format(**p)
        except (KeyError, IndexError, ValueError):
            p["reason"] = reason[DEFAULT]
    try:
        sentence = (models.get(language) or models[DEFAULT]).format(**p)
    except (KeyError, IndexError, ValueError):
        return original_text or models[DEFAULT]
    if code == "text_not_translatable":
        advice = DIALECT_ADVICE.get(str(p.get("dialect") or "").lower()) or DIALECT_ADVICE[""]
        sentence += " " + (advice.get(language) or advice[DEFAULT])
    return sentence


def error_message(e: Exception, language: str) -> str:
    """A (gateway's) `DbtExportError` in the requested language."""
    return translate(getattr(e, "code", None), getattr(e, "params", None), language, str(e))


def engine_message(detail, language: str) -> str:
    """The `detail` of an engine 422: {code, params, message} or, from an old engine, a
    text."""
    if isinstance(detail, dict):
        return translate(detail.get("code"), detail.get("params"), language, str(detail.get("message") or ""))
    return str(detail)

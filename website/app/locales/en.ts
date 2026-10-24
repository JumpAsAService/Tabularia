// Every text of the landing page, in one place.
// Shaped like a vue-i18n message catalogue so @nuxtjs/i18n can take it over later:
// add it/fr/de/es next to this file and switch useCopy() to $t / useI18n().
// Facts come from the README and docs/ only. No customers, testimonials, benchmarks or prices exist:
// none may be added here. Integrations are named for what they are; there is no Airflow plugin.

export default {
  meta: {
    title: 'Tabularia · Messy in. Trusted out.',
    description:
      'Self-hosted, open-source data preparation. Connect any source, build a flow on a canvas, preview every step, run it on five engines with one semantics, write Python and R in governed environments, orchestrate your containers, publish a table people can trust.',
  },

  nav: {
    home: 'Tabularia home',
    how: 'How it works',
    engines: 'Engines',
    code: 'Python, R, containers',
    stack: 'Integrations',
    docs: 'Docs',
    github: 'GitHub',
    getStarted: 'Get started',
  },

  hero: {
    titleA: 'Messy in.',
    titleB1: 'Trusted',
    titleB2: ' out.',
    sentence: 'Open-source data preparation that runs on your own servers: connect your systems, build a flow on a canvas, publish a table people can trust.',
    readDocs: 'Read the docs',
    patternLabel: 'Scattered fragments on the left settle into straight rows on the right as they cross a blue curve.',
    scroll: 'See how data from real systems becomes a table',
    cards: {
      label: 'What Tabularia gives you',
      pause: 'Pause the rotation',
      play: 'Play the rotation',
      keys: 'the up and down arrow keys move between the points; scrolling moves too',
      skip: 'Skip the nine points',
      factLabel: 'In short',
      items: [
        { title: 'Open source. No fees per seat.', fact: 'AGPL-3.0', text: 'AGPL-3.0, self-hosted on your servers. Every user, every flow, no licence count.', link: ['/docs/getting-started', 'Get started'] },
        { title: 'Every source you need.', fact: 'SQL · files · APIs', text: 'Postgres, MySQL, Oracle, SQL Server, ClickHouse, Trino; files; folders on S3 and SFTP; SharePoint; any HTTP API.', link: ['/#connect', 'All the connectors'] },
        { title: 'No vendor lock-in.', fact: 'Parquet · dbt', text: 'AGPL code, parquet in your own bucket, an open REST API, OpenLineage. A flow can also be exported as a dbt project.', link: ['/#open', 'What you keep'] },
        { title: 'Built for big tables.', fact: '25M rows · 0.6 s', text: 'Polars streams, DuckDB spills to disk, DuckDB and chDB read only the columns and row groups a query needs, and ClickHouse or BigQuery read the parquet where it lies. On a managed ClickHouse, an aggregation over 25 million rows answers in 0.6 s.', link: ['/#engines', 'The five engines'] },
        { title: 'Fits your infrastructure.', fact: 'Compose · Helm', text: 'Docker Compose to try it, Helm on Kubernetes for production; your S3, your Postgres, your databases; OIDC sign-in; API tokens and signed webhooks.', link: ['/#stack', 'Integrations'] },
        { title: 'Python, R and your containers, governed.', fact: 'Kubernetes', text: 'Write Python and R in environments an administrator defines, or run the images your team already ships, as steps of the same flows: Kubernetes Jobs with limits, versions, logs and an audit trail.', link: ['/#code', 'Code and containers'] },
        { title: 'Contracts and lineage from day one.', fact: 'OpenLineage', text: 'Rules checked before anything is published, and a lineage map of every flow and datasource, also sent out as OpenLineage.', link: ['/#governance', 'Governance'] },
        { title: 'Make your DPO chill.', fact: 'Audit → SIEM', text: 'An append-only audit log that can go to Splunk, sessions you can revoke, roles and inherited permissions, an audit retention you set and the privacy notice quotes.', link: ['/docs/siem-splunk', 'Audit log to Splunk'] },
        { title: 'AI that answers, builds flows and writes code.', fact: 'Optional', text: 'Ask your data in plain language, let the editor’s assistant build the flow on the canvas, or have it write and fix your Python and R without ever seeing a row. Optional, on the provider you choose.', link: ['/#stack', 'The assistant'] },
      ],
    },
  },

  intro: [
    { strong: true, text: 'Data rarely arrives clean. An export writes decimals with commas, a supplier drops a new file every night, a CRM table carries a column nobody documented.' },
    { text: 'Data and IT leaders want that work on infrastructure they control: their servers, their bucket, their sign-in, an audit trail of who ran what and which data was downloaded.' },
    { text: 'Analysts want to shape the data by clicking and see the result at every step. Data engineers want SQL, an API and nothing they cannot take with them.' },
    { text: 'Tabularia is built for all three, and it is AGPL-3.0 open source.' },
  ],
  introLink: 'What it is, in the README',

  story: {
    title: 'How it works',
    steps: [
      { stage: 'Connect', title: 'Last night’s file, over SFTP.', text: 'The datasource reads /in/sales_{yesterday:yyyyMMdd}.csv, and this morning that is sales_20261017.csv. Every value arrives as text.' },
      { stage: 'Shape', title: 'Turn text into numbers.', text: 'A computed column parses «1.234,50 €» with an SQL expression in the engine’s dialect (here Polars: TRY_CAST). «n/a» becomes NULL, not an error.' },
      { stage: 'Check', title: 'Cast the dates, see what breaks.', text: 'Change type reads the ISO dates. «17/10/2026» is not one, so it becomes NULL, there in the preview.' },
      { stage: 'Connect', title: 'The web shop’s orders, from S3.', text: 'One parquet file a day in the shop’s bucket, already typed: web/orders_{yesterday:yyyyMMdd}.parquet.' },
      { stage: 'Shape', title: 'One table for both channels.', text: 'Union (append rows) stacks the store’s orders and the web shop’s.' },
      { stage: 'Connect', title: 'Customers from the ERP.', text: 'A Join with the customers table of the company’s Postgres adds the name and the city.' },
      { stage: 'Check', title: 'Keep only what holds.', text: 'Drop nulls removes the rows without a date or an amount, and the data contract checks the rest.' },
      { stage: 'Publish', title: 'Publish it, every night.', text: 'A datasource for the reports, a table back in Postgres, a file on SFTP or S3, an email. On a schedule.' },
    ],
    rowsLabel: 'rows',
    colsLabel: 'columns',
    contract: 'Contract OK',
    publishedTo: 'Published to',
    destinations: ['Datasource · Daily sales', 'Postgres · sales.daily (append)', 'Email · Daily sales {yesterday:dd/MM/yyyy}'],
    sampleNote: 'Sample data, shown as the editor’s preview shows it.',
    controlsLabel: 'Nodes of the example flow',
  },

  editor: {
    alt: 'The Tabularia flow editor: a chain of nodes on the canvas (change type, rename, computed column, join, group by, sort, limit rows) with a second branch joining in.',
    caption: 'The editor, with a flow from the sample data: two sources, a join, a group by. Every node carries its parameters; click one to see its output.',
  },

  engines: {
    title: 'One flow, five engines',
    lead: 'A flow carries two engines: one for the editor, one for production. The flow does not change by a single node.',
    detail: 'Design on Polars, schedule on ClickHouse or BigQuery — on the whole table, with an optional development sample where reads are billed; every run records the engine it ran on.',
    rules:
      'The engines follow one data standard (nulls, casts, joins, aggregates, sort order, pivots), checked by an oracle test suite that runs the same expectations on all five. The differences that remain are documented, not discovered in production.',
    rulesLink: 'The documented engine differences',
    panelTitle: 'Flow settings',
    devLabel: 'Development engine',
    prodLabel: 'Production engine',
    whereLabel: 'Runs',
    needsLabel: 'Needs',
    billedLabel: 'Billed by',
    rows: [
      { id: 'polars', name: 'Polars', where: 'In the worker process, streaming', needs: 'Nothing', billed: 'Your servers', status: 'default' },
      { id: 'duckdb', name: 'DuckDB', where: 'In the worker, spilling to disk', needs: 'Nothing', billed: 'Your servers', status: '' },
      { id: 'chdb', name: 'chDB', where: 'Embedded ClickHouse in the worker', needs: 'The chDB build of the image', billed: 'Your servers', status: 'optional' },
      { id: 'clickhouse', name: 'ClickHouse (external)', where: 'On your ClickHouse; with the s3 transport it reads the parquet in place', needs: 'A ClickHouse the workers can reach', billed: 'The server you run', status: 'optional' },
      { id: 'bigquery', name: 'BigQuery', where: 'Serverless, temporary external tables on GCS', needs: 'A GCP project and a GCS bucket', billed: 'Bytes read, capped per job', status: 'optional' },
    ],
  },

  code: {
    title: 'Your code and your containers, governed',
    lead: 'When a click is not enough, the data team writes Python or R, or runs the image it already ships: inside the same flows, with the same permissions, schedules and audit trail. On Kubernetes, as Jobs you can account for.',
    rows: {
      python: {
        name: 'Write R and Python in a governed environment',
        line: 'Administrators define the environments: the language version, the packages pinned, the groups that may use them, the variables and the secrets. Tabularia builds the image and runs every script as a Kubernetes Job in its own namespace, with CPU and memory limits, network policies and a timeout. Scripts are saved in folders with their versions; Python is checked by Ruff as you type; every run keeps its log.',
      },
      assistant: {
        name: 'An assistant that never sees your data',
        line: 'Beside the code, it writes and fixes Python and R for the script’s environment. It gets the code, the packages, the names and types of the columns and the error of a failed run: never a row. What it proposes is applied only when you say so.',
      },
      containers: {
        name: 'Orchestrate your containers',
        line: 'Any image of your registries (Harbor, GitHub, GitLab, Docker Hub, your cloud’s), from the repositories an administrator allows to each group. Set the command, the arguments, the variables, CPU and memory: the container gets its input as parquet and hands back its output, or simply does its job elsewhere. Retries, timeouts, logs and the outcome come with it.',
      },
      groups: {
        name: 'One schedule for all of it',
        line: 'A flow group runs steps in order and cells side by side: refresh the sources, run the flows, a script and a container, then email the results as Excel files. A failed step stops the ones after it, and every cell checks its rights again when it runs.',
      },
    },
    assistantNote: 'Columns and the error go to the model. Rows never do.',
    docsLink: 'Python and R in flows',
    containersLink: 'The Container node',
  },

  open: {
    title: 'Nothing you cannot take with you',
    lead: 'No proprietary store: what Tabularia keeps is in formats and interfaces you already use. (The bundled Grafana keeps its own default analytics and update checks unless you turn them off.)',
    items: [
      { name: 'Open source', text: 'AGPL-3.0, the code and the issues on GitHub, no per-seat fees.' },
      { name: 'Self-hosted', text: 'Docker Compose to try it, Helm on Kubernetes for production, with your own Postgres, S3 and Valkey if you prefer.' },
      { name: 'Parquet in your bucket', text: 'Every datasource and result is a parquet file on your object storage: MinIO, S3, Scaleway or Google Cloud Storage.' },
      { name: 'An open API', text: 'REST, with read-only or read-write tokens that expire and never exceed their account’s permissions.' },
      { name: 'OpenLineage', text: 'Runs, inputs and outputs reported to the catalogue you choose, in the open standard.' },
      { name: 'A dbt project, if you want one', text: 'A flow can be exported as a dbt project.', link: 'What the export covers, and its limits' },
    ],
  },

  stack: {
    title: 'Fits the stack you run',
    lead: 'Named for what they are: the systems Tabularia talks to, and the standard each one speaks.',
    items: [
      { name: 'Orchestrators and CI', text: 'The REST API, API tokens and service accounts let any orchestrator (Airflow, a CI job, a scheduler) start a flow and read its results; signed webhooks tell it when the run ends.' },
      { name: 'Data catalogues', text: 'OpenLineage events to Marquez, DataHub or any collector: names, schemas, queries and paths, never rows.' },
      { name: 'SIEM', text: 'Every audit entry forwarded to Splunk’s HTTP Event Collector, nothing skipped while Splunk is down.' },
      { name: 'Single sign-on', text: 'Any OIDC provider (Keycloak, Microsoft Entra ID, Okta, Auth0…), verified against Keycloak; identity-provider groups map onto Tabularia groups by name.' },
      { name: 'Monitoring', text: 'VictoriaMetrics and Grafana dashboards: task durations, cache hit rate, storage growth, preview latency.' },
      { name: 'An AI assistant, optional', text: 'Any OpenAI-compatible endpoint, including one you host; an administrator decides which models may be used.' },
    ],
    pictureAlt: 'A Tabularia flow in Marquez: the datasources it read on the left, the flow job in the middle, the datasource it published on the right, each with its columns.',
    pictureCaption: 'One flow run, as Marquez receives it through OpenLineage.',
  },

  governance: {
    title: 'Governed from the first run',
    lead: 'Sessions that can be closed, secrets encrypted at rest, and about a hundred settings an administrator changes from the panel, most of them without a restart.',
    cells: {
      contract: { name: 'Data contracts', line: 'Columns, types and rules checked before publishing. An error blocks the publish; the last good data keeps serving.' },
      sso: { name: 'Single sign-on', line: 'Any OIDC provider (Keycloak, Entra ID, Okta, Auth0…), verified against Keycloak; local sign-in stays as break-glass.' },
      rbac: { name: 'Inherited permissions', line: 'Users, groups and nested projects with view, edit, manage and connect.' },
      audit: { name: 'An audit log that answers', line: 'Who signed in, who ran what, which data was downloaded. Append-only, and it can be forwarded to Splunk.' },
      sql: { name: 'Free-form SQL, contained', line: 'A SQL node reads its own input. On ClickHouse, chDB and BigQuery the query is parsed and any other table is refused; DuckDB and Polars run it in a sandbox that sees only the input.' },
      sample: { name: 'The whole table, by default', line: 'Previews and runs read the full table: sampling is off unless you turn it on. A development sample is optional, for sources where every read is billed; scheduled runs always read everything.' },
    },
    sampleAlt: 'The Development sample setting of a source node at its default, “off (all rows)”: previews read the whole table. The note under it says a sample is only for speeding up previews, and that scheduled runs and Run in production always read everything.',
  },

  connect: {
    titleA: 'Any source.',
    titleB: 'Every day’s file.',
    button: 'The placeholder language',
    sources:
      'Postgres, MySQL, MariaDB, ClickHouse, Trino, Oracle, Microsoft SQL Server (its driver is added by you), SharePoint (Excel and CSV); uploaded files (CSV, Excel, JSON, parquet) and folders on SFTP or S3 (CSV, Excel, NDJSON, parquet); HTTP APIs with page-number, offset-and-limit, cursor or next-link pagination, and API keys, bearer tokens, basic auth or OAuth2 client credentials. Results go to a datasource, a database table, SFTP, S3 or email, and a flow group can email several datasources as Excel files.',
    datasourcesAlt: 'The Datasources page: database tables from the sample company with their folders and row counts, two marked AI ready.',
    tryLabel: 'Try a name',
    tryHint: 'Edit it as a datasource’s file name: {day} and {day_end} (the range), {symbol} (a list), {today}, {now-2h}, {last_refresh}, formats such as {day:ddMMyyyy} or {now:%Y%m%d}. It is checked and resolved with the product’s rules and messages, for the selected day and the time zone you pick.',
    zoneLabel: 'Time zone',
    daysLabel: 'The seven days before today',
    rangeNote: 'day = today-7 .. today step 1d',
    bands: [
      { time: '06:00', kind: 'SFTP folder', role: 'Input', template: '/in/{day:yyyy}/{day:MM}/sales_{day:ddMMyyyy}.csv' },
      { time: '06:05', kind: 'HTTP API', role: 'Input', template: 'GET /quotes/{symbol}?from={day}&to={day_end}' },
      { time: '06:10', kind: 'S3 folder', role: 'Input', template: 'exports/{day:yyyy/MM/dd}/*.parquet' },
      { time: '06:40', kind: 'SFTP server', role: 'Output', template: '/out/{today:yyyy}/{today:MM}/report_{today:yyyyMMdd}_{run_id}.csv' },
      { time: '07:00', kind: 'Email', role: 'Output', template: 'Daily sales {yesterday:dd/MM/yyyy}' },
    ],
    sampleNote: 'Sample schedule times; {symbol} is a list variable (AAPL, MSFT) and {run_id} the run’s number.',
    scheduleTitle: 'Then put it on a schedule.',
    scheduleText: 'Time-zone aware and DST-safe. The schedule load shows busy bands and collisions before they bite; flow groups chain refreshes, flows, scripts, containers and emails in steps.',
    scheduleAlt: 'The schedule load of the Flows page: a week of time bands with the scheduled runs, and the note that no minute exceeds the worker capacity.',
  },

  install: {
    titleA: 'Run it on',
    titleB: 'your servers.',
    lines: 'Docker Compose to try it on one machine, Helm on Kubernetes for production. Images for amd64 and arm64 on Docker Hub; the latest release on GitHub.',
    command: 'git clone https://github.com/JumpAsAService/Tabularia.git\ncd Tabularia/infrastructure\ndocker compose up -d\n# then open http://localhost:3000 and create the administrator',
    commandLabel: 'Commands to start the try-it stack',
    primary: 'Get started',
    secondary: 'Releases on GitHub',
    config: 'Most settings are environment variables:',
    configLink: 'the configuration reference',
  },

  footer: {
    groups: [
      { title: 'Product', links: [['How it works', '/#how'], ['Engines', '/#engines'], ['Python, R, containers', '/#code'], ['Integrations', '/#stack'], ['Install', '/#install']] },
      { title: 'Documentation', links: [['Getting started', '/docs/getting-started'], ['Kubernetes and Helm', '/docs/deploy/kubernetes'], ['Python and R', '/docs/scripting'], ['Containers', '/docs/container-node'], ['Configuration reference', '/docs/configuration'], ['API and webhooks', '/docs/api'], ['Export to dbt', '/docs/dbt-export']] },
      { title: 'Project', links: [['Source on GitHub', 'repo'], ['Releases', 'releases'], ['Issues', 'issues'], ['Images on Docker Hub', 'dockerhub']] },
    ],
    contactTitle: 'Contact',
    contact: 'Questions and bugs: GitHub issues. Commercial licensing: contact the author through GitHub.',
    copyright: '© 2025–2026 Leonardo Trivelli. Licensed under the AGPL-3.0.',
    trademarks:
      'BigQuery and Google Cloud are trademarks of Google LLC; ClickHouse is a trademark of ClickHouse, Inc. Other names are trademarks of their owners, named only to say what Tabularia works with.',
    top: 'Back to top',
    warranty: 'Provided “as is”, without warranty; the authors are not liable for any damage, including loss of data (AGPL-3.0 §15–16). Keep backups.',
    licence: 'Read the licence',
    name: 'Tabularia',
    origin: 'Named after the Tabularium, the records office of ancient Rome, where the state’s tables were kept in order.',
  },
}

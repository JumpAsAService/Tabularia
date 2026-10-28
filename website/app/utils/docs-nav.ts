// The documentation sidebar: sections and order, keyed by route.
// A doc that exists but is not listed here still gets published: it lands in "Reference",
// so a new file in docs/ is never lost. A route listed here that does not exist at build
// time (a doc not committed yet) is simply not shown.

export type DocsIcon = 'start' | 'capabilities' | 'sources' | 'connections' | 'integrations' | 'governance' | 'operate' | 'reference'

export interface DocsNavItem { path: string, title: string }
export interface DocsNavGroup { title: string, icon: DocsIcon, intro: string, items: DocsNavItem[] }

export const DOCS_GROUPS: { title: string, icon: DocsIcon, intro: string, items: [string, string][] }[] = [
  {
    title: 'Getting started',
    icon: 'start',
    intro: 'Install it on one machine, learn the few ideas everything else is built on, and run a first flow.',
    items: [
      ['/docs/getting-started', 'Getting started'],
      ['/docs/getting-started-docker-compose', 'Walkthrough with Docker Compose'],
      ['/docs/deploy/minikube', 'Try it on your computer (minikube)'],
      ['/docs/concepts', 'Concepts'],
      ['/docs/overview', 'Overview (README)'],
    ],
  },
  {
    title: 'Capabilities',
    icon: 'capabilities',
    intro: 'What you can build: visual flows, analysis on any step, AI questions, pixel-perfect reports, Python, R and SQL scripts, containers and the orchestration that ties them together.',
    items: [
      ['/docs/capabilities', 'All capabilities'],
      ['/docs/capabilities/flows', 'Low-code flows'],
      ['/docs/capabilities/preview-and-analysis', 'Preview and analysis'],
      ['/docs/capabilities/ask-your-data', 'Ask your data with AI'],
      ['/docs/capabilities/reports', 'Pixel-perfect reports'],
      ['/docs/capabilities/report-functions', 'Report functions'],
      ['/docs/scripting', 'Python and R'],
      ['/docs/sql-scripts', 'SQL scripts'],
      ['/docs/capabilities/notebooks', 'marimo notebooks as apps'],
      ['/docs/container-node', 'Docker images in flows'],
      ['/docs/capabilities/flow-groups', 'Flow groups: orchestration'],
      ['/docs/capabilities/scheduling', 'Scheduling'],
      ['/docs/pushmail', 'Pushmail: datasources and reports by email'],
      ['/docs/placeholders', 'Dynamic dates'],
      ['/docs/capabilities/engines', 'Engines'],
      ['/docs/engines/engine-differences', 'Engine differences'],
    ],
  },
  {
    title: 'Sources',
    icon: 'sources',
    intro: 'Every way data comes in: files, databases, buckets, SFTP folders, HTTP APIs, SharePoint, Google Sheets, and the results of your own flows.',
    items: [
      ['/docs/sources', 'All sources'],
      ['/docs/sources/files', 'Uploaded files'],
      ['/docs/sources/databases', 'Database tables and queries'],
      ['/docs/s3-datasource', 'S3 buckets'],
      ['/docs/sftp', 'SFTP folders'],
      ['/docs/api-datasource', 'HTTP APIs'],
      ['/docs/sources/sharepoint', 'Excel on SharePoint'],
      ['/docs/sources/google-sheets', 'Google Sheets'],
      ['/docs/sources/flow-outputs', 'Results of flows and scripts'],
    ],
  },
  {
    title: 'Connections',
    icon: 'connections',
    intro: 'The systems Tabularia reads from and writes to, how each connection is set up, and who may use it.',
    items: [
      ['/docs/connections', 'All connections'],
      ['/docs/connections/databases', 'Databases'],
      ['/docs/sqlserver', 'Microsoft SQL Server driver'],
      ['/docs/connections/s3', 'S3 and compatible storage'],
      ['/docs/sftp', 'SFTP servers'],
      ['/docs/api-datasource', 'HTTP APIs'],
      ['/docs/sources/sharepoint', 'SharePoint'],
      ['/docs/sources/google-sheets', 'Google Sheets'],
      ['/docs/connections/smtp', 'SMTP email'],
      ['/docs/container-registries', 'Container registries'],
      ['/docs/connections/destinations', 'Writing data out'],
    ],
  },
  {
    title: 'Integrations',
    icon: 'integrations',
    intro: 'How Tabularia fits your stack: AI providers, the REST API and webhooks, dbt, OpenLineage, single sign-on, SIEM and monitoring.',
    items: [
      ['/docs/integrations', 'All integrations'],
      ['/docs/integrations/ai', 'AI: where it is used'],
      ['/docs/api', 'API, tokens and webhooks'],
      ['/docs/dbt-export', 'Export to dbt'],
      ['/docs/openlineage', 'OpenLineage'],
      ['/docs/sso-keycloak', 'Single sign-on with Keycloak'],
      ['/docs/siem-splunk', 'Audit log to Splunk'],
      ['/docs/integrations/monitoring', 'Monitoring'],
    ],
  },
  {
    title: 'Security & governance',
    icon: 'governance',
    intro: 'Who may see and do what, what is recorded, and how data quality and personal data are kept in check.',
    items: [
      ['/docs/governance', 'Overview'],
      ['/docs/governance/permissions', 'Permissions'],
      ['/docs/governance/audit-log', 'Audit log'],
      ['/docs/governance/data-contracts', 'Data contracts'],
      ['/docs/design/sso-group-mapping', 'SSO and group mapping'],
      ['/docs/examples/keycloak', 'Keycloak example on a laptop'],
      ['/docs/engines/clickhouse-ai-user', 'A ClickHouse user for the AI assistant'],
    ],
  },
  {
    title: 'Install & operate',
    icon: 'operate',
    intro: 'Run it in production on Kubernetes, configure it, upgrade it and keep it healthy.',
    items: [
      ['/docs/deploy/kubernetes', 'Kubernetes and Helm'],
      ['/docs/deploy/kind', 'A company-like cluster on one computer (kind)'],
      ['/docs/deploy/upgrading', 'Upgrading, backups and restore'],
      ['/docs/deploy/release-checklist', 'Install and upgrade checklist'],
      ['/docs/configuration', 'Configuration reference'],
      ['/docs/engine-settings', 'Settings from the panel'],
      ['/docs/database-migrations', 'Database migrations'],
    ],
  },
  {
    title: 'Reference',
    icon: 'reference',
    intro: 'Release process and the remaining reference pages.',
    items: [
      ['/docs/release-process', 'Release process'],
    ],
  },
]

/** Builds the sidebar from the routes that really exist. */
export function buildDocsNav (existing: { path: string, title?: string }[]): DocsNavGroup[] {
  const byPath = new Map(existing.map(d => [d.path, d]))
  const listed = new Set<string>()
  const groups: DocsNavGroup[] = DOCS_GROUPS.map(g => ({
    title: g.title,
    icon: g.icon,
    intro: g.intro,
    items: g.items.filter(([p]) => byPath.has(p)).map(([p, t]) => { listed.add(p); return { path: p, title: t } }),
  }))
  const rest = existing.filter(d => !listed.has(d.path)).sort((a, b) => a.path.localeCompare(b.path))
  const ref = groups.find(g => g.title === 'Reference')!
  for (const d of rest) ref.items.push({ path: d.path, title: d.title || d.path })
  return groups.filter(g => g.items.length)
}

export function flattenNav (groups: DocsNavGroup[]): DocsNavItem[] {
  return groups.flatMap(g => g.items)
}

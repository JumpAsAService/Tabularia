// The documentation sidebar: groups and order, keyed by route.
// A doc that exists but is not listed here still gets published: it lands in "Reference",
// so a new file in docs/ is never lost. A route listed here that does not exist at build
// time (a doc not committed yet) is simply not shown.

export interface DocsNavItem { path: string, title: string }
export interface DocsNavGroup { title: string, items: DocsNavItem[] }

export const DOCS_GROUPS: { title: string, items: [string, string][] }[] = [
  {
    title: 'Getting started',
    items: [
      ['/docs/getting-started-docker-compose', 'Walkthrough with Docker Compose'],
      ['/docs/deploy/minikube', 'Try it on your computer (minikube)'],
      ['/docs/getting-started', 'Getting started'],
      ['/docs/overview', 'Overview (README)'],
    ],
  },
  {
    title: 'Install & operate',
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
    title: 'Connect data',
    items: [
      ['/docs/sftp', 'SFTP'],
      ['/docs/s3-datasource', 'S3 buckets'],
      ['/docs/api-datasource', 'HTTP APIs'],
      ['/docs/sqlserver', 'Microsoft SQL Server'],
    ],
  },
  {
    title: 'Build flows',
    items: [
      ['/docs/placeholders', 'Dynamic dates in inputs and outputs'],
      ['/docs/engines/engine-differences', 'Engine differences'],
    ],
  },
  {
    title: 'Integrate',
    items: [
      ['/docs/api', 'API, tokens and webhooks'],
      ['/docs/dbt-export', 'Export to dbt'],
      ['/docs/openlineage', 'OpenLineage'],
    ],
  },
  {
    title: 'Security & governance',
    items: [
      ['/docs/sso-keycloak', 'Single sign-on with Keycloak'],
      ['/docs/design/sso-group-mapping', 'SSO and group mapping'],
      ['/docs/examples/keycloak', 'Keycloak example on a laptop'],
      ['/docs/siem-splunk', 'Audit log to Splunk'],
      ['/docs/scripting', 'Python and R in flows'],
      ['/docs/container-node', 'The Container node'],
      ['/docs/container-registries', 'Container registries'],
      ['/docs/engines/clickhouse-ai-user', 'A ClickHouse user for the AI assistant'],
    ],
  },
  {
    title: 'Reference',
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

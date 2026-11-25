// One generic icon and one colour for every kind of connection, so a list of connections
// reads at a glance. Plain lucide pictograms (never a product's logo) and a colour token per
// kind (`--conn-<type>` in assets/main.css, defined in each of the five themes with at least
// 3:1 against the surfaces). The icon is decoration: the kind's name is always written beside it.
// `icon` is the lucide export's name; components/ui/ConnIcon.vue maps it to the component.
export interface ConnIconSpec {
  icon: string
  color: string
}

export const CONN_ICONS: Record<string, ConnIconSpec> = {
  postgresql: { icon: 'Database', color: 'var(--conn-postgresql)' },
  mysql: { icon: 'Database', color: 'var(--conn-mysql)' },
  mariadb: { icon: 'Database', color: 'var(--conn-mariadb)' },
  clickhouse: { icon: 'Columns3', color: 'var(--conn-clickhouse)' },
  trino: { icon: 'Waypoints', color: 'var(--conn-trino)' },
  oracle: { icon: 'Cylinder', color: 'var(--conn-oracle)' },
  sqlserver: { icon: 'Server', color: 'var(--conn-sqlserver)' },
  s3: { icon: 'Archive', color: 'var(--conn-s3)' },
  smtp: { icon: 'Mail', color: 'var(--conn-smtp)' },
  sharepoint: { icon: 'FolderOpen', color: 'var(--conn-sharepoint)' },
  sftp: { icon: 'FolderLock', color: 'var(--conn-sftp)' },
  api: { icon: 'Braces', color: 'var(--conn-api)' },
  gsheets: { icon: 'Sheet', color: 'var(--conn-gsheets)' },
  mlflow: { icon: 'FlaskConical', color: 'var(--conn-mlflow)' },
}

// a kind this build does not know: a neutral plug, in the muted text colour
export const CONN_ICON_FALLBACK: ConnIconSpec = { icon: 'Plug', color: 'var(--muted)' }

export const connIcon = (type: string | null | undefined): ConnIconSpec =>
  (type && CONN_ICONS[type]) || CONN_ICON_FALLBACK

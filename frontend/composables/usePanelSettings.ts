// Administration → Settings: every setting an administrator may change without touching
// the deployment (docs/specs/panel-settings.md). The gateway's come from
// /admin/gateway-settings, the engine's from /admin/engine-settings; the ones read only
// when a process starts carry `rollout` (the components to restart), and
// /admin/settings-rollout says which components still run an old value.
import type { EngineSetting, EngineSettings } from '~/composables/useEngineSettings'

export type SettingKind = 'int' | 'float' | 'bool' | 'str' | 'timezone' | 'cpu' | 'memory'
export type SettingSource = 'gateway' | 'engine'
export type SettingSection = 'gateway' | 'kubernetes' | 'engine'

export interface GatewaySetting {
  key: string
  group: string
  kind: SettingKind
  unit: string
  min: number | null
  max: number | null
  max_length: number | null
  rollout: string[]
  env_var: string
  env_value: unknown
  value: unknown
  applied: unknown
  started_with: unknown
  updated_at: string | null
  updated_by: string | null
}

export interface GatewaySettings {
  items: GatewaySetting[]
  editable: boolean
  panel_enabled: boolean
  refresh_seconds: number
}

// one shape for both: the panel draws them the same way
export interface PanelSetting {
  source: SettingSource
  key: string
  group: string
  kind: SettingKind
  unit: string
  min: number | null
  max: number | null
  maxLength: number | null
  rollout: string[]
  envVar: string
  envValue: unknown
  value: unknown // from the panel; null = the environment's applies
  applied: unknown
  // the gateway's settings read at start: what the process that answered started with
  startedWith?: unknown
  updatedAt: string | null
  updatedBy: string | null
  off: boolean // a feature this installation does not have (the engine's ClickHouse, BigQuery…)
}

export interface SettingError {
  key: string
  code: string
  limit?: number
  other?: string
  message?: string
}

export type RolloutMode = 'kubernetes' | 'manual' | 'compose'

export interface RolloutComponent {
  component: string
  changed_at: string | null
  requested_at: string | null
  requested_by: string | null
  pending: boolean
  compose_service: string
  pods: number | null
  ready: number | null
  old: number | null
  waiting_for: string[]
  error: string | null
}

export interface RolloutState {
  mode: RolloutMode
  components: RolloutComponent[]
  editable: boolean
  namespace: string // for the command shown when the panel cannot restart the pods itself
  release: string
}

// the boxes, in the order of the page; the engine's categories are its groups
export const GROUPS: Record<SettingSection, string[]> = {
  gateway: ['signin', 'sso', 'orchestration', 'history', 'webhooks', 'ai', 'dbt', 'previews', 'backups',
    'monitoring', 'openlineage', 'processes'],
  kubernetes: ['kubernetes', 'scripts', 'builds', 'containers'],
  engine: ['execution', 'cache', 'embedded', 'engine_api', 'import', 'export', 'sftp', 's3', 'api',
    'placeholders', 'clickhouse', 'bigquery'],
}

export const sectionOf = (s: PanelSetting): SettingSection =>
  s.source === 'engine' ? 'engine' : GROUPS.kubernetes.includes(s.group) ? 'kubernetes' : 'gateway'

export function fromGateway(v: GatewaySetting): PanelSetting {
  return {
    source: 'gateway', key: v.key, group: v.group, kind: v.kind, unit: v.unit, min: v.min, max: v.max,
    maxLength: v.max_length, rollout: v.rollout, envVar: v.env_var, envValue: v.env_value, value: v.value,
    applied: v.applied, startedWith: v.started_with, updatedAt: v.updated_at, updatedBy: v.updated_by, off: false,
  }
}

export function fromEngine(v: EngineSetting, unavailable: string[]): PanelSetting {
  const e = v as EngineSetting & { kind?: SettingKind, rollout?: string[] }
  return {
    source: 'engine', key: v.key, group: v.category, kind: e.kind ?? 'int', unit: v.unit, min: v.min, max: v.max,
    maxLength: null, rollout: e.rollout ?? [], envVar: v.env_var, envValue: v.env_value, value: v.value,
    applied: v.applied, updatedAt: v.updated_at, updatedBy: v.updated_by,
    off: unavailable.includes(v.category),
  }
}

export function usePanelSettings() {
  const { apiFetch } = useApiClient()
  return {
    gateway: () => apiFetch<GatewaySettings>('/admin/gateway-settings'),
    engine: () => apiFetch<EngineSettings>('/admin/engine-settings'),
    saveGateway: (values: Record<string, unknown>) =>
      apiFetch<GatewaySettings>('/admin/gateway-settings', { method: 'PUT', body: { values } }),
    saveEngine: (values: Record<string, unknown>) =>
      apiFetch<EngineSettings>('/admin/engine-settings', { method: 'PUT', body: { values } }),
    rollout: () => apiFetch<RolloutState>('/admin/settings-rollout'),
    startRollout: (components: string[]) =>
      apiFetch<RolloutState>('/admin/settings-rollout', { method: 'POST', body: { components } }),
    cancelRollout: (component: string) =>
      apiFetch<RolloutState>(`/admin/settings-rollout/${encodeURIComponent(component)}`, { method: 'DELETE' }),
  }
}

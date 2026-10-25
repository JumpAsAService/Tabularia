// Engine settings (Administration → Settings): the parameters the engine reads when
// needed, changed from the panel without restarts. The engine says which they are and
// with what limits; the gateway keeps them and has them applied.

export type SettingUnit = string // rows | seconds | bytes | threads | processes | files | …
export type SettingCategory = 'import' | 'export' | 'execution' | 'cache' | 'clickhouse' | 'bigquery'
  | 'embedded' | 'engine_api' | 'sftp' | 's3' | 'api' | 'placeholders'

export interface EngineSetting {
  key: string // "<section>.<field>", e.g. "ingest.parquet_row_group_rows"
  category: SettingCategory | 'workers' // workers: shown in Admin → Workers
  unit: SettingUnit
  kind?: 'int' | 'float' | 'bool'
  rollout?: string[] // the components to restart for it to apply; empty = live
  min: number
  max: number
  env_var: string // the environment variable: the starting value
  env_value: number
  value: number | null // from the panel; null = the environment holds (a decimal or true/false for some)
  applied: number | null // the one the engine is applying now
  updated_at: string | null
  updated_by: string | null
}

export interface EngineSettings {
  items: EngineSetting[]
  editable: boolean
  panel_enabled: boolean // ENGINE__SETTINGS_PANEL
  unavailable: SettingCategory[] // engines this installation does not have
  refresh_seconds: number
}

// an engine refusal: a code, the panel writes the sentence in the viewer's language
export interface SettingError {
  key: string
  code: 'unknown' | 'integer' | 'min' | 'max' | 'below' | 'above'
  limit?: number
  other?: string
}

export const SETTING_CATEGORIES: SettingCategory[] = ['import', 'export', 'execution', 'cache', 'clickhouse', 'bigquery',
  'embedded', 'engine_api', 'sftp', 's3', 'api', 'placeholders']
// the processes per worker are engine settings with a section of their own (Admin → Workers)
export const isSettingsCategory = (c: string): c is SettingCategory => (SETTING_CATEGORIES as string[]).includes(c)

// the key in the translations: vue-i18n reads the dots as a path
export const settingI18nKey = (key: string) => key.replace(/\./g, '_')

export const GIB = 1024 ** 3

export function useEngineSettings() {
  const { apiFetch } = useApiClient()
  return {
    get: () => apiFetch<EngineSettings>('/admin/engine-settings'),
    // only those that change: a number sets, null goes back to the environment's value
    save: (values: Record<string, number | null>) =>
      apiFetch<EngineSettings>('/admin/engine-settings', { method: 'PUT', body: { values } }),
  }
}

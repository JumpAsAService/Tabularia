// Engine settings (Administration → Settings): the parameters the engine reads when
// needed, changed from the panel without restarts. The engine says which they are and
// with what limits; the gateway keeps them and has them applied.

export type SettingUnit = string // rows | seconds | bytes | threads | processes | files | …
export type SettingCategory = 'import' | 'export' | 'execution' | 'cache' | 'clickhouse_connection' | 'clickhouse'
  | 'bigquery' | 'embedded' | 'engine_api' | 'sftp' | 's3' | 'api' | 'placeholders'

export interface EngineSetting {
  key: string // "<section>.<field>", e.g. "ingest.parquet_row_group_rows"
  category: SettingCategory | 'workers' // workers: shown in Admin → Workers
  unit: SettingUnit
  kind?: 'int' | 'float' | 'bool' | 'str' | 'choice' | 'secret'
  rollout?: string[] // the components to restart for it to apply; empty = live
  choices?: string[] // choice: the values allowed
  optional?: boolean // str: it may be empty
  format?: string // str: the shape it must have (TEXT_FORMATS)
  min: number
  max: number
  env_var: string // the environment variable: the starting value
  env_value: unknown // a secret: true when set, never its value
  value: unknown // from the panel; null = the environment holds (a number, true/false, a text)
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

export const SETTING_CATEGORIES: SettingCategory[] = ['import', 'export', 'execution', 'cache', 'clickhouse_connection',
  'clickhouse', 'bigquery', 'embedded', 'engine_api', 'sftp', 's3', 'api', 'placeholders']
// the processes per worker are engine settings with a section of their own (Admin → Workers)
export const isSettingsCategory = (c: string): c is SettingCategory => (SETTING_CATEGORIES as string[]).includes(c)

// the key in the translations: vue-i18n reads the dots as a path
export const settingI18nKey = (key: string) => key.replace(/\./g, '_')

export const GIB = 1024 ** 3

// the shapes of the engine's texts, as backend/app/core/panel_settings.py FORMATS checks
// them (the engine checks again): a host without scheme, path or port; a name without
// quoting surprises; an http(s) address; a user name without spaces
export const TEXT_FORMATS: Record<string, RegExp> = {
  host: /^(?=.*[A-Za-z0-9])[A-Za-z0-9.\-:[\]]+$/,
  name: /^[A-Za-z0-9_][A-Za-z0-9_-]*$/,
  url: /^https?:\/\/[^\s/?#]+(\/[^\s?#]*)?$/,
  user: /^[^\s\x00-\x1f\x7f]+$/,
}

export function useEngineSettings() {
  const { apiFetch } = useApiClient()
  return {
    get: () => apiFetch<EngineSettings>('/admin/engine-settings'),
    // only those that change: a number sets, null goes back to the environment's value
    save: (values: Record<string, number | null>) =>
      apiFetch<EngineSettings>('/admin/engine-settings', { method: 'PUT', body: { values } }),
  }
}

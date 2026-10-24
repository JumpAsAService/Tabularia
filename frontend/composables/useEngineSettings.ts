// Impostazioni del motore (Amministrazione → Impostazioni): i parametri che il
// motore legge quando servono, cambiati dal pannello senza riavvii. Quali sono e
// con che limiti lo dice il motore; il gateway li conserva e li fa applicare.

export type SettingUnit = string // rows | seconds | bytes | threads | processes | files | …
export type SettingCategory = 'import' | 'export' | 'execution' | 'cache' | 'clickhouse' | 'bigquery'
  | 'embedded' | 'engine_api' | 'sftp' | 's3' | 'api' | 'placeholders'

export interface EngineSetting {
  key: string // "<sezione>.<campo>", es. "ingest.parquet_row_group_rows"
  category: SettingCategory | 'workers' // workers: shown in Admin → Workers
  unit: SettingUnit
  kind?: 'int' | 'float' | 'bool'
  rollout?: string[] // the components to restart for it to apply; empty = live
  min: number
  max: number
  env_var: string // la variabile d'ambiente: il valore di partenza
  env_value: number
  value: number | null // dal pannello; null = vale l'ambiente (a decimal or true/false for some)
  applied: number | null // quello che il motore sta applicando adesso
  updated_at: string | null
  updated_by: string | null
}

export interface EngineSettings {
  items: EngineSetting[]
  editable: boolean
  panel_enabled: boolean // ENGINE__SETTINGS_PANEL
  unavailable: SettingCategory[] // motori che questa installazione non ha
  refresh_seconds: number
}

// un rifiuto del motore: un codice, la frase la scrive il pannello nella lingua di chi guarda
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

// la chiave nelle traduzioni: vue-i18n legge i punti come un percorso
export const settingI18nKey = (key: string) => key.replace(/\./g, '_')

export const GIB = 1024 ** 3

export function useEngineSettings() {
  const { apiFetch } = useApiClient()
  return {
    get: () => apiFetch<EngineSettings>('/admin/engine-settings'),
    // solo quelle che cambiano: un numero imposta, null torna al valore dell'ambiente
    save: (values: Record<string, number | null>) =>
      apiFetch<EngineSettings>('/admin/engine-settings', { method: 'PUT', body: { values } }),
  }
}

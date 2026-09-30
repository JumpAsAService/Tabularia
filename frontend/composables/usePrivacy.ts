// Informativa sulla privacy: una sola, scritta dall'amministratore.
import { useApiClient } from '~/composables/useApiClient'

export interface PrivacyNotice {
  enabled: boolean
  // con i segnaposto già sostituiti dal server: è ciò che si MOSTRA
  summary: string
  body: string
  // come li ha scritti l'amministratore: è ciò che si MODIFICA
  summary_template: string
  body_template: string
  url: string
  updated_at: string | null
}

export function usePrivacy() {
  const { apiFetch } = useApiClient()

  return {
    get: () => apiFetch<PrivacyNotice>('/privacy-notice'),
    update: (body: Partial<Omit<PrivacyNotice, 'updated_at'>>) =>
      apiFetch<PrivacyNotice>('/privacy-notice', { method: 'PUT', body }),
  }
}

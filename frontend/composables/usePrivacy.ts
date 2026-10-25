// Privacy notice: only one, written by the administrator.
import { useApiClient } from '~/composables/useApiClient'

export interface PrivacyNotice {
  enabled: boolean
  // with the placeholders replaced by the server already: it is what is SHOWN
  summary: string
  body: string
  // as the administrator wrote them: it is what is EDITED
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

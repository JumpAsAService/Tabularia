// Informativa sulla privacy: una sola, scritta dall'amministratore.
import { ref } from 'vue'
import { useApiClient } from '~/composables/useApiClient'

export interface PrivacyNotice {
  enabled: boolean
  summary: string
  body: string
  url: string
  updated_at: string | null
}

// La chiusura si ricorda nel BROWSER e porta con sé la DATA dell'informativa:
// se l'amministratore la riscrive, ricompare a chi l'aveva già chiusa. Averla
// letta una volta non vale per un testo diverso.
const CHIAVE = 'tabularia.privacyRead'
const chiusaPer = ref<string | null>(null)

export function usePrivacy() {
  const { apiFetch } = useApiClient()

  function leggiChiusura() {
    if (!import.meta.client) return
    try {
      chiusaPer.value = localStorage.getItem(CHIAVE)
    } catch {
      chiusaPer.value = null
    }
  }

  function chiudi(versione: string | null) {
    chiusaPer.value = versione ?? 'letta'
    try {
      localStorage.setItem(CHIAVE, chiusaPer.value)
    } catch {
      /* senza storage si ripresenta: è il verso giusto in cui sbagliare */
    }
  }

  return {
    chiusaPer,
    leggiChiusura,
    chiudi,
    get: () => apiFetch<PrivacyNotice>('/privacy-notice'),
    update: (body: Partial<Omit<PrivacyNotice, 'updated_at'>>) =>
      apiFetch<PrivacyNotice>('/privacy-notice', { method: 'PUT', body }),
  }
}

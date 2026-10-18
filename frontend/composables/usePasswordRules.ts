// The rule of a new password, as the gateway applies it (gateway/app/core/passwords.py,
// served by GET /users/password-rules): the forms say what is wrong before sending.
// Until the rule arrives, the same numbers the gateway uses stand in for it.
import { ref } from 'vue'
import { useApiClient } from '~/composables/useApiClient'

export interface PasswordRules {
  min_length: number
  max_bytes: number
  known: string[]
}

const rules = ref<PasswordRules>({ min_length: 12, max_bytes: 72, known: [] })
let loaded = false

/** What is wrong with `password` as a new password, as an i18n key and its
 *  values, or null when it is fine. */
export function passwordProblem(password: string, r: PasswordRules): { key: string; values: Record<string, number> } | null {
  if (r.known.includes(password.trim().toLowerCase())) return { key: 'password.known', values: {} }
  if (password.length < r.min_length) return { key: 'password.tooShort', values: { n: r.min_length } }
  if (new TextEncoder().encode(password).length > r.max_bytes) return { key: 'password.tooLong', values: { n: r.max_bytes } }
  return null
}

export function usePasswordRules() {
  const { apiFetch } = useApiClient()
  async function load() {
    if (loaded) return
    try {
      rules.value = await apiFetch<PasswordRules>('/users/password-rules')
      loaded = true
    } catch { /* the stand-in numbers stay; the gateway still checks */ }
  }
  return { rules, load }
}

import { ref } from 'vue'
import { useApi, type AppInfo } from '~/composables/useApi'
import { setDefaultSampleRows } from '~/composables/useFlowModel'

// Info di deployment lette UNA volta per sessione e condivise (menu
// impostazioni, editor, dialog schedule). Il campione automatico viene
// propagato a useFlowModel così `devSampleOperation` lo applica senza che
// ogni chiamante debba passarlo.
const info = ref<AppInfo | null>(null)
let pending: Promise<AppInfo | null> | null = null

export function useAppInfo() {
  const api = useApi()

  async function load(): Promise<AppInfo | null> {
    if (info.value) return info.value
    if (!pending) {
      pending = api
        .appInfo()
        .then((i) => {
          info.value = i
          setDefaultSampleRows(i.preview_default_sample_rows ?? 0)
          return i
        })
        .catch(() => null) // solo cosmetico/di default: l'app funziona anche senza
        .finally(() => { pending = null })
    }
    return pending
  }

  return { info, load }
}

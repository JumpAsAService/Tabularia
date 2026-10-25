import { ref } from 'vue'
import { useApi, type AppInfo } from '~/composables/useApi'
import { setDefaultSampleRows } from '~/composables/useFlowModel'

// Deployment info read ONCE per session and shared (settings menu, editor, schedule
// dialog). The automatic sample is propagated to useFlowModel so `devSampleOperation`
// applies it without every caller having to pass it.
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
        .catch(() => null) // only cosmetic/default: the app works without it too
        .finally(() => { pending = null })
    }
    return pending
  }

  return { info, load }
}

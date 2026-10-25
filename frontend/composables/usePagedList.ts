// A paginated list with a SERVER-SIDE search: the `q` filter goes to the server and
// matches over the whole dataset, not only the current page. It handles q (debounced),
// window (offset/limit), total, loading state. The pages pass it their endpoint's
// /…/search fetcher.
import { onMounted, ref, watch, type Ref } from 'vue'
import { errMessage, type Page } from '~/composables/useApi'
import { skeletonPad } from '~/composables/useSkeleton'

export function usePagedList<T>(
  fetcher: (p: { q?: string; limit: number; offset: number }) => Promise<Page<T>>,
  opts: { pageSize?: number; initialQuery?: string } = {},
) {
  const pageSize = opts.pageSize ?? 50
  const q = ref(opts.initialQuery ?? '') // e.g. coming from the global search
  const offset = ref(0)
  const total = ref(0)
  const items = ref<T[]>([]) as Ref<T[]>
  const loading = ref(true)
  const error = ref('')

  // `quiet`: the rows update in place, with no skeleton and nothing emptied on an
  // error, for refreshes the user did not ask for (an import that just finished):
  // the list blinking out and back in reads as a crash
  async function load(opts: { quiet?: boolean } = {}) {
    const quiet = opts.quiet === true
    if (!quiet) loading.value = true
    const t0 = performance.now()
    try {
      const r = await fetcher({ q: q.value.trim() || undefined, limit: pageSize, offset: offset.value })
      items.value = r.items
      total.value = r.total
      error.value = ''
    } catch (e) {
      if (quiet) return // keep what is on screen; the next load will tell
      error.value = errMessage(e)
      items.value = []
      total.value = 0
    } finally {
      if (!quiet) {
        await skeletonPad(t0) // the skeleton stays visible a minimum: no flash
        loading.value = false
      }
    }
  }

  // typing: resets to the first page and reloads, with a small pause
  let deb: ReturnType<typeof setTimeout> | null = null
  watch(q, () => {
    if (deb) clearTimeout(deb)
    deb = setTimeout(() => {
      offset.value = 0
      load()
    }, 350)
  })

  function next() {
    if (offset.value + pageSize < total.value) {
      offset.value += pageSize
      load()
    }
  }
  function prev() {
    if (offset.value > 0) {
      offset.value = Math.max(0, offset.value - pageSize)
      load()
    }
  }

  onMounted(() => load())
  return { q, items, total, offset, pageSize, loading, error, load, next, prev }
}

// Lista paginata con ricerca SERVER-SIDE: il filtro `q` va al server e combacia
// sull'intero dataset, non solo sulla pagina corrente. Gestisce q (debounced),
// finestra (offset/limit), totale, stato di caricamento. Le pagine ci passano il
// fetcher del rispettivo endpoint /…/search.
import { onMounted, ref, watch, type Ref } from 'vue'
import { errMessage, type Page } from '~/composables/useApi'

export function usePagedList<T>(
  fetcher: (p: { q?: string; limit: number; offset: number }) => Promise<Page<T>>,
  opts: { pageSize?: number } = {},
) {
  const pageSize = opts.pageSize ?? 50
  const q = ref('')
  const offset = ref(0)
  const total = ref(0)
  const items = ref<T[]>([]) as Ref<T[]>
  const loading = ref(true)
  const error = ref('')

  async function load() {
    loading.value = true
    try {
      const r = await fetcher({ q: q.value.trim() || undefined, limit: pageSize, offset: offset.value })
      items.value = r.items
      total.value = r.total
      error.value = ''
    } catch (e) {
      error.value = errMessage(e)
      items.value = []
      total.value = 0
    } finally {
      loading.value = false
    }
  }

  // digitando: azzera alla prima pagina e ricarica, con una piccola pausa
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

  onMounted(load)
  return { q, items, total, offset, pageSize, loading, error, load, next, prev }
}

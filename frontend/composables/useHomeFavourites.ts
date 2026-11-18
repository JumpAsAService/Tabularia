// The objects a person pinned to their home page (GET/PUT/DELETE /home/favourites): one shared
// copy, so Explore's row menu and the Favourites widget say the same thing at once.
import { ref } from 'vue'

export interface FavouriteItem { kind: string; id: number; name: string; project_id: number }

const items = ref<FavouriteItem[] | null>(null)
let pending: Promise<void> | null = null
/** raised when the person changes: an answer still on its way is the one before's, and is dropped */
let person = 0

/** The pins are one person's: forgotten when another signs in on this tab (useAuth). */
export function resetHomeFavourites() {
  items.value = null
  pending = null
  person++
}

export function useHomeFavourites() {
  const { apiFetch } = useApiClient()
  const take = (r: { items: FavouriteItem[] }) => { items.value = r.items }
  function load(force = false): Promise<void> {
    if (items.value && !force) return Promise.resolve()
    const who = person
    const mine = pending ??= apiFetch<{ items: FavouriteItem[] }>('/home/favourites')
      .then((r) => { if (who === person) take(r) })
      .finally(() => { if (pending === mine) pending = null })
    return mine
  }
  const has = (kind: string, id: number) => !!items.value?.some((f) => f.kind === kind && f.id === id)
  /** Pins what is not pinned, unpins what is; answers whether it is pinned now. */
  async function toggle(kind: string, id: number): Promise<boolean> {
    const on = !has(kind, id)
    const who = person
    const r = await apiFetch<{ items: FavouriteItem[] }>(`/home/favourites/${encodeURIComponent(kind)}/${id}`, { method: on ? 'PUT' : 'DELETE' })
    if (who === person) take(r)
    return on
  }
  return { items, load, has, toggle }
}

// Where an object lives. Names of datasources, flows, views and connections are
// unique only inside their folder, so a picker shows the folder path under each
// name: "Sample › Private tables". One shared copy of the folder tree, read again
// when it is older than a minute, so a renamed or moved folder shows up soon.
import { ref } from 'vue'
import { useProjects, type Project } from '~/composables/useProjects'

const FRESH_MS = 60_000
const folders = ref<Map<number, Project>>(new Map())
let loadedAt = 0
let pending: Promise<void> | null = null

export function useFolderPaths() {
  const api = useProjects()

  function load(force = false): Promise<void> {
    if (!force && Date.now() - loadedAt < FRESH_MS) return Promise.resolve()
    pending ??= api
      .list()
      .then((list) => {
        folders.value = new Map(list.map((p) => [p.id, p]))
        loadedAt = Date.now()
      })
      // the path is a help, not a requirement: without it the picker shows names only
      .catch(() => {})
      .finally(() => {
        pending = null
      })
    return pending
  }

  // Folders the user cannot read are missing from the tree: the path then starts
  // at the first readable one, which is still where the object lives for them.
  function pathOf(projectId: number | null | undefined): string | undefined {
    const names: string[] = []
    const seen = new Set<number>()
    let id = projectId ?? null
    while (id != null && !seen.has(id)) {
      seen.add(id)
      const folder = folders.value.get(id)
      if (!folder) break
      names.unshift(folder.name)
      id = folder.parent_id
    }
    return names.length ? names.join(' › ') : undefined
  }

  load()
  return { pathOf, load }
}

// What the flow editor knows of the saved Bayesian models its nodes pin: the models this person sees (one list for the whole
// editor, read again when it is older than half a minute) and, for a model, its versions without their graphs (the notice «a newer
// version exists» compares the semantic hashes). One copy shared by the panel and by every node on the canvas, so a flow of ten
// Bayesian nodes asks the gateway once for the list and once for each model. A model absent from the list is one this person
// does not see: the gateway answers the same for «absent» and «not seen».
import { ref } from 'vue'
import { useBayesModels, type BayesModelListed, type BayesVersionListed } from '~/composables/useBayesModels'

const FRESH_MS = 30_000
const models = ref<BayesModelListed[] | null>(null)
const failed = ref(false)
const versionsOf = ref<Record<number, BayesVersionListed[]>>({})
let modelsAt = 0
let pending: Promise<void> | null = null
const versionsAt = new Map<number, number>()
const versionsPending = new Map<number, Promise<void>>()

/** Another person signs in on this tab: what the previous one saw is forgotten. */
export function resetBayesPins() {
  models.value = null
  failed.value = false
  versionsOf.value = {}
  modelsAt = 0
  pending = null
  versionsAt.clear()
  versionsPending.clear()
}

// The API client is asked when a call is made, not when the composable is (the palette and the canvas nodes read the state alone).
export function useBayesPins() {
  const api = useBayesModels()

  function load(force = false): Promise<void> {
    if (!force && models.value && Date.now() - modelsAt < FRESH_MS) return Promise.resolve()
    if (pending) return pending
    const mine: Promise<void> = pending = api
      .list()
      .then((list) => {
        if (pending !== mine) return // asked for the person before
        models.value = list
        failed.value = false
        modelsAt = Date.now()
      })
      .catch(() => { if (pending === mine) failed.value = true })
      .finally(() => { if (pending === mine) pending = null })
    return mine
  }

  function loadVersions(id: number, force = false): Promise<void> {
    if (!force && versionsOf.value[id] && Date.now() - (versionsAt.get(id) ?? 0) < FRESH_MS) return Promise.resolve()
    const running = versionsPending.get(id)
    if (running) return running
    const mine: Promise<void> = api
      .versions(id)
      .then((list) => {
        versionsOf.value = { ...versionsOf.value, [id]: list }
        versionsAt.set(id, Date.now())
      })
      .catch(() => { /* the notice is a help: without the list it is not shown */ })
      .finally(() => { if (versionsPending.get(id) === mine) versionsPending.delete(id) })
    versionsPending.set(id, mine)
    return mine
  }

  return { models, failed, versionsOf, load, loadVersions }
}

// The statistical-model groups as the flow editor needs them: which ones the administrator turned
// on and this person may use (the palette shows only those, the nodes of the others are greyed
// out with the reason), and the lists a Predict node chooses its model from.
//
// The contract of the route the editor asks (built by the groups' lane, `gateway-go/internal/api/
// models_admin.go` or a file beside it; any signed-in person who may edit flows may ask):
//
//   GET /models/groups
//   → { "disabled_reason": null | "kubernetes" | "not_configured",
//       "groups": [ { "id": "regression", "enabled": true, "usable": true, "ready": true,
//                     "max_timeout_seconds": 3600, "max_rows": 1000000 }, … ] }
//
// One entry per group the installation ships (the catalogue's ids), whether enabled or not:
// `enabled` is the administrator's switch, `usable` is `enabled` and this person is among those
// the administrator let use it, `ready` is a built version. `disabled_reason` is «kubernetes» on
// Compose and «not_configured» where the scripting machinery is not set up. The numbers are the
// group's limits as the administrator set them (null: the recipe's).
//
// Predict's lists (gateway-go/internal/api/models_mlflow.go, the engine's `POST /models/mlflow/browse`),
// with CONNECT on the connection:
//
//   GET /models/mlflow/{connection_id}/experiments
//   → { "experiments": [ { "name": "Pasta/price elasticity" }, … ] }
//   GET /models/mlflow/{connection_id}/versions?experiment=<name>
//   → { "versions": [ { "model": "price-elasticity", "version": "7", "kind": "linear",
//                       "created_at": "2026-11-19T10:00:00Z", "flow": "Pricing", "node": "ctl-3",
//                       "metric": { "name": "r_squared", "value": 0.83 } }, … ] }   (newest first)
//
// `kind` is the catalogue id of the model the version holds (its `tabularia.model` tag): it decides
// which options the Predict form offers.
import { computed, ref } from 'vue'
import type { ModelGroupsState } from '~/composables/modelCatalog'

export interface MlflowExperiment { name: string }
export interface MlflowVersion {
  model: string
  version: string
  kind: string
  created_at?: string | null
  flow?: string | null
  node?: string | null
  metric?: { name: string; value: number } | null
}

// one copy for the whole editor (palette, node panels); loaded again when the editor page opens, so
// what an administrator changed meanwhile shows up without reloading the browser
const state = ref<ModelGroupsState | null>(null)
let loading: Promise<ModelGroupsState | null> | null = null

// another person signs in in the same tab: nothing of the previous one's stays
export function resetModelGroups() {
  state.value = null
  loading = null
}

// The API client is asked when a call is made, not when the composable is: the palette reads the state
// alone, and the tests that draw it need no Nuxt.
export function useModelGroups() {
  function load(force = false): Promise<ModelGroupsState | null> {
    if (loading && !force) return loading
    const { apiFetch } = useApiClient()
    loading = apiFetch<ModelGroupsState>('/models/groups')
      .then((s) => (state.value = s))
      // a failure is not kept: the next load asks again (a gateway restarting, a gateway that
      // has no such route yet: the palette then has no models, as when none is enabled)
      .catch(() => { loading = null; return (state.value = null) })
    return loading
  }
  const experiments = (connectionId: number) =>
    useApiClient().apiFetch<{ experiments: MlflowExperiment[] }>(`/models/mlflow/${connectionId}/experiments`).then((r) => r.experiments ?? [])
  const versions = (connectionId: number, experiment: string) =>
    useApiClient().apiFetch<{ versions: MlflowVersion[] }>(`/models/mlflow/${connectionId}/versions`, { query: { experiment } }).then((r) => r.versions ?? [])
  // the palette offers the group when there is a group to use (or Compose, to say it needs Kubernetes)
  const available = computed(() => !!state.value && (state.value.disabled_reason === 'kubernetes' || state.value.groups.some((g) => g.usable)))
  return { state, load, experiments, versions, available }
}

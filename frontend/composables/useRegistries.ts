// Container registries (Admin panel): where the images of the Container node come from.
// The gateway's /admin/registries; the password and the secret variables never come back.
import { computed, ref } from 'vue'

export type RegistryTls = 'public' | 'ca' | 'none'

export interface RegistryVariable {
  name: string
  secret: boolean
  value: string | null // null for a secret one
  updated_at: string | null
  updated_by: string | null
}

// who may see and use a registry: a group or a user, and which repositories
// (empty: all those the registry allows)
export interface RegistryAccess {
  group_id: number | null
  user_id: number | null
  repositories: string[]
  name?: string // the group's name or the user's email, as the server reads them
}

export interface Registry {
  id: number
  name: string
  description: string
  host: string
  protocol: 'https' | 'http'
  tls: RegistryTls
  ca_cert: string
  username: string
  password_set: boolean
  repositories: string[]
  enabled: boolean
  access: RegistryAccess[]
  variables: RegistryVariable[]
  last_test_at: string | null
  last_test_ok: boolean | null
  last_test_message: string
  created_at: string | null
  created_by: string | null
  updated_at: string | null
  updated_by: string | null
}

export interface RegistriesState {
  disabled_reason: 'kubernetes' | null
  editable: boolean
  registries: Registry[]
}

export interface RegistryInput {
  name?: string
  description?: string
  host?: string
  protocol?: 'https' | 'http'
  tls?: RegistryTls
  ca_cert?: string
  username?: string
  password?: string // absent: kept; "": removed
  repositories?: string[]
  enabled?: boolean
  access?: RegistryAccess[]
}

export interface RegistryTest {
  ok: boolean
  message: string
  auth: 'anonymous' | 'basic' | 'bearer' | null
  repository: string | null
  tags: string[] | null
}

export function useRegistries() {
  const { apiFetch } = useApiClient()
  const base = '/admin/registries'
  return {
    get: () => apiFetch<RegistriesState>(base),
    registry: (id: number) => apiFetch<Registry>(`${base}/${id}`),
    create: (body: RegistryInput) => apiFetch<Registry>(base, { method: 'POST', body }),
    update: (id: number, body: RegistryInput) => apiFetch<Registry>(`${base}/${id}`, { method: 'PUT', body }),
    remove: (id: number) => apiFetch<void>(`${base}/${id}`, { method: 'DELETE' }),
    test: (id: number) => apiFetch<RegistryTest>(`${base}/${id}/test`, { method: 'POST' }),
    tags: (id: number, repository: string) =>
      apiFetch<{ repository: string; tags: string[] }>(`${base}/${id}/tags`, { query: { repository } }),
    // can a node of the cluster download this image? (nothing of it runs)
    clusterTest: (id: number, repository: string, reference: string) =>
      apiFetch<{ ok: boolean; message: string; image: string | null }>(`${base}/${id}/cluster-test`, {
        method: 'POST', body: { repository, reference },
      }),
    setVariable: (id: number, name: string, body: { value?: string; secret: boolean }) =>
      apiFetch<Registry>(`${base}/${id}/variables/${encodeURIComponent(name)}`, { method: 'PUT', body }),
    deleteVariable: (id: number, name: string) =>
      apiFetch<Registry>(`${base}/${id}/variables/${encodeURIComponent(name)}`, { method: 'DELETE' }),
  }
}

// the flow editor's view: only the registries and repositories the person may use
export interface EditorRegistry {
  id: number
  name: string
  description: string
  host: string
  repositories: string[] // patterns (team/*) as well as names
  variables: { name: string; secret: boolean; value: string | null }[] // set by the administrators
}

/** What an image does when nothing overrides it, read from its registry. */
export interface ImageConfig {
  repository: string
  reference: string
  digest: string | null
  platforms: string[]
  created: string | null
  entrypoint: string[]
  cmd: string[]
  env: { name: string; value: string }[]
  labels: Record<string, string>
  working_dir: string | null
  user: string | null
  size: number | null
}

export interface EditorRegistries {
  disabled_reason: 'kubernetes' | null
  registries: EditorRegistry[]
  limits: { max_cpu: string; max_memory: string; max_timeout_seconds: number }
  storage_ready: boolean // the scripts bucket, through which a node's input and output go
  tabularia_variables: string[]
}

// one copy for the whole editor (palette, nodes, node panel), loaded again when the
// editor page opens: a registry granted meanwhile shows up without reloading
const editorState = ref<EditorRegistries | null>(null)
let editorLoading: Promise<EditorRegistries | null> | null = null

export function useRegistriesEditor() {
  const { apiFetch } = useApiClient()
  function load(force = false): Promise<EditorRegistries | null> {
    if (editorLoading && !force) return editorLoading
    editorLoading = apiFetch<EditorRegistries>('/registries')
      .then((s) => (editorState.value = s))
      .catch(() => (editorState.value = null))
    return editorLoading
  }
  const tags = (id: number, repository: string) =>
    apiFetch<{ repository: string; tags: string[] }>(`/registries/${id}/tags`, { query: { repository } })
  const image = (id: number, repository: string, reference: string) =>
    apiFetch<ImageConfig>(`/registries/${id}/image`, { query: { repository, reference: reference || 'latest' } })
  // the palette offers the Container node when there is a registry to take images from
  const available = computed(() => !!editorState.value && !editorState.value.disabled_reason
    && editorState.value.registries.length > 0)
  return { state: editorState, load, tags, image, available }
}

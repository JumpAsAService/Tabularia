<script setup lang="ts">
// Container registries (Admin panel): where the images of the Container node come from.
// A registry has an address, a way to trust its certificate (the public authorities,
// the company's own, or none for a test registry), credentials, and the repositories
// whose images may run. Kubernetes only: elsewhere the section says why. Observers
// read it all, without any secret, and change nothing.
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  ArrowLeft, Boxes, CheckCircle2, CircleDot, KeyRound, ListTree, LoaderCircle, Plus, Send, ServerCog, ShieldAlert,
  Trash2, TriangleAlert, XCircle,
} from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useFormat } from '~/composables/useFormat'
import type { GroupOut, UserOut } from '~/composables/useProjects'
import {
  useRegistries, type RegistriesState, type Registry, type RegistryAccess, type RegistryInput, type RegistryTest,
  type RegistryTls,
} from '~/composables/useRegistries'

const props = defineProps<{ groups: GroupOut[]; users: UserOut[] }>()
const emit = defineEmits<{ (e: 'count', n: number): void }>()

const { t } = useI18n()
const fmt = useFormat()
const api = useRegistries()
const toast = useToast()

const state = ref<RegistriesState | null>(null)
const loadError = ref('')
const editable = computed(() => !!state.value?.editable && !state.value?.disabled_reason)

async function load() {
  try {
    state.value = await api.get()
    loadError.value = ''
    emit('count', state.value.registries.length)
  } catch (e) {
    loadError.value = errMessage(e)
  }
}
onMounted(load)

// ── one registry ────────────────────────────────────────────────────────────
// null = the list; 'new' = the creation form; a number = that registry
const open = ref<number | 'new' | null>(null)
const reg = ref<Registry | null>(null)
const saving = ref(false)

interface Form {
  name: string
  description: string
  host: string
  protocol: 'https' | 'http'
  tls: RegistryTls
  ca_cert: string
  username: string
  password: string
  repositories: string
  enabled: boolean
  access: { group_id: number | null; user_id: number | null; repositories: string }[]
}
const blank = (): Form => ({
  name: '', description: '', host: '', protocol: 'https', tls: 'public', ca_cert: '', username: '', password: '',
  repositories: '', enabled: true, access: [],
})
const form = ref<Form>(blank())

function fill(r: Registry) {
  form.value = {
    name: r.name, description: r.description, host: r.host, protocol: r.protocol, tls: r.tls, ca_cert: r.ca_cert,
    username: r.username, password: '', repositories: r.repositories.join('\n'), enabled: r.enabled,
    access: r.access.map((a) => ({ group_id: a.group_id, user_id: a.user_id, repositories: a.repositories.join('\n') })),
  }
}

function startNew() {
  reg.value = null
  form.value = blank()
  test.value = null
  open.value = 'new'
}

async function openRegistry(id: number) {
  try {
    reg.value = await api.registry(id)
    fill(reg.value)
    test.value = null
    tagRepo.value = reg.value.repositories.find((x) => !x.includes('*')) ?? ''
    tags.value = null
    pullRepo.value = tagRepo.value
    pull.value = null
    open.value = id
  } catch (e) {
    toast.error(errMessage(e))
  }
}

function back() {
  open.value = null
  reg.value = null
  load()
}

const lines = (text: string) => text.split('\n').map((x) => x.trim()).filter(Boolean)

// on an existing registry only what differs goes out; the password only when typed
const changes = computed<RegistryInput>(() => {
  const f = form.value
  const body: RegistryInput = {
    name: f.name.trim(), description: f.description.trim(), host: f.host.trim(), protocol: f.protocol,
    tls: f.protocol === 'http' ? 'none' : f.tls, ca_cert: f.tls === 'ca' && f.protocol === 'https' ? f.ca_cert.trim() : '',
    username: f.username.trim(), repositories: lines(f.repositories), enabled: f.enabled,
    access: f.access.map((a) => ({ group_id: a.group_id, user_id: a.user_id, repositories: lines(a.repositories) })),
  }
  const r = reg.value
  if (!r) return { ...body, ...(f.password ? { password: f.password } : {}) }
  const before: RegistryInput = {
    name: r.name, description: r.description, host: r.host, protocol: r.protocol, tls: r.tls, ca_cert: r.ca_cert,
    username: r.username, repositories: r.repositories, enabled: r.enabled,
    access: r.access.map((a) => ({ group_id: a.group_id, user_id: a.user_id, repositories: a.repositories })),
  }
  const out: RegistryInput = {}
  for (const k of Object.keys(body) as (keyof RegistryInput)[]) {
    if (JSON.stringify(body[k]) !== JSON.stringify(before[k])) (out as Record<string, unknown>)[k] = body[k]
  }
  if (f.password) out.password = f.password
  return out
})
const dirty = computed(() => Object.keys(changes.value).length > 0)

async function save(): Promise<boolean> {
  saving.value = true
  try {
    const saved = open.value === 'new' ? await api.create(changes.value) : await api.update(reg.value!.id, changes.value)
    reg.value = saved
    fill(saved)
    open.value = saved.id
    toast.success(t('registries.saved'))
    return true
  } catch (e) {
    toast.error(errMessage(e))
    return false
  } finally {
    saving.value = false
  }
}

async function forgetPassword() {
  if (!reg.value || !confirm(t('registries.confirmForgetPassword'))) return
  try {
    reg.value = await api.update(reg.value.id, { password: '' })
    fill(reg.value)
  } catch (e) {
    toast.error(errMessage(e))
  }
}

async function remove() {
  const r = reg.value
  if (!r || !confirm(t('registries.confirmDelete', { name: r.name }))) return
  try {
    await api.remove(r.id)
    toast.success(t('registries.deleted', { name: r.name }))
    back()
  } catch (e) {
    toast.error(errMessage(e))
  }
}

// ── who may use it ──────────────────────────────────────────────────────────
// a new line: a group or a user not listed yet, with all the registry's repositories
const newAccess = ref<{ kind: 'group' | 'user'; id: number | null }>({ kind: 'group', id: null })
const accessOptions = computed(() => {
  const taken = new Set(form.value.access.map((a) => (a.group_id !== null ? `g${a.group_id}` : `u${a.user_id}`)))
  return newAccess.value.kind === 'group'
    ? props.groups.filter((g) => !taken.has(`g${g.id}`)).map((g) => ({ id: g.id, label: g.name }))
    : props.users.filter((u) => !u.is_superuser && !taken.has(`u${u.id}`)).map((u) => ({ id: u.id, label: u.email }))
})

function addAccess() {
  const n = newAccess.value
  if (n.id === null) return
  form.value.access.push({ group_id: n.kind === 'group' ? n.id : null, user_id: n.kind === 'user' ? n.id : null, repositories: '' })
  newAccess.value = { kind: n.kind, id: null }
}

function accessName(a: { group_id: number | null; user_id: number | null }): string {
  if (a.group_id !== null) return props.groups.find((g) => g.id === a.group_id)?.name ?? `#${a.group_id}`
  return props.users.find((u) => u.id === a.user_id)?.email ?? `#${a.user_id}`
}

function accessSummary(list: RegistryAccess[]): string {
  return list.length ? list.map((a) => a.name ?? accessName(a)).join(', ') : t('registries.adminsOnly')
}

// ── the test, and the tags of a repository ──────────────────────────────────
const testing = ref(false)
const test = ref<RegistryTest | null>(null)

async function runTest() {
  testing.value = true
  test.value = null
  try {
    if (dirty.value && !(await save())) return
    test.value = await api.test(reg.value!.id)
    reg.value = await api.registry(reg.value!.id)
  } catch (e) {
    test.value = { ok: false, message: errMessage(e), auth: null, repository: null, tags: null }
  } finally {
    testing.value = false
  }
}

const tagRepo = ref('')
const tags = ref<string[] | null>(null)
const tagsLoading = ref(false)
const tagsError = ref('')

async function readTags() {
  if (!reg.value || !tagRepo.value.trim()) return
  tagsLoading.value = true
  tagsError.value = ''
  tags.value = null
  try {
    tags.value = (await api.tags(reg.value.id, tagRepo.value.trim())).tags
  } catch (e) {
    tagsError.value = errMessage(e)
  } finally {
    tagsLoading.value = false
  }
}

// ── «Try in the cluster»: a node downloads the image, nothing of it runs ─────
const pullRepo = ref('')
const pullRef = ref('latest')
const pulling = ref(false)
const pull = ref<{ ok: boolean; message: string; image: string | null } | null>(null)

async function tryInCluster() {
  if (!reg.value || !pullRepo.value.trim()) return
  pulling.value = true
  pull.value = null
  try {
    pull.value = await api.clusterTest(reg.value.id, pullRepo.value.trim(), pullRef.value.trim() || 'latest')
  } catch (e) {
    pull.value = { ok: false, message: errMessage(e), image: null }
  } finally {
    pulling.value = false
  }
}

// ── variables ───────────────────────────────────────────────────────────────
const variable = ref({ name: '', value: '', secret: false })
const editingVariable = ref<string | null>(null)
const savingVariable = ref(false)

function editVariable(name: string) {
  const v = reg.value?.variables.find((x) => x.name === name)
  if (!v) return
  variable.value = { name: v.name, value: v.secret ? '' : v.value ?? '', secret: v.secret }
  editingVariable.value = v.name
}

function clearVariable() {
  variable.value = { name: '', value: '', secret: false }
  editingVariable.value = null
}

async function saveVariable() {
  if (!reg.value) return
  const v = variable.value
  const name = v.name.trim().toUpperCase()
  // a secret one being edited keeps its value when the field is left empty
  const keep = editingVariable.value === name && v.secret && !v.value
  savingVariable.value = true
  try {
    reg.value = await api.setVariable(reg.value.id, name, { ...(keep ? {} : { value: v.value }), secret: v.secret })
    clearVariable()
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    savingVariable.value = false
  }
}

async function deleteVariable(name: string) {
  if (!reg.value || !confirm(t('registries.confirmDeleteVariable', { name }))) return
  try {
    reg.value = await api.deleteVariable(reg.value.id, name)
    if (editingVariable.value === name) clearVariable()
  } catch (e) {
    toast.error(errMessage(e))
  }
}

// ── how things read ─────────────────────────────────────────────────────────
function trustLabel(r: Pick<Registry, 'protocol' | 'tls'>): string {
  if (r.protocol === 'http') return t('registries.trust.http')
  return t(`registries.trust.${r.tls}`)
}

function testIcon(ok: boolean | null) {
  return ok === null ? CircleDot : ok ? CheckCircle2 : XCircle
}
</script>

<template>
  <div class="card">
    <h4><Boxes :size="14" /> {{ $t('registries.title') }}</h4>
    <p class="hint muted small">{{ $t('registries.lead') }}</p>
    <p v-if="loadError" class="page-error" role="alert">{{ loadError }}</p>
    <p v-if="state?.disabled_reason" class="note" role="note">
      <TriangleAlert :size="13" /> {{ $t('registries.offKubernetes') }}
    </p>
  </div>

  <!-- the list -->
  <div v-if="state && open === null" class="card">
    <div class="heading">
      <h4>{{ $t('registries.listTitle') }}</h4>
      <button v-if="editable" type="button" class="primary mini" @click="startNew">
        <Plus :size="13" /> {{ $t('registries.new') }}
      </button>
    </div>
    <p v-if="!state.registries.length" class="hint muted small">{{ $t('registries.none') }}</p>
    <ul v-else class="registries">
      <li v-for="r in state.registries" :key="r.id">
        <button type="button" class="registry" @click="openRegistry(r.id)">
          <span class="name">{{ r.name }}<em v-if="!r.enabled" class="off">{{ $t('registries.disabled') }}</em></span>
          <span class="host"><code>{{ r.host }}</code></span>
          <span class="trust" :class="{ weak: r.protocol === 'http' || r.tls === 'none' }">
            <ShieldAlert v-if="r.protocol === 'http' || r.tls === 'none'" :size="12" /> {{ trustLabel(r) }}
          </span>
          <span class="trial" :class="r.last_test_ok === null ? '' : r.last_test_ok ? 'ok' : 'ko'">
            <component :is="testIcon(r.last_test_ok)" :size="13" />
            {{ r.last_test_ok === null ? $t('registries.neverTested') : r.last_test_ok
              ? $t('registries.testedOk', { when: fmt.ago(r.last_test_at) }) : $t('registries.testedKo', { when: fmt.ago(r.last_test_at) }) }}
          </span>
          <span class="muted small">
            {{ accessSummary(r.access) }}
            · {{ $t('registries.repositoriesCount', { n: r.repositories.length }) }}
          </span>
        </button>
      </li>
    </ul>
  </div>

  <!-- one registry -->
  <template v-else-if="state && open !== null">
    <form class="card" @submit.prevent="save">
      <div class="heading">
        <button type="button" class="mini" @click="back"><ArrowLeft :size="13" /> {{ $t('registries.back') }}</button>
        <h4>{{ open === 'new' ? $t('registries.new') : reg?.name }}</h4>
        <button v-if="editable && reg" type="button" class="mini danger-zone" @click="remove">
          <Trash2 :size="13" /> {{ $t('registries.delete') }}
        </button>
      </div>
      <div class="fields">
        <label class="field">
          <span>{{ $t('registries.name') }}</span>
          <input v-model="form.name" type="text" maxlength="120" required :disabled="!editable || saving" />
        </label>
        <label class="field">
          <span>{{ $t('registries.host') }}</span>
          <input v-model="form.host" type="text" maxlength="260" required spellcheck="false" autocomplete="off"
                 placeholder="ghcr.io" :disabled="!editable || saving" />
          <small>{{ $t('registries.hostHint') }}</small>
        </label>
        <label class="field largo">
          <span>{{ $t('registries.description') }}</span>
          <input v-model="form.description" type="text" maxlength="2000" :disabled="!editable || saving" />
        </label>
        <label class="field">
          <span>{{ $t('registries.protocol') }}</span>
          <select v-model="form.protocol" :disabled="!editable || saving">
            <option value="https">HTTPS</option>
            <option value="http">{{ $t('registries.httpOption') }}</option>
          </select>
        </label>
        <label class="field">
          <span>{{ $t('registries.certificate') }}</span>
          <select v-model="form.tls" :disabled="!editable || saving || form.protocol === 'http'">
            <option value="public">{{ $t('registries.trust.public') }}</option>
            <option value="ca">{{ $t('registries.trust.ca') }}</option>
            <option value="none">{{ $t('registries.trust.none') }}</option>
          </select>
        </label>
        <label v-if="form.protocol === 'https' && form.tls === 'ca'" class="field largo">
          <span>{{ $t('registries.caCert') }}</span>
          <textarea v-model="form.ca_cert" class="pem" rows="6" spellcheck="false"
                    placeholder="-----BEGIN CERTIFICATE-----" :disabled="!editable || saving" />
          <small>{{ $t('registries.caCertHint') }}</small>
        </label>
      </div>
      <p v-if="form.protocol === 'http' || form.tls === 'none'" class="notice" role="note">
        <ShieldAlert :size="13" />
        {{ form.protocol === 'http' ? $t('registries.httpWarning') : $t('registries.noneWarning') }}
      </p>
      <p v-if="form.protocol === 'http' || form.tls === 'ca'" class="hint muted small">
        {{ $t('registries.nodesTrust') }}
      </p>

      <fieldset class="field-group">
        <legend>{{ $t('registries.credentials') }}</legend>
        <div class="fields">
          <label class="field">
            <span>{{ $t('registries.username') }}</span>
            <input v-model="form.username" type="text" maxlength="255" autocomplete="off" spellcheck="false"
                   :disabled="!editable || saving" />
          </label>
          <label class="field">
            <span>{{ $t('registries.password') }}</span>
            <input v-model="form.password" type="password" autocomplete="new-password"
                   :placeholder="reg?.password_set ? $t('registries.secretSet') : ''" :disabled="!editable || saving" />
          </label>
        </div>
        <small class="hint muted">{{ $t('registries.credentialsHint') }}</small>
        <button v-if="editable && reg?.password_set" type="button" class="mini align-start" @click="forgetPassword">
          {{ $t('registries.forgetPassword') }}
        </button>
      </fieldset>

      <label class="field">
        <span>{{ $t('registries.repositories') }}</span>
        <textarea v-model="form.repositories" class="pem" rows="5" spellcheck="false"
                  :placeholder="'team/forecast\nml/*'" :disabled="!editable || saving" />
        <small>{{ $t('registries.repositoriesHint') }}</small>
      </label>

      <fieldset class="field-group">
        <legend>{{ $t('registries.access') }}</legend>
        <p class="hint muted small">{{ $t('registries.accessHint') }}</p>
        <table v-if="form.access.length" class="sign-ins">
          <tbody>
            <tr v-for="(a, i) in form.access" :key="a.group_id !== null ? `g${a.group_id}` : `u${a.user_id}`">
              <td class="chi">
                <span class="label">{{ a.group_id !== null ? $t('registries.group') : $t('registries.user') }}</span>
                {{ accessName(a) }}
              </td>
              <td>
                <textarea v-model="a.repositories" class="pem" rows="2" spellcheck="false"
                          :placeholder="$t('registries.allRepositories')"
                          :aria-label="$t('registries.accessRepositories', { name: accessName(a) })"
                          :disabled="!editable || saving" />
              </td>
              <td v-if="editable" class="row-actions">
                <button type="button" class="mini icon-only" :aria-label="$t('registries.removeAccess', { name: accessName(a) })"
                        @click="form.access.splice(i, 1)"><Trash2 :size="13" /></button>
              </td>
            </tr>
          </tbody>
        </table>
        <p v-else class="hint muted small">{{ $t('registries.adminsOnlyHint') }}</p>
        <div v-if="editable" class="new-access">
          <select v-model="newAccess.kind" :aria-label="$t('registries.accessKind')" :disabled="saving" @change="newAccess.id = null">
            <option value="group">{{ $t('registries.group') }}</option>
            <option value="user">{{ $t('registries.user') }}</option>
          </select>
          <select v-model="newAccess.id" :aria-label="$t('registries.accessWho')" :disabled="saving">
            <option :value="null" disabled>{{ newAccess.kind === 'group' ? $t('registries.chooseGroup') : $t('registries.chooseUser') }}</option>
            <option v-for="o in accessOptions" :key="o.id" :value="o.id">{{ o.label }}</option>
          </select>
          <button type="button" class="mini" :disabled="saving || newAccess.id === null" @click="addAccess">
            <Plus :size="13" /> {{ $t('registries.addAccess') }}
          </button>
        </div>
      </fieldset>

      <label class="tick">
        <input v-model="form.enabled" type="checkbox" :disabled="!editable || saving" />
        <span>{{ $t('registries.enabled') }}</span>
      </label>

      <div v-if="editable" class="actions">
        <button type="submit" class="primary" :disabled="saving || !dirty || !form.name.trim() || !form.host.trim()">
          <LoaderCircle v-if="saving" :size="13" class="spin" /> {{ $t('registries.save') }}
        </button>
        <button type="button" :disabled="testing || saving || !form.name.trim() || !form.host.trim()" @click="runTest">
          <LoaderCircle v-if="testing" :size="13" class="spin" /><Send v-else :size="13" />
          {{ dirty ? $t('registries.saveAndTest') : $t('registries.test') }}
        </button>
      </div>
      <p v-if="test" class="outcome" :class="test.ok ? 'ok' : 'ko'" role="status">
        <component :is="test.ok ? CheckCircle2 : XCircle" :size="14" />
        <span>
          {{ test.ok ? $t('registries.testOk') : $t('registries.testFailed') }}
          <code>{{ test.message }}</code>
          <span v-if="test.tags?.length" class="muted small">{{ $t('registries.someTags', { tags: test.tags.join(', ') }) }}</span>
        </span>
      </p>
      <p v-else-if="reg?.last_test_at" class="outcome" :class="reg.last_test_ok ? 'ok' : 'ko'">
        <component :is="testIcon(reg.last_test_ok)" :size="14" />
        <span>
          {{ reg.last_test_ok ? $t('registries.testedOk', { when: fmt.ago(reg.last_test_at) }) : $t('registries.testedKo', { when: fmt.ago(reg.last_test_at) }) }}
          <code>{{ reg.last_test_message }}</code>
        </span>
      </p>
    </form>

    <template v-if="reg">
      <!-- the tags of an allowed repository -->
      <div class="card">
        <h4><ListTree :size="14" /> {{ $t('registries.tagsTitle') }}</h4>
        <p class="hint muted small">{{ $t('registries.tagsLead') }}</p>
        <form class="read-tags" @submit.prevent="readTags">
          <input v-model="tagRepo" type="text" list="repository-suggestions" spellcheck="false" autocomplete="off"
                 :placeholder="$t('registries.repository')" :aria-label="$t('registries.repository')" />
          <datalist id="repository-suggestions">
            <option v-for="x in reg.repositories.filter((r) => !r.includes('*'))" :key="x" :value="x" />
          </datalist>
          <button type="submit" class="mini" :disabled="tagsLoading || !tagRepo.trim()">
            <LoaderCircle v-if="tagsLoading" :size="13" class="spin" /> {{ $t('registries.showTags') }}
          </button>
        </form>
        <p v-if="tagsError" class="error small" role="alert">{{ tagsError }}</p>
        <p v-else-if="tags && !tags.length" class="hint muted small">{{ $t('registries.noTags') }}</p>
        <ul v-else-if="tags" class="tag">
          <li v-for="x in tags" :key="x"><code>{{ x }}</code></li>
        </ul>
      </div>

      <!-- can the cluster's nodes download from it? -->
      <div class="card">
        <h4><ServerCog :size="14" /> {{ $t('registries.clusterTitle') }}</h4>
        <p class="hint muted small">{{ $t('registries.clusterLead') }}</p>
        <form v-if="editable" class="read-tags" @submit.prevent="tryInCluster">
          <input v-model="pullRepo" type="text" list="repository-suggestions" spellcheck="false" autocomplete="off"
                 :placeholder="$t('registries.repository')" :aria-label="$t('registries.repository')" />
          <input v-model="pullRef" class="reference" type="text" spellcheck="false" autocomplete="off"
                 placeholder="latest" :aria-label="$t('registries.reference')" />
          <button type="submit" class="mini" :disabled="pulling || !pullRepo.trim()">
            <LoaderCircle v-if="pulling" :size="13" class="spin" /><ServerCog v-else :size="13" /> {{ $t('registries.tryInCluster') }}
          </button>
        </form>
        <p v-if="pulling" class="hint muted small">{{ $t('registries.clusterWaiting') }}</p>
        <p v-if="pull" class="outcome" :class="pull.ok ? 'ok' : 'ko'" role="status">
          <component :is="pull.ok ? CheckCircle2 : XCircle" :size="14" />
          <span>
            {{ pull.ok ? $t('registries.clusterOk') : $t('registries.clusterFailed') }}
            <code>{{ pull.message }}</code>
          </span>
        </p>
      </div>

      <!-- variables -->
      <div class="card">
        <h4><KeyRound :size="14" /> {{ $t('registries.variablesTitle') }}</h4>
        <p class="hint muted small">{{ $t('registries.variablesLead') }}</p>
        <table v-if="reg.variables.length" class="variables">
          <tbody>
            <tr v-for="v in reg.variables" :key="v.name">
              <td><code>{{ v.name }}</code></td>
              <td>
                <span v-if="v.secret" class="muted">•••••••• <em>{{ $t('registries.secret') }}</em></span>
                <code v-else class="value">{{ v.value }}</code>
              </td>
              <td class="muted small">{{ fmt.ago(v.updated_at) }}</td>
              <td v-if="editable" class="row-actions">
                <button type="button" class="mini" @click="editVariable(v.name)">{{ $t('registries.change') }}</button>
                <button type="button" class="mini icon-only" :aria-label="$t('registries.delete')" @click="deleteVariable(v.name)"><Trash2 :size="13" /></button>
              </td>
            </tr>
          </tbody>
        </table>
        <form v-if="editable" class="new-variable" @submit.prevent="saveVariable">
          <input v-model="variable.name" type="text" :placeholder="$t('registries.variableName')"
                 :disabled="!!editingVariable || savingVariable" spellcheck="false" />
          <input v-model="variable.value" :type="variable.secret ? 'password' : 'text'" autocomplete="new-password"
                 :placeholder="editingVariable && variable.secret ? $t('registries.secretKeep') : $t('registries.variableValue')"
                 :disabled="savingVariable" spellcheck="false" />
          <label class="tick"><input v-model="variable.secret" type="checkbox" :disabled="savingVariable" /> {{ $t('registries.secret') }}</label>
          <button type="submit" class="primary mini" :disabled="savingVariable || !variable.name.trim()">
            {{ editingVariable ? $t('registries.save') : $t('registries.add') }}
          </button>
          <button v-if="editingVariable" type="button" class="mini" @click="clearVariable">{{ $t('registries.cancel') }}</button>
        </form>
      </div>
    </template>
  </template>

  <p v-if="state && !state.editable" class="note">{{ $t('registries.readOnly') }}</p>
</template>

<style scoped>
.card { display: flex; flex-direction: column; gap: 10px; padding: 16px; background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius); }
.card h4 { margin: 0; display: inline-flex; align-items: center; gap: 7px; }
.hint { margin: 0; line-height: 1.5; }
.small { font-size: 12px; }
.page-error, .error { color: var(--danger); margin: 0; }
.page-error { font-size: 13px; }
.note { margin: 0; display: flex; gap: 6px; align-items: flex-start; font-size: 12.5px; padding: 8px 10px; border-radius: 8px; background: var(--bg-soft); border: 1px solid var(--border); }
.heading { display: flex; align-items: center; gap: 10px; }
.heading h4 { flex: 1; }
button.mini { padding: 2px 10px; min-height: 26px; font-size: 12px; display: inline-flex; align-items: center; gap: 5px; }
button.mini.icon-only { padding: 2px 6px; }
button.danger-zone { color: var(--danger); }
button.align-start { align-self: flex-start; }

.fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px 14px; }
.field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); min-width: 0; }
.field.largo { grid-column: 1 / -1; }
.field small { font-size: 11.5px; line-height: 1.45; }
.pem { font-family: var(--font-mono, ui-monospace, monospace); font-size: 12px; resize: vertical; }
.field-group { border: 1px solid var(--border-soft); border-radius: 8px; padding: 10px 12px; margin: 0; display: flex; flex-direction: column; gap: 8px; }
.field-group legend { font-size: 12px; color: var(--muted); padding: 0 4px; }
.sign-ins { width: 100%; border-collapse: collapse; font-size: 13px; }
.sign-ins td { padding: 6px 8px; border-top: 1px solid var(--border-soft); vertical-align: top; }
.sign-ins td.chi { width: 34%; overflow-wrap: anywhere; }
.sign-ins textarea { width: 100%; }
.label { font-size: 11px; padding: 1px 7px; border-radius: 999px; border: 1px solid var(--border); margin-right: 6px; color: var(--muted); }
.new-access { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 8px; align-items: center; }
.tick { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; }
.actions { display: flex; gap: 8px; flex-wrap: wrap; }
.notice { margin: 0; display: flex; gap: 6px; align-items: flex-start; font-size: 12.5px; color: var(--warning); }
.outcome { margin: 0; display: flex; gap: 8px; align-items: flex-start; font-size: 13px; }
.outcome > span { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.outcome code { display: block; font-size: 11.5px; white-space: pre-wrap; overflow-wrap: anywhere; color: var(--muted); }
.outcome.ok svg { color: var(--accent-2); }
.outcome.ko svg { color: var(--danger); }

.registries { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.registry {
  width: 100%; display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(0, 1.1fr) minmax(0, 1fr) minmax(0, 1fr) minmax(0, 1.2fr);
  gap: 10px; align-items: center; text-align: left; padding: 10px 12px; border: 1px solid var(--border-soft);
  border-radius: 8px; background: var(--bg-soft); font-size: 13px; min-height: 0;
}
.registry:hover { border-color: var(--border); }
.name { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.host code { font-size: 12px; overflow-wrap: anywhere; }
.off { font-style: normal; font-weight: 400; font-size: 11px; color: var(--muted); margin-left: 8px; }
.trust { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; color: var(--muted); }
.trust.weak { color: var(--warning); }
.trial { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; color: var(--muted); }
.trial.ok svg { color: var(--accent-2); }
.trial.ko, .trial.ko svg { color: var(--danger); }

.read-tags { display: flex; gap: 8px; align-items: center; }
.read-tags input { flex: 1; min-width: 0; font-family: var(--font-mono, ui-monospace, monospace); font-size: 12.5px; }
.read-tags input.reference { flex: 0 1 160px; }
.tag { list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: 6px; max-height: 220px; overflow: auto; }
.tag code { font-size: 11.5px; padding: 1px 7px; border-radius: 999px; border: 1px solid var(--border); }

.variables { width: 100%; border-collapse: collapse; font-size: 13px; }
.variables td { padding: 6px 8px; border-top: 1px solid var(--border-soft); vertical-align: middle; }
.variables .value { overflow-wrap: anywhere; }
.variables em { font-size: 11px; }
.row-actions { display: flex; gap: 6px; justify-content: flex-end; }
.new-variable { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.4fr) auto auto auto; gap: 8px; align-items: center; }

@media (max-width: 720px) {
  .fields, .registry, .new-variable { grid-template-columns: 1fr; }
}
</style>

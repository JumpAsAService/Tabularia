<script setup lang="ts">
// The node panel of a Container node: the image (a registry, a repository, a tag, among
// those an administrator granted), the command and arguments, the variables, CPU and
// memory, and the «Run» of the saved flow. Input and output come from its edges, as on
// the Script node: through the scripts bucket, as TABULARIA_INPUT and TABULARIA_OUTPUT.
// The image's own configuration, read from the registry, guides the rest: what it runs
// by default, the variables it declares, its platforms and labels. In a flow group
// (`inGroup`) the same panel sets a Container cell: no edges, no «Run» of a flow.
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { CheckCircle2, Container, Info, KeyRound, LoaderCircle, Play, Plus, RefreshCw, Trash2, TriangleAlert, X, XCircle } from 'lucide-vue-next'
import type { Node } from '@vue-flow/core'
import { useRegistriesEditor, type ImageConfig } from '~/composables/useRegistries'
import type { ScriptOutputs } from '~/composables/useScripting'
import { useFormat } from '~/composables/useFormat'
import { errMessage } from '~/composables/useApi'
import Select from '~/components/ui/Select.vue'

const props = defineProps<{
  node: Node
  readOnly?: boolean
  outputs?: ScriptOutputs[string] | null
  trial?: { runId: number | null; status: string; startedAt: number; error: string } | null
  flowDirty?: boolean
  inGroup?: boolean
}>()
const emit = defineEmits<{
  (e: 'update', patch: Record<string, any>): void
  (e: 'delete'): void
  (e: 'run'): void
}>()
const fmt = useFormat()
const { t } = useI18n()
const registries = useRegistriesEditor()
onMounted(() => { registries.load() })

const data = computed(() => props.node.data ?? {})
const state = computed(() => registries.state.value)
const registry = computed(() => state.value?.registries.find((r) => r.id === data.value.registryId) ?? null)
const options = computed(() => (state.value?.registries ?? []).map((r) => ({ value: r.id, label: r.name, hint: r.host })))
// what may be typed: the exact names are suggested, the patterns (team/*) said
const exact = computed(() => (registry.value?.repositories ?? []).filter((x) => !x.includes('*')))
const patterns = computed(() => (registry.value?.repositories ?? []).filter((x) => x.includes('*')))
const listId = computed(() => `container-repos-${props.node.id}`)
const tagListId = computed(() => `container-tags-${props.node.id}`)

function pickRegistry(id: number | null) {
  const r = state.value?.registries.find((x) => x.id === id)
  if (!r) return
  const repository = r.repositories.find((x) => !x.includes('*')) ?? ''
  emit('update', { registryId: r.id, registryName: r.name, repository, tag: '' })
  tags.value = null
}

// the tags of the repository, read from the registry when asked (or when the image changes)
const tags = ref<string[] | null>(null)
const tagsError = ref('')
const loadingTags = ref(false)
async function loadTags() {
  const repository = (data.value.repository ?? '').trim()
  if (!registry.value || !repository) return
  loadingTags.value = true
  tagsError.value = ''
  try {
    tags.value = (await registries.tags(registry.value.id, repository)).tags
  } catch (e) {
    tags.value = null
    tagsError.value = errMessage(e)
  } finally {
    loadingTags.value = false
  }
}
watch([() => data.value.registryId, () => data.value.repository], () => { tags.value = null; tagsError.value = '' })

// the image's own configuration, read when the image or the tag changes
const image = ref<ImageConfig | null>(null)
const imageError = ref('')
const loadingImage = ref(false)
let imageSeq = 0
async function loadImage() {
  const repository = (data.value.repository ?? '').trim()
  image.value = null
  imageError.value = ''
  if (!registry.value || !repository) return
  const seq = ++imageSeq
  loadingImage.value = true
  try {
    const got = await registries.image(registry.value.id, repository, (data.value.tag ?? '').trim())
    if (seq === imageSeq) image.value = got
  } catch (e) {
    if (seq === imageSeq) imageError.value = errMessage(e)
  } finally {
    if (seq === imageSeq) loadingImage.value = false
  }
}
watch([() => registry.value?.id, () => data.value.repository, () => data.value.tag], loadImage, { immediate: true })
const runsByDefault = computed(() => [...(image.value?.entrypoint ?? []), ...(image.value?.cmd ?? [])].join(' '))
const LABEL_KEYS = ['org.opencontainers.image.title', 'org.opencontainers.image.description', 'org.opencontainers.image.version']
const mainLabels = computed(() => LABEL_KEYS.map((k) => image.value?.labels[k]).filter(Boolean) as string[])
// what the image declares, offered as variables of the node (its own value is the default)
const offered = computed(() => (image.value?.env ?? []).filter((v) => v.name !== 'PATH' && !/^(TABULARIA_|AWS_)/.test(v.name)
  && /^[A-Z_][A-Z0-9_]*$/.test(v.name)
  && !variables.value.some((x) => x.name === v.name) && !registry.value?.variables.some((x) => x.name === v.name)))
function offerVariable(v: { name: string; value: string }) {
  emit('update', { variables: [...variables.value, { name: v.name, value: v.value }] })
}
function useDefaults() {
  if (!image.value) return
  emit('update', { command: [...image.value.entrypoint], args: [...image.value.cmd] })
}
function size(n: number | null) {
  if (n == null) return ''
  return n >= 2 ** 30 ? `${(n / 2 ** 30).toFixed(1)} GB` : n >= 2 ** 20 ? `${(n / 2 ** 20).toFixed(0)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`
}

// command and arguments: one per line, as Kubernetes takes them (no shell splitting)
function lines(value: unknown): string {
  return Array.isArray(value) ? value.join('\n') : ''
}
function setLines(key: 'command' | 'args', text: string) {
  emit('update', { [key]: text.split('\n').map((x) => x.replace(/\r$/, '')).filter((x) => x.trim() !== '') })
}

// the node's variables; the registry's are the administrators', shown and not editable
const variables = computed<{ name: string; value: string }[]>(() => data.value.variables ?? [])
const newName = ref('')
const newValue = ref('')
const nameError = computed(() => {
  const n = newName.value.trim()
  if (!n) return ''
  if (!/^[A-Z_][A-Z0-9_]*$/.test(n)) return t('containers.node.badVariableName')
  if (/^(TABULARIA_|AWS_)/.test(n)) return t('containers.node.reservedVariable')
  if (variables.value.some((v) => v.name === n) || registry.value?.variables.some((v) => v.name === n)) {
    return t('containers.node.variableTaken')
  }
  return ''
})
function addVariable() {
  const name = newName.value.trim()
  if (!name || nameError.value) return
  emit('update', { variables: [...variables.value, { name, value: newValue.value }] })
  newName.value = ''
  newValue.value = ''
}
function setVariable(i: number, value: string) {
  emit('update', { variables: variables.value.map((v, j) => (j === i ? { ...v, value } : v)) })
}
function removeVariable(i: number) {
  emit('update', { variables: variables.value.filter((_, j) => j !== i) })
}

function numberField(key: string, value: string, min: number, max: number) {
  const n = Math.round(Number(value))
  if (!Number.isFinite(n)) return
  emit('update', { [key]: Math.min(max, Math.max(min, n)) })
}
function quantity(key: 'cpu' | 'memory', value: string) {
  emit('update', { [key]: value.trim() })
}

// «Run»: the trial in progress, its seconds, and what the last run of the node left
const running = computed(() => !!props.trial && (props.trial.status === 'PENDING' || props.trial.status === 'STARTED'))
const now = ref(Date.now())
let ticker: ReturnType<typeof setInterval> | undefined
watch(running, (on) => {
  clearInterval(ticker)
  if (on) ticker = setInterval(() => { now.value = Date.now() }, 1000)
}, { immediate: true })
onBeforeUnmount(() => clearInterval(ticker))
const seconds = computed(() => (props.trial ? Math.max(0, Math.round((now.value - props.trial.startedAt) / 1000)) : 0))
const last = computed(() => props.outputs?.last ?? null)
// the trial's own message only when its run is not the last one shown (it never started,
// or the outputs are not read yet): otherwise the last run says it, with its log
const failedEarly = computed(() => props.trial?.status === 'FAILURE' && !!props.trial.error
  && (props.trial.runId == null || last.value?.run_id !== props.trial.runId))
const timeoutMax = computed(() => state.value?.limits.max_timeout_seconds ?? 86400)

const unavailable = computed(() => {
  const s = state.value
  if (!s) return ''
  if (s.disabled_reason) return t('containers.node.off')
  if (data.value.registryId != null && !registry.value) return t('containers.node.registryGone')
  return ''
})
const canRun = computed(() => !props.readOnly && !!registry.value && !!(data.value.repository ?? '').trim() && !unavailable.value)
</script>

<template>
  <div class="container-panel">
    <div class="row">
      <h3><Container :size="15" /> {{ $t('containers.node.title') }}</h3>
      <button v-if="!readOnly" class="del" :title="$t('containers.node.delete')" @click="emit('delete')"><Trash2 :size="14" /></button>
    </div>
    <p class="muted outhint">{{ inGroup ? $t('containers.node.groupHint') : $t('containers.node.hint') }}</p>
    <p v-if="unavailable" class="warn" role="note"><TriangleAlert :size="13" /> {{ unavailable }}</p>

    <label>{{ $t('containers.node.name') }}</label>
    <input
      type="text"
      :value="data.label || ''"
      maxlength="80"
      :disabled="readOnly"
      @change="emit('update', { label: ($event.target as HTMLInputElement).value.trim() || t('containers.node.title') })"
    />

    <label>{{ $t('containers.node.registry') }}</label>
    <Select
      :model-value="data.registryId ?? null"
      :options="options"
      :placeholder="$t('containers.node.chooseRegistry')"
      :disabled="readOnly"
      @update:model-value="pickRegistry"
    />

    <template v-if="registry">
      <div class="image">
        <label>
          <span>{{ $t('containers.node.repository') }}</span>
          <input
            type="text"
            :value="data.repository ?? ''"
            :list="listId"
            spellcheck="false"
            autocomplete="off"
            :disabled="readOnly"
            :placeholder="exact[0] ?? 'team/forecast'"
            @change="emit('update', { repository: ($event.target as HTMLInputElement).value.trim(), tag: '' })"
          />
          <datalist :id="listId"><option v-for="x in exact" :key="x" :value="x" /></datalist>
        </label>
        <label class="tag">
          <span>{{ $t('containers.node.tag') }}</span>
          <span class="tag-row">
            <input
              type="text"
              :value="data.tag ?? ''"
              :list="tagListId"
              spellcheck="false"
              autocomplete="off"
              placeholder="latest"
              :disabled="readOnly"
              @focus="tags === null && !loadingTags && loadTags()"
              @change="emit('update', { tag: ($event.target as HTMLInputElement).value.trim() })"
            />
            <button type="button" class="mini" :title="$t('containers.node.readTags')" :aria-label="$t('containers.node.readTags')"
                    :disabled="loadingTags || !(data.repository ?? '').trim()" @click="loadTags">
              <LoaderCircle v-if="loadingTags" :size="13" class="spin" /><RefreshCw v-else :size="13" />
            </button>
          </span>
          <datalist :id="tagListId"><option v-for="x in tags ?? []" :key="x" :value="x" /></datalist>
        </label>
      </div>
      <p v-if="patterns.length" class="muted outhint">{{ $t('containers.node.patterns', { list: patterns.join(', ') }) }}</p>
      <p v-if="tagsError" class="warn"><TriangleAlert :size="13" /> {{ tagsError }}</p>
      <p v-else-if="tags && !tags.length" class="muted outhint">{{ $t('containers.node.noTags') }}</p>

      <div v-if="loadingImage || image || imageError" class="guide" aria-live="polite">
        <div class="guide-head"><Info :size="13" /> {{ $t('containers.node.imageTitle') }}</div>
        <p v-if="loadingImage" class="muted outhint"><LoaderCircle :size="12" class="spin" /> {{ $t('containers.node.imageReading') }}</p>
        <p v-else-if="imageError" class="warn"><TriangleAlert :size="13" /> {{ imageError }}</p>
        <template v-else-if="image">
          <p v-if="mainLabels.length" class="guide-labels">{{ mainLabels.join(' · ') }}</p>
          <dl class="guide-facts">
            <dt>{{ $t('containers.node.imageRuns') }}</dt>
            <dd><code>{{ runsByDefault || '—' }}</code></dd>
            <template v-if="image.platforms.length">
              <dt>{{ $t('containers.node.imagePlatforms') }}</dt><dd>{{ image.platforms.join(', ') }}</dd>
            </template>
            <template v-if="image.created">
              <dt>{{ $t('containers.node.imageCreated') }}</dt><dd>{{ fmt.ago(image.created) }}<template v-if="image.size"> · {{ size(image.size) }}</template></dd>
            </template>
            <template v-if="image.working_dir || image.user">
              <dt>{{ $t('containers.node.imageWhere') }}</dt><dd><code>{{ image.working_dir ?? '/' }}</code><template v-if="image.user"> · {{ $t('containers.node.imageUser', { user: image.user }) }}</template></dd>
            </template>
          </dl>
          <button v-if="!readOnly && (image.entrypoint.length || image.cmd.length)" type="button" class="mini" @click="useDefaults">
            {{ $t('containers.node.imageUseDefaults') }}
          </button>
          <div v-if="offered.length && !readOnly" class="guide-env">
            <span class="muted outhint">{{ $t('containers.node.imageDeclares') }}</span>
            <button v-for="v in offered" :key="v.name" type="button" class="chip" :title="$t('containers.node.imageAddVariable', { name: v.name })" @click="offerVariable(v)">
              <Plus :size="11" /> {{ v.name }}<span v-if="v.value" class="muted">={{ v.value }}</span>
            </button>
          </div>
        </template>
      </div>
    </template>

    <label>{{ $t('containers.node.command') }}</label>
    <textarea
      :value="lines(data.command)"
      rows="2"
      spellcheck="false"
      class="mono"
      :disabled="readOnly"
      :placeholder="$t('containers.node.commandPlaceholder')"
      @change="setLines('command', ($event.target as HTMLTextAreaElement).value)"
    />
    <label>{{ $t('containers.node.args') }}</label>
    <textarea
      :value="lines(data.args)"
      rows="3"
      spellcheck="false"
      class="mono"
      :disabled="readOnly"
      :placeholder="$t('containers.node.argsPlaceholder')"
      @change="setLines('args', ($event.target as HTMLTextAreaElement).value)"
    />
    <p class="muted outhint">{{ $t('containers.node.commandHint') }}</p>

    <div v-if="!inGroup" class="trial">
      <button type="button" :disabled="!canRun || running" @click="emit('run')">
        <LoaderCircle v-if="running" :size="14" class="spin" /><Play v-else :size="14" />
        {{ running ? $t('containers.node.running', { s: seconds }) : flowDirty ? $t('containers.node.saveAndRun') : $t('containers.node.run') }}
      </button>
      <p class="muted outhint">{{ $t('containers.node.runHint') }}</p>
      <!-- a trial that failed before its run said anything (refused, unreachable): only its message -->
      <p v-if="failedEarly" class="outcome ko" role="alert"><XCircle :size="13" /> {{ trial!.error }}</p>
      <template v-if="last && !running">
        <p class="outcome" :class="last.status === 'SUCCESS' ? 'ok' : 'ko'">
          <component :is="last.status === 'SUCCESS' ? CheckCircle2 : XCircle" :size="13" />
          {{ $t(last.trial ? 'containers.node.lastTrial' : 'containers.node.lastRun', { when: fmt.ago(last.finished_at ?? last.started_at), status: last.status }) }}
          <span v-if="outputs?.output?.rows != null && last.status === 'SUCCESS'">· {{ $t('containers.node.rows', { n: fmt.count(outputs.output.rows) }) }}</span>
        </p>
        <p v-if="last.status !== 'SUCCESS' && last.error" class="muted outhint error">{{ last.error }}</p>
        <p v-if="last.stale" class="warn"><TriangleAlert :size="13" /> {{ $t('containers.node.changed') }}</p>
        <details v-if="last.log">
          <summary>{{ $t('containers.node.log') }}</summary>
          <pre class="log">{{ last.log }}</pre>
        </details>
        <p v-else-if="last.log_expired" class="muted outhint">{{ $t('scripting.run.logExpired') }}</p>
      </template>
    </div>

    <fieldset class="vars">
      <legend>{{ $t('containers.node.variables') }}</legend>
      <ul v-if="registry?.variables.length || variables.length">
        <li v-for="v in registry?.variables ?? []" :key="`r-${v.name}`" class="fixed" :title="$t('containers.node.setByRegistry')">
          <code>{{ v.name }}</code>
          <span v-if="v.secret" class="muted"><KeyRound :size="11" /> {{ $t('containers.node.secret') }}</span>
          <span v-else class="muted value">{{ v.value }}</span>
        </li>
        <li v-for="(v, i) in variables" :key="`n-${v.name}`">
          <code>{{ v.name }}</code>
          <input type="text" :value="v.value" :disabled="readOnly" :aria-label="v.name"
                 @change="setVariable(i, ($event.target as HTMLInputElement).value)" />
          <button v-if="!readOnly" type="button" class="del" :title="$t('containers.node.removeVariable')"
                  :aria-label="$t('containers.node.removeVariable')" @click="removeVariable(i)"><X :size="13" /></button>
        </li>
      </ul>
      <form v-if="!readOnly" class="new-var" @submit.prevent="addVariable">
        <input v-model="newName" type="text" placeholder="NAME" spellcheck="false" autocomplete="off"
               :aria-label="$t('containers.node.variableName')" :aria-invalid="!!nameError" />
        <input v-model="newValue" type="text" :placeholder="$t('containers.node.variableValue')"
               :aria-label="$t('containers.node.variableValue')" />
        <button type="submit" class="mini" :disabled="!newName.trim() || !!nameError" :title="$t('containers.node.addVariable')"
                :aria-label="$t('containers.node.addVariable')"><Plus :size="13" /></button>
      </form>
      <p v-if="nameError" class="warn">{{ nameError }}</p>
      <p class="muted outhint">{{ $t('containers.node.variablesHint', { list: (state?.tabularia_variables ?? []).join(', ') }) }}</p>
    </fieldset>

    <div class="numbers">
      <label>
        <span>{{ $t('containers.node.cpu') }}</span>
        <input type="text" :value="data.cpu ?? '1'" :disabled="readOnly" spellcheck="false"
               @change="quantity('cpu', ($event.target as HTMLInputElement).value)" />
      </label>
      <label>
        <span>{{ $t('containers.node.memory') }}</span>
        <input type="text" :value="data.memory ?? '1Gi'" :disabled="readOnly" spellcheck="false"
               @change="quantity('memory', ($event.target as HTMLInputElement).value)" />
      </label>
    </div>
    <p v-if="state" class="muted outhint">{{ $t('containers.node.limits', { cpu: state.limits.max_cpu, memory: state.limits.max_memory }) }}</p>
    <div class="numbers three">
      <label>
        <span>{{ $t('containers.node.timeout') }}</span>
        <input type="number" min="10" :max="timeoutMax" :value="data.timeoutSeconds ?? 900" :disabled="readOnly"
               @change="numberField('timeoutSeconds', ($event.target as HTMLInputElement).value, 10, timeoutMax)" />
      </label>
      <label>
        <span>{{ $t('containers.node.retries') }}</span>
        <input type="number" min="0" max="5" :value="data.retries ?? 0" :disabled="readOnly"
               @change="numberField('retries', ($event.target as HTMLInputElement).value, 0, 5)" />
      </label>
      <label>
        <span>{{ $t('containers.node.retryDelay') }}</span>
        <input type="number" min="0" max="3600" :value="data.retryDelaySeconds ?? 60" :disabled="readOnly || !(data.retries > 0)"
               @change="numberField('retryDelaySeconds', ($event.target as HTMLInputElement).value, 0, 3600)" />
      </label>
    </div>
    <p class="muted outhint">{{ state && !state.storage_ready ? $t('containers.node.noBucket') : $t('containers.node.ioHint') }}</p>
  </div>
</template>

<style scoped>
.container-panel { display: flex; flex-direction: column; gap: 8px; }
.row { display: flex; align-items: center; gap: 8px; }
.row h3 { flex: 1; margin: 0; display: inline-flex; align-items: center; gap: 7px; font-size: 14px; }
.del { border: 0; background: none; color: var(--muted); cursor: pointer; padding: 4px; border-radius: 6px; }
.del:hover { color: var(--danger); background: var(--bg-soft); }
label { font-size: 12px; color: var(--muted); }
.outhint { margin: 0; font-size: 12px; line-height: 1.5; }
.warn { margin: 0; display: flex; gap: 6px; align-items: flex-start; font-size: 12px; color: var(--warning); }
.mono { font-family: var(--font-mono, ui-monospace, monospace); font-size: 12px; resize: vertical; }
.image { display: grid; grid-template-columns: minmax(0, 1fr) 140px; gap: 8px; }
.image label { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.image input { min-width: 0; }
.tag-row { display: flex; gap: 4px; }
.tag-row input { flex: 1; }
.mini { display: inline-flex; align-items: center; justify-content: center; padding: 4px 8px; }
.trial { display: flex; flex-direction: column; gap: 6px; }
.trial button { display: inline-flex; align-items: center; justify-content: center; gap: 6px; }
.outcome { margin: 0; display: flex; gap: 6px; align-items: flex-start; flex-wrap: wrap; font-size: 12px; }
.outcome.ok svg { color: var(--accent-2); }
.outcome.ko, .outcome.ko svg { color: var(--danger); }
.error { overflow-wrap: anywhere; }
details summary { cursor: pointer; font-size: 12px; color: var(--muted); }
.log { margin: 6px 0 0; max-height: 220px; overflow: auto; font-size: 11px; padding: 6px; border-radius: 6px; background: var(--bg-soft); white-space: pre-wrap; overflow-wrap: anywhere; }
.vars { margin: 0; padding: 8px 10px 10px; border: 1px solid var(--border-soft); border-radius: 8px; display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.vars legend { font-size: 12px; color: var(--muted); padding: 0 4px; }
.vars ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
.vars li { display: grid; grid-template-columns: minmax(0, 40%) minmax(0, 1fr) auto; gap: 6px; align-items: center; font-size: 12px; }
.vars li.fixed { grid-template-columns: minmax(0, 40%) minmax(0, 1fr); }
.vars li code { overflow: hidden; text-overflow: ellipsis; }
.vars .value { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.vars li input { min-width: 0; }
.new-var { display: grid; grid-template-columns: minmax(0, 40%) minmax(0, 1fr) auto; gap: 6px; }
.new-var input { min-width: 0; font-size: 12px; }
.guide { display: flex; flex-direction: column; gap: 6px; padding: 9px 10px; border: 1px dashed var(--border); border-radius: 8px; background: var(--bg-soft); }
.guide-head { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 600; }
.guide-labels { margin: 0; font-size: 12px; }
.guide-facts { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 3px 10px; margin: 0; font-size: 12px; }
.guide-facts dt { color: var(--muted); }
.guide-facts dd { margin: 0; overflow-wrap: anywhere; }
.guide-env { display: flex; flex-wrap: wrap; gap: 5px; align-items: center; }
.chip { display: inline-flex; align-items: center; gap: 3px; font-size: 11.5px; padding: 2px 7px; border-radius: 999px; }
.numbers { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
.numbers.three { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.numbers label { display: flex; flex-direction: column; gap: 4px; }
.numbers input { min-width: 0; }
</style>

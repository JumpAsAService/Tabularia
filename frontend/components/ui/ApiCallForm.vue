<script setup lang="ts">
// The call of an «API call» datasource, inside the datasource dialog: what it asks
// (method, path, query, headers, JSON body, list and range variables), how the answer becomes
// rows (records path, keyed records, JMESPath, a list exploded), how it pages, how
// a refresh lands (snapshot or append with keys) and its limits. «Try the call»
// makes the first page of the first call with the connection's credentials and
// shows the request as sent (secrets masked), the status, an excerpt of the answer
// and the first rows — before saving.
//
// Query, headers and variables are edited as text, one per line: compact, and
// pasted easily from an API's documentation.
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { FlaskConical, LoaderCircle, CheckCircle2, XCircle } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useConnections, type ApiTryResult } from '~/composables/useConnections'
import { cleanApiCall, type ApiCallDraft, type ApiPair } from '~/composables/useDatasources'
import { splitKeys, usePlaceholderPreview } from '~/composables/usePlaceholders'
import DynamicSourceFields from '~/components/ui/DynamicSourceFields.vue'
import TemplatePreview from '~/components/ui/TemplatePreview.vue'

const props = defineProps<{ connectionId: number | null }>()
const call = defineModel<ApiCallDraft>({ required: true })

const { t } = useI18n()
const connApi = useConnections()

function pairsToText(pairs: ApiPair[], sep: string): string {
  return pairs.map((p) => `${p.name}${sep}${p.value}`).join('\n')
}
function textToPairs(text: string, sep: string): ApiPair[] {
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const i = line.indexOf(sep.trim())
      return i < 0 ? { name: line, value: '' } : { name: line.slice(0, i).trim(), value: line.slice(i + sep.trim().length).trim() }
    })
}

const queryText = ref(pairsToText(call.value.query, '='))
const headersText = ref(pairsToText(call.value.headers, ': '))
const keysText = ref(call.value.keys.join(', '))

watch(queryText, (v) => { call.value.query = textToPairs(v, '=') })
watch(headersText, (v) => { call.value.headers = textToPairs(v, ': ') })
watch(keysText, (v) => { call.value.keys = splitKeys(v) })

// what the path and each query value give now, with the call's zone and variables
const preview = usePlaceholderPreview(() => ({
  kind: 'input',
  templates: { path: call.value.path, ...Object.fromEntries(call.value.query.map((p, i) => [`query_${i}`, p.value])) },
  timezone: call.value.timezone,
  variables: call.value.variables,
  last_refresh_initial: call.value.last_refresh_initial,
}))

const pg = computed(() => call.value.pagination)

// ── «Try the call» ────────────────────────────────────────────────────────────
const trying = ref(false)
const tried = ref<ApiTryResult | null>(null)
const tryError = ref('')
let trySeq = 0
async function tryCall() {
  if (props.connectionId == null || trying.value) return
  const seq = ++trySeq
  trying.value = true
  tried.value = null
  tryError.value = ''
  try {
    const res = await connApi.tryApi(props.connectionId, cleanApiCall(call.value))
    if (seq === trySeq) tried.value = res
  } catch (e) {
    if (seq === trySeq) tryError.value = errMessage(e)
  } finally {
    if (seq === trySeq) trying.value = false
  }
}
// an answer for another call says nothing about this one
watch(call, () => { tried.value = null; tryError.value = '' }, { deep: true })
watch(() => props.connectionId, () => { tried.value = null; tryError.value = '' })

const cell = (v: unknown) => (v === null || v === undefined ? '' : typeof v === 'object' ? JSON.stringify(v) : String(v))
</script>

<template>
  <div class="ac">
    <div class="ac-line">
      <div class="ac-method">
        <label>{{ $t('apiSource.methodLabel') }}</label>
        <Select v-model="call.method" :options="['GET', 'POST']" />
      </div>
      <div class="ac-grow">
        <label>{{ $t('apiSource.pathLabel') }} <span class="ac-soft">{{ $t('apiSource.pathHint') }}</span></label>
        <input v-model="call.path" type="text" spellcheck="false" class="ac-mono ac-path" aria-describedby="tp-api-path" placeholder="/v1/rates/{today-30}.." />
      </div>
    </div>
    <TemplatePreview id="tp-api-path" :preview="preview" name="path" />

    <label>{{ $t('apiSource.queryLabel') }} <span class="ac-soft">{{ $t('apiSource.queryHint') }}</span></label>
    <textarea v-model="queryText" rows="2" spellcheck="false" class="ac-mono ac-query" placeholder="base=EUR&#10;from={today-7d}" />
    <TemplatePreview v-for="(p, i) in call.query" :key="i" :preview="preview" :name="`query_${i}`" :label="p.name" />

    <label>{{ $t('apiSource.headersLabel') }} <span class="ac-soft">{{ $t('apiSource.headersHint') }}</span></label>
    <textarea v-model="headersText" rows="1" spellcheck="false" class="ac-mono" placeholder="Accept: application/json" />

    <template v-if="call.method === 'POST'">
      <label>{{ $t('apiSource.bodyLabel') }} <span class="ac-soft">{{ $t('apiSource.bodyHint') }}</span></label>
      <textarea v-model="call.body" rows="3" spellcheck="false" class="ac-mono" placeholder='{"since": "{last_refresh}"}' />
    </template>

    <DynamicSourceFields v-model="call" :preview="preview" :with-mode="false" />

    <fieldset class="ac-box">
      <legend>{{ $t('apiSource.responseTitle') }}</legend>
      <div class="ac-two">
        <div>
          <label>{{ $t('apiSource.formatLabel') }}</label>
          <Select v-model="call.response.format" :options="[{ value: 'json', label: 'JSON' }, { value: 'ndjson', label: 'NDJSON' }, { value: 'csv', label: 'CSV' }]" />
        </div>
        <div v-if="call.response.format === 'json'">
          <label>{{ $t('apiSource.recordsPathLabel') }}</label>
          <input v-model="call.response.records_path" type="text" spellcheck="false" class="ac-mono" placeholder="data.items" />
        </div>
        <div v-else-if="call.response.format === 'csv'">
          <label>{{ $t('apiSource.separatorLabel') }}</label>
          <input v-model="call.response.csv_separator" type="text" maxlength="1" class="ac-mono ac-sep" />
        </div>
      </div>
      <p v-if="call.response.format === 'json'" class="ac-soft ac-note">{{ $t('apiSource.recordsPathHint') }}</p>
      <label v-if="call.response.format === 'json'" class="ac-check">
        <input v-model="call.response.keyed" type="checkbox" /> {{ $t('apiSource.keyedLabel') }}
      </label>
      <details v-if="call.response.format === 'json'" class="ac-more">
        <summary>{{ $t('apiSource.advancedTitle') }}</summary>
        <label>{{ $t('apiSource.jmespathLabel') }} <span class="ac-soft">{{ $t('apiSource.jmespathHint') }}</span></label>
        <input v-model="call.response.jmespath" type="text" spellcheck="false" class="ac-mono" placeholder="results[].quote" />
        <label>{{ $t('apiSource.explodeLabel') }} <span class="ac-soft">{{ $t('apiSource.explodeHint') }}</span></label>
        <input v-model="call.response.explode" type="text" spellcheck="false" class="ac-mono" placeholder="lines" />
      </details>
    </fieldset>

    <fieldset class="ac-box">
      <legend>{{ $t('apiSource.paginationTitle') }}</legend>
      <Select
        v-model="call.pagination.type"
        :options="[
          { value: 'none', label: $t('apiSource.pagNone') },
          { value: 'page', label: $t('apiSource.pagPage') },
          { value: 'offset', label: $t('apiSource.pagOffset') },
          { value: 'cursor', label: $t('apiSource.pagCursor') },
          { value: 'next_link', label: $t('apiSource.pagNext') },
        ]"
      />
      <div v-if="pg.type === 'page'" class="ac-two">
        <div><label>{{ $t('apiSource.pageParamLabel') }}</label><input v-model="pg.param" type="text" class="ac-mono" placeholder="page" /></div>
        <div><label>{{ $t('apiSource.startLabel') }}</label><input v-model.number="pg.start" type="number" min="0" placeholder="1" /></div>
      </div>
      <div v-else-if="pg.type === 'offset'" class="ac-three">
        <div><label>{{ $t('apiSource.offsetParamLabel') }}</label><input v-model="pg.param" type="text" class="ac-mono" placeholder="offset" /></div>
        <div><label>{{ $t('apiSource.limitParamLabel') }}</label><input v-model="pg.limit_param" type="text" class="ac-mono" placeholder="limit" /></div>
        <div><label>{{ $t('apiSource.limitLabel') }}</label><input v-model.number="pg.limit" type="number" min="1" /></div>
      </div>
      <div v-else-if="pg.type === 'cursor'" class="ac-two">
        <div><label>{{ $t('apiSource.cursorParamLabel') }}</label><input v-model="pg.param" type="text" class="ac-mono" placeholder="cursor" /></div>
        <div><label>{{ $t('apiSource.cursorPathLabel') }}</label><input v-model="pg.cursor_path" type="text" class="ac-mono" placeholder="meta.next_cursor" /></div>
      </div>
      <div v-else-if="pg.type === 'next_link'">
        <label>{{ $t('apiSource.nextPathLabel') }} <span class="ac-soft">{{ $t('apiSource.nextPathHint') }}</span></label>
        <input v-model="pg.next_path" type="text" class="ac-mono" placeholder="links.next" />
      </div>
    </fieldset>

    <fieldset class="ac-box">
      <legend>{{ $t('apiSource.refreshTitle') }}</legend>
      <Select
        v-model="call.mode"
        :options="[
          { value: 'snapshot', label: $t('apiSource.modeSnapshot') },
          { value: 'append', label: $t('apiSource.modeAppend') },
        ]"
      />
      <template v-if="call.mode === 'append'">
        <label>{{ $t('apiSource.keysLabel') }} <span class="ac-soft">{{ $t('apiSource.keysHint') }}</span></label>
        <input v-model="keysText" type="text" spellcheck="false" class="ac-mono ac-keys" placeholder="_key, _param_symbol" />
      </template>
      <details class="ac-more">
        <summary>{{ $t('apiSource.limitsTitle') }}</summary>
        <p class="ac-soft ac-note">{{ $t('apiSource.limitsHint') }}</p>
        <div class="ac-two">
          <div><label>{{ $t('apiSource.maxPagesLabel') }}</label><input v-model.number="call.limits.max_pages" type="number" min="0" /></div>
          <div><label>{{ $t('apiSource.maxCallsLabel') }}</label><input v-model.number="call.limits.max_calls" type="number" min="0" /></div>
          <div><label>{{ $t('apiSource.maxRowsLabel') }}</label><input v-model.number="call.limits.max_rows" type="number" min="0" /></div>
          <div><label>{{ $t('apiSource.minIntervalLabel') }}</label><input v-model.number="call.limits.min_interval_seconds" type="number" min="0" step="0.1" /></div>
        </div>
      </details>
    </fieldset>

    <div class="ac-try">
      <button type="button" :disabled="trying || connectionId == null" @click="tryCall">
        <LoaderCircle v-if="trying" :size="13" class="spin" /><FlaskConical v-else :size="13" /> {{ $t('apiSource.tryButton') }}
      </button>
      <span class="ac-soft">{{ $t('apiSource.tryHint') }}</span>
    </div>
    <p v-if="tryError" class="ac-err" role="alert">{{ tryError }}</p>
    <div v-else-if="tried" class="ac-result" role="status">
      <p class="ac-req ac-mono"><strong>{{ tried.request.method }}</strong> {{ tried.request.url }}</p>
      <ul v-if="Object.keys(tried.request.headers).length" class="ac-headers ac-mono">
        <li v-for="(v, k) in tried.request.headers" :key="k">{{ k }}: {{ v }}</li>
      </ul>
      <pre v-if="tried.request.body" class="ac-pre">{{ tried.request.body }}</pre>
      <p class="ac-status" :class="tried.ok ? 'ok' : 'ko'">
        <CheckCircle2 v-if="tried.ok" :size="13" /><XCircle v-else :size="13" />
        <span>
          <template v-if="tried.status">{{ $t('apiSource.tryStatus') }} {{ tried.status }}</template>
          · {{ $t('apiSource.tryCalls', { n: tried.calls }) }}
          <template v-if="tried.next"> · {{ $t('apiSource.tryNext') }}: <code>{{ tried.next }}</code></template>
        </span>
      </p>
      <p v-if="tried.error" class="ac-err">{{ tried.error }}</p>
      <template v-if="tried.ok">
        <p class="ac-soft">{{ tried.rows?.length ? $t('apiSource.tryRows', { n: tried.records ?? 0 }) : $t('apiSource.tryNoRows') }}</p>
        <div v-if="tried.rows?.length" class="ac-table">
          <table>
            <thead><tr><th v-for="c in tried.columns" :key="c">{{ c }}</th></tr></thead>
            <tbody>
              <tr v-for="(r, i) in tried.rows" :key="i"><td v-for="c in tried.columns" :key="c">{{ cell(r[c]) }}</td></tr>
            </tbody>
          </table>
        </div>
      </template>
      <details v-if="tried.excerpt" class="ac-more" :open="!tried.ok">
        <summary>{{ $t('apiSource.tryAnswer') }} <span v-if="tried.content_type" class="ac-soft">({{ tried.content_type }})</span></summary>
        <pre class="ac-pre">{{ tried.excerpt }}</pre>
      </details>
    </div>
    <p class="ac-soft ac-note">{{ $t('apiSource.machineNote') }}</p>
  </div>
</template>

<style scoped>
.ac { display: flex; flex-direction: column; gap: 6px; }
.ac label { font-size: 12px; color: var(--muted); }
.ac-soft { opacity: 0.7; font-size: 12px; }
.ac-note { margin: 0; }
.ac-mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; }
.ac textarea { resize: vertical; }
.ac-line { display: flex; gap: 8px; align-items: flex-end; }
.ac-line > div { display: flex; flex-direction: column; gap: 3px; }
.ac-method { width: 6.5em; flex: none; }
.ac-grow { flex: 1; min-width: 0; }
.ac-two, .ac-three { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 10px; }
.ac-three { grid-template-columns: 1fr 1fr 1fr; }
.ac-two > div, .ac-three > div { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
@media (max-width: 520px) { .ac-two, .ac-three { grid-template-columns: 1fr; } }
.ac-sep { width: 4.5em; text-align: center; }
.ac-box { border: 1px solid var(--border-soft); border-radius: 8px; padding: 6px 10px 8px; margin: 2px 0 0; display: flex; flex-direction: column; gap: 5px; min-width: 0; }
.ac-box legend { font-size: 12px; color: var(--muted); padding: 0 4px; }
.ac-check { display: inline-flex; align-items: flex-start; gap: 6px; }
.ac-check input { margin-top: 2px; }
.ac-more summary { cursor: pointer; font-size: 12px; color: var(--muted); }
.ac-more[open] { display: flex; flex-direction: column; gap: 4px; }
.ac-try { display: flex; align-items: center; gap: 10px; margin-top: 4px; flex-wrap: wrap; }
.ac-try button { display: inline-flex; align-items: center; gap: 6px; }
.ac-result { border: 1px solid var(--border-soft); border-radius: 8px; background: var(--bg-soft); padding: 8px 10px; display: flex; flex-direction: column; gap: 5px; font-size: 12px; min-width: 0; }
.ac-result p { margin: 0; }
.ac-req { overflow-wrap: anywhere; }
.ac-headers { list-style: none; margin: 0; padding: 0; font-size: 11.5px; color: var(--muted); overflow-wrap: anywhere; }
.ac-pre { margin: 0; max-height: 160px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 11.5px; }
.ac-status { display: flex; align-items: flex-start; gap: 6px; }
.ac-status svg { flex: none; margin-top: 1px; }
.ac-status.ok { color: var(--accent-2); }
.ac-status.ko { color: var(--danger); }
.ac-err { color: var(--danger); font-size: 12px; margin: 0; overflow-wrap: anywhere; }
.ac-table { overflow: auto; max-height: 220px; border: 1px solid var(--border-soft); border-radius: 6px; background: var(--panel); }
.ac-table table { border-collapse: collapse; font-size: 11.5px; font-variant-numeric: tabular-nums; }
.ac-table th, .ac-table td { padding: 3px 8px; border-bottom: 1px solid var(--border-soft); white-space: nowrap; text-align: left; max-width: 240px; overflow: hidden; text-overflow: ellipsis; }
.ac-table th { position: sticky; top: 0; background: var(--panel); font-weight: 600; }
</style>

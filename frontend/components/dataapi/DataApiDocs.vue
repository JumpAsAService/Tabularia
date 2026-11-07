<script setup lang="ts">
// A data API's documentation, for whoever integrates it (docs/specs/data-apis.md, «Pieces», 4):
// the address and the key, each resource with its columns, their types, the filters and their
// operators, the query's parameters, the answers and the errors, curl lines filled in with the
// API's real address, and the OpenAPI description to download. The gateway writes it from the
// definition with the pods' own code (GET /data-apis/{id}/documentation): VIEW is enough, no key
// and no pod are needed, a draft is documented too.
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { BookOpen, Check, Copy, Download, KeyRound, LoaderCircle } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import {
  curlLine, dataApiOrigin, filterParam, openapiForDownload, sampleValue, useDataApis,
  type DataApiDoc, type DataApiKind, type DataApiOperator, type DataApiState,
} from '~/composables/useDataApis'

const props = defineProps<{ apiId: number; state: DataApiState; version: number }>()
const { t, te } = useI18n()
const api = useDataApis()

const doc = ref<DataApiDoc | null>(null)
const failed = ref('')
const loading = ref(true)

async function load() {
  loading.value = true
  try {
    doc.value = await api.documentation(props.apiId)
    failed.value = ''
  } catch (e) {
    failed.value = errMessage(e)
  } finally {
    loading.value = false
  }
}
onMounted(load)
watch(() => [props.apiId, props.version, props.state], load)

const origin = computed(() => (doc.value ? dataApiOrigin(doc.value.base_url, doc.value.host, location) : ''))
const base = computed(() => (doc.value ? origin.value + doc.value.path : ''))
const first = computed(() => doc.value?.resources[0] ?? null)

const kindLabel = (k: DataApiKind | '') => t(`dataApi.kind_${k || 'unknown'}`)

/** The first filter of a resource that reads a single value, for the examples. */
function lookup(r: DataApiDoc['resources'][number]): { param: string; value: string } | null {
  for (const f of r.filters) {
    const op = (['eq', 'in', 'gte', 'lte', 'gt', 'lt'] as DataApiOperator[]).find((o) => f.operators.includes(o))
    if (!op) continue
    const kind = r.columns.find((c) => c.name === f.column)?.kind ?? ''
    return { param: filterParam(f.column, op), value: op === 'in' ? `${sampleValue(kind)},${sampleValue(kind)}` : sampleValue(kind) }
  }
  return null
}

function examples(r: DataApiDoc['resources'][number]): { label: string; line: string }[] {
  const url = origin.value + r.path
  const out = [{ label: t('dataApi.docExamplePage'), line: curlLine(url, { _limit: '5' }) }]
  const l = lookup(r)
  if (l) out.push({ label: t('dataApi.docExampleFilter', { param: l.param }), line: curlLine(url, { [l.param]: l.value }) })
  out.push({ label: t('dataApi.docExampleCount'), line: curlLine(url, { _count: 'true', _limit: '1' }, 'apikey') })
  out.push({ label: t('dataApi.docExampleCsv'), line: `${curlLine(url, { _format: 'csv' })} -o ${r.name}.csv` })
  return out
}

/** A JSON value of this kind, as an answer writes it. */
function sampleJSON(kind: DataApiKind | ''): unknown {
  switch (kind) {
    case 'integer': return 42
    case 'float': case 'decimal': return 12.5
    case 'boolean': return true
    case 'other': case '': return null
  }
  return sampleValue(kind)
}
function answer(r: DataApiDoc['resources'][number]): string {
  const row = Object.fromEntries(r.columns.map((c) => [c.name, sampleJSON(c.kind)]))
  return JSON.stringify({ data: [row], next: 'eyJnIjoi…', version: '2026-11-07T08:12:03Z' }, null, 2)
}

const PARAMS = ['_columns', '_sort', '_limit', '_cursor', '_count', '_format'] as const
const errorMeaning = (code: string, fallback: string) => (te(`dataApi.docErr_${code}`) ? t(`dataApi.docErr_${code}`) : fallback)

const quick = computed(() => {
  const r = first.value
  if (!r) return ''
  return [`TABULARIA_KEY=tdk_…   # ${t('dataApi.docKeyComment')}`, curlLine(origin.value + r.path, { _limit: '5' })].join('\n')
})

// copying: the clipboard, or the text selected for Ctrl+C where there is none (plain HTTP)
const copied = ref('')
async function copy(id: string, text: string, ev: Event) {
  // the button, read now: an event has no currentTarget once its dispatch is over
  const pre = (ev.currentTarget as HTMLElement | null)?.closest('.code')?.querySelector('pre')
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    if (pre) {
      const range = document.createRange()
      range.selectNodeContents(pre)
      window.getSelection()?.removeAllRanges()
      window.getSelection()?.addRange(range)
    }
    return
  }
  copied.value = id
  setTimeout(() => { if (copied.value === id) copied.value = '' }, 1600)
}

function download() {
  if (!doc.value) return
  const body = JSON.stringify(openapiForDownload(doc.value, origin.value), null, 2)
  const a = document.createElement('a')
  a.href = URL.createObjectURL(new Blob([body], { type: 'application/json' }))
  a.download = `${doc.value.path.split('/').filter(Boolean).pop() ?? 'data-api'}-openapi.json`
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(a.href), 1000)
}
</script>

<template>
  <div class="docs" :aria-label="$t('dataApi.docTitle')">
    <p v-if="failed" class="err small" role="alert">{{ failed }}</p>
    <SkeletonRows v-else-if="loading && !doc" :rows="4" />
    <template v-if="doc">
      <div class="card">
        <div class="dhead">
          <h2><BookOpen :size="15" /> {{ $t('dataApi.docTitle') }}</h2>
          <button type="button" class="mini" :disabled="!doc.resources.length" @click="download">
            <Download :size="13" /> {{ $t('dataApi.docDownload') }}
          </button>
        </div>
        <p class="muted small help">{{ $t('dataApi.docLead') }}</p>
        <p v-if="doc.state !== 'active'" class="note small" role="note">{{ $t(`dataApi.docNotServed_${doc.state}`) }}</p>
        <dl class="facts small">
          <div><dt>{{ $t('dataApi.docBase') }}</dt><dd class="mono" data-test="doc-base">{{ base }}</dd></div>
          <div>
            <dt><KeyRound :size="12" /> {{ $t('dataApi.docAuth') }}</dt>
            <dd>{{ $t('dataApi.docAuthText') }}</dd>
          </div>
          <div><dt>{{ $t('dataApi.docVersion') }}</dt><dd>v{{ doc.version }}</dd></div>
        </dl>
        <template v-if="quick">
          <h3>{{ $t('dataApi.docQuick') }}</h3>
          <div class="code">
            <pre data-test="doc-quick">{{ quick }}</pre>
            <button type="button" class="mini copy" :aria-label="$t('dataApi.docCopy')" :title="$t('dataApi.docCopy')"
                    @click="copy('quick', quick, $event)">
              <Check v-if="copied === 'quick'" :size="13" /><Copy v-else :size="13" />
            </button>
          </div>
        </template>
        <p v-else class="muted small">{{ $t('dataApi.docNoResources') }}</p>
      </div>

      <section v-for="r in doc.resources" :key="r.name" class="card res" :data-resource="r.name">
        <h2><span class="verb">GET</span> <code class="rpath">{{ r.path }}</code></h2>
        <table class="cols small">
          <thead>
            <tr><th>{{ $t('dataApi.column') }}</th><th>{{ $t('dataApi.docType') }}</th><th>{{ $t('dataApi.docFilters') }}</th></tr>
          </thead>
          <tbody>
            <tr v-for="c in r.columns" :key="c.name">
              <td class="mono">{{ c.name }}</td>
              <td><span class="kind" :class="{ unknown: !c.kind }">{{ kindLabel(c.kind) }}</span></td>
              <td>
                <span v-for="op in r.filters.find((f) => f.column === c.name)?.operators ?? []" :key="op" class="param mono"
                      :title="$t(`dataApi.op_${op}`)">{{ filterParam(c.name, op) }}=</span>
                <span v-if="!r.filters.some((f) => f.column === c.name)" class="muted">—</span>
              </td>
            </tr>
          </tbody>
        </table>
        <p class="muted small help">
          {{ r.default_sort.length ? $t('dataApi.docOrder', { order: r.default_sort.join(', ') }) : $t('dataApi.docOrderNone') }}
          · {{ $t('dataApi.docPage', { n: r.max_page }) }}
          <template v-if="r.filters.length"> · {{ $t('dataApi.docAnd') }}</template>
        </p>
        <h3>{{ $t('dataApi.docExamples') }}</h3>
        <div v-for="(ex, i) in examples(r)" :key="i" class="example">
          <span class="muted small">{{ ex.label }}</span>
          <div class="code">
            <pre :data-test="`doc-curl-${r.name}-${i}`">{{ ex.line }}</pre>
            <button type="button" class="mini copy" :aria-label="$t('dataApi.docCopy')" :title="$t('dataApi.docCopy')"
                    @click="copy(`${r.name}-${i}`, ex.line, $event)">
              <Check v-if="copied === `${r.name}-${i}`" :size="13" /><Copy v-else :size="13" />
            </button>
          </div>
        </div>
        <details class="answer">
          <summary class="small">{{ $t('dataApi.docAnswer') }}</summary>
          <pre class="small">{{ answer(r) }}</pre>
        </details>
      </section>

      <div v-if="doc.resources.length" class="card">
        <h2>{{ $t('dataApi.docParams') }}</h2>
        <table class="cols small">
          <tbody>
            <tr v-for="p in PARAMS" :key="p">
              <td class="mono pname">{{ p }}</td>
              <td>{{ $t(`dataApi.docParam${p}`, { n: doc.max_sort_keys, values: doc.max_in_values }) }}</td>
            </tr>
            <tr>
              <td class="mono pname">{{ $t('dataApi.docParamFilterName') }}</td>
              <td>{{ $t('dataApi.docParamFilter', { values: doc.max_in_values }) }}</td>
            </tr>
          </tbody>
        </table>
        <h3>{{ $t('dataApi.docAnswers') }}</h3>
        <ul class="plain small">
          <li>{{ $t('dataApi.docAnswerJson') }}</li>
          <li>{{ $t('dataApi.docAnswerCursor') }}</li>
          <li>{{ $t('dataApi.docAnswerCsv') }}</li>
          <li>{{ $t('dataApi.docAnswerValues') }}</li>
        </ul>
      </div>

      <div class="card">
        <h2>{{ $t('dataApi.docErrors') }}</h2>
        <p class="muted small help">{{ $t('dataApi.docErrorsLead') }}</p>
        <div class="tablewrap">
          <table class="cols small errors">
            <thead><tr><th>{{ $t('dataApi.docStatus') }}</th><th>{{ $t('dataApi.docCode') }}</th><th>{{ $t('dataApi.docMeaning') }}</th></tr></thead>
            <tbody>
              <tr v-for="e in doc.errors" :key="e.code">
                <td class="mono" :class="`s${Math.floor(e.status / 100)}`">{{ e.status }}</td>
                <td class="mono">{{ e.code }}</td>
                <td>{{ errorMeaning(e.code, e.meaning) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </template>
    <p v-if="loading && doc" class="muted small"><LoaderCircle :size="12" class="spin" /></p>
  </div>
</template>

<style scoped>
.docs { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
.card { border: 1px solid var(--border-soft); border-radius: 10px; padding: 12px 14px; background: var(--panel); display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.card h2 { margin: 0; font-size: 13.5px; display: flex; align-items: center; gap: 7px; flex-wrap: wrap; min-width: 0; }
.card h3 { margin: 4px 0 0; font-size: 12.5px; font-weight: 600; color: var(--muted); }
.dhead { display: flex; align-items: center; gap: 10px; justify-content: space-between; flex-wrap: wrap; }
.mini { display: inline-flex; align-items: center; gap: 5px; }
.small { font-size: 12px; }
.help { margin: 0; line-height: 1.5; }
.err { color: var(--danger); }
.note { margin: 0; padding: 8px 10px; border-radius: 8px; background: var(--bg-soft); border: 1px solid var(--border); }
.mono, code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.facts { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: 6px 14px; margin: 0; }
.facts > div { display: contents; }
.facts dt { color: var(--muted); display: inline-flex; align-items: center; gap: 4px; }
.facts dd { margin: 0; min-width: 0; overflow-wrap: anywhere; }
.code { position: relative; border: 1px solid var(--border-soft); border-radius: 8px; background: var(--bg-soft); min-width: 0; }
.code pre { margin: 0; padding: 9px 40px 9px 11px; font-size: 12px; line-height: 1.5; white-space: pre-wrap; overflow-wrap: anywhere; }
.code .copy { position: absolute; top: 5px; right: 5px; padding: 3px 6px; min-height: 0; }
.example { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.verb { font-size: 10.5px; font-weight: 700; letter-spacing: 0.05em; padding: 1px 6px; border-radius: 5px;
  color: var(--accent-2); border: 1px solid color-mix(in srgb, var(--accent-2) 45%, var(--border)); }
.rpath { font-size: 13px; overflow-wrap: anywhere; min-width: 0; }
.cols { width: 100%; border-collapse: collapse; }
.cols th { text-align: left; font-weight: 500; color: var(--muted); font-size: 11.5px; padding: 4px 6px; border-bottom: 1px solid var(--border-soft); }
.cols td { padding: 4px 6px; border-bottom: 1px solid var(--border-soft); vertical-align: top; overflow-wrap: anywhere; }
.cols td.pname { white-space: nowrap; width: 1%; }
.kind { font-size: 11.5px; padding: 0 6px; border-radius: 6px; background: var(--panel-2); border: 1px solid var(--border-soft); white-space: nowrap; }
.kind.unknown { color: var(--warning); }
.param { display: inline-block; font-size: 11.5px; margin: 0 6px 2px 0; padding: 0 5px; border-radius: 5px; background: var(--tint-accent); }
.answer summary { cursor: pointer; color: var(--muted); }
.answer pre { margin: 6px 0 0; padding: 9px 11px; border-radius: 8px; background: var(--bg-soft); border: 1px solid var(--border-soft);
  white-space: pre-wrap; overflow-wrap: anywhere; }
.plain { margin: 0; padding-left: 18px; display: flex; flex-direction: column; gap: 4px; line-height: 1.5; }
.tablewrap { overflow-x: auto; }
.errors td.s4 { color: var(--warning); }
.errors td.s5 { color: var(--danger); }
.spin { animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (max-width: 600px) {
  .facts { grid-template-columns: minmax(0, 1fr); }
  .facts dt { margin-top: 4px; }
}
</style>

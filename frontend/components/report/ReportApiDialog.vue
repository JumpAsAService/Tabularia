<script setup lang="ts">
// The report for scripts: the address that downloads it (GET /reports/{id}/render, with
// the values on screen) and the same in Python. A script needs an API token, which is
// never shown here: the example reads it from the TABULARIA_TOKEN variable, and the
// dialog leads to where tokens are made.
import { computed, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Check, Copy, KeyRound } from 'lucide-vue-next'
import Modal from '~/components/ui/Modal.vue'
import { useReports, type ReportParams } from '~/composables/useReports'

const props = defineProps<{
  open: boolean
  reportId: number
  reportName: string
  params: ReportParams
  /** the link was copied already when the dialog opened */
  copied?: boolean
}>()
const emit = defineEmits<{ (e: 'close'): void; (e: 'tokens'): void }>()

const { t } = useI18n()
const { user } = useAuth()
const api = useReports()

type ApiFormat = 'pdf' | 'xlsx' | 'html'
const format = ref<ApiFormat>('pdf')
const FORMATS: { id: ApiFormat; key: string; ext: string }[] = [
  { id: 'pdf', key: 'report.apiFormatPdf', ext: 'pdf' },
  { id: 'xlsx', key: 'report.apiFormatXlsx', ext: 'xlsx' },
  { id: 'html', key: 'report.apiFormatHtml', ext: 'mhtml' },
]
const link = computed(() => (props.open ? api.apiUrl(props.reportId, format.value, props.params) : ''))

// a text as Python writes it: JSON's quoting is valid Python for these texts
const py = (s: string) => JSON.stringify(s)
const example = computed(() => {
  if (!props.open) return ''
  const root = api.apiUrl(props.reportId, format.value).split('?')[0]
  const entries = Object.entries(props.params).filter(([, v]) => v !== null && v !== '' && !(Array.isArray(v) && !v.length))
  const pairs = [`"format": ${py(format.value)}`, ...entries.map(([k, v]) =>
    `${py(k)}: ${Array.isArray(v) ? `[${v.map((x) => py(String(x))).join(', ')}]` : py(String(v))}`)]
  const ext = FORMATS.find((f) => f.id === format.value)!.ext
  const file = `${(props.reportName || 'report').replace(/[\\/:*?"<>|]+/g, '_')}.${ext}`
  return [
    'import os',
    'import requests',
    '',
    `# ${t('report.apiExampleToken')}`,
    'TOKEN = os.environ["TABULARIA_TOKEN"]',
    '',
    'response = requests.get(',
    `    ${py(root)},`,
    `    params={${pairs.join(', ')}},`,
    '    headers={"Authorization": f"Bearer {TOKEN}"},',
    '    timeout=180,',
    ')',
    'response.raise_for_status()',
    `with open(${py(file)}, "wb") as f:`,
    '    f.write(response.content)',
    '',
  ].join('\n')
})

const linkEl = ref<HTMLInputElement | null>(null)
const exampleEl = ref<HTMLElement | null>(null)
const done = ref<'' | 'link' | 'example'>('')
let timer: ReturnType<typeof setTimeout> | null = null

async function copy(what: 'link' | 'example') {
  const text = what === 'link' ? link.value : example.value
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    // no clipboard (a page not served over HTTPS): the text is selected, for Ctrl+C
    if (what === 'link') linkEl.value?.select()
    else {
      const range = document.createRange()
      if (exampleEl.value) range.selectNodeContents(exampleEl.value)
      window.getSelection()?.removeAllRanges()
      window.getSelection()?.addRange(range)
    }
    return
  }
  done.value = what
  if (timer) clearTimeout(timer)
  timer = setTimeout(() => { done.value = '' }, 2000)
}

watch(() => props.open, (o) => {
  if (!o) return
  format.value = 'pdf'
  done.value = props.copied ? 'link' : ''
  if (props.copied) {
    if (timer) clearTimeout(timer)
    timer = setTimeout(() => { done.value = '' }, 2500)
  }
  nextTick(() => linkEl.value?.select())
})
</script>

<template>
  <Modal :open="open" :title="$t('report.apiTitle')" :width="680" @close="emit('close')">
    <div class="api">
      <p class="lead muted">{{ $t('report.apiLead') }}</p>

      <div class="fmt" role="group" :aria-label="$t('report.apiFormat')">
        <span class="lbl">{{ $t('report.apiFormat') }}</span>
        <button
          v-for="f in FORMATS"
          :key="f.id"
          type="button"
          class="mini seg"
          :aria-pressed="format === f.id"
          :class="{ on: format === f.id }"
          @click="format = f.id"
        >{{ $t(f.key) }}</button>
      </div>

      <label class="block">
        <span class="lbl">{{ $t('report.apiLink') }}</span>
        <span class="row">
          <input ref="linkEl" class="mono" type="text" readonly :value="link" @focus="($event.target as HTMLInputElement).select()" />
          <button type="button" class="copy" :class="{ ok: done === 'link' }" @click="copy('link')">
            <Check v-if="done === 'link'" :size="14" /><Copy v-else :size="14" />
            {{ done === 'link' ? $t('report.apiCopied') : $t('report.apiCopy') }}
          </button>
        </span>
      </label>

      <div class="block">
        <span class="head">
          <span class="lbl">{{ $t('report.apiPython') }}</span>
          <button type="button" class="mini copy" :class="{ ok: done === 'example' }" @click="copy('example')">
            <Check v-if="done === 'example'" :size="12" /><Copy v-else :size="12" />
            {{ done === 'example' ? $t('report.apiCopied') : $t('report.apiCopyExample') }}
          </button>
        </span>
        <pre ref="exampleEl" class="code" tabindex="0" :aria-label="$t('report.apiPython')"><code>{{ example }}</code></pre>
      </div>

      <p class="muted small">{{ $t('report.apiTokenNote') }}</p>
      <span class="sr-only" aria-live="polite">{{ done ? $t('report.apiCopied') : '' }}</span>
    </div>
    <template #footer>
      <button v-if="user?.personal_tokens" type="button" @click="emit('tokens')">
        <KeyRound :size="14" /> {{ $t('report.apiTokens') }}
      </button>
      <span v-else class="muted small">{{ $t('report.apiTokensByAdmin') }}</span>
      <span class="grow" />
      <button type="button" @click="emit('close')">{{ $t('report.close') }}</button>
    </template>
  </Modal>
</template>

<style scoped>
.api { display: flex; flex-direction: column; gap: 14px; }
.lead { margin: 0; line-height: 1.5; }
.lbl { font-size: 12px; font-weight: 500; color: var(--muted); }
.fmt { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.fmt .lbl { margin-right: 4px; }
.seg.on { border-color: var(--accent); background: var(--tint-accent); color: var(--accent-hi); }
.block { display: flex; flex-direction: column; gap: 6px; }
.row { display: flex; gap: 8px; }
.row input { flex: 1; min-width: 0; }
.head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.mono, .code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.copy { flex-shrink: 0; white-space: nowrap; }
.copy.ok { color: var(--accent-2); border-color: color-mix(in srgb, var(--accent-2) 50%, var(--border)); }
.code {
  margin: 0; padding: 10px 12px; border-radius: 8px; border: 1px solid var(--border-soft);
  background: var(--bg-soft); color: var(--text); overflow: auto; max-height: 300px; line-height: 1.55;
  white-space: pre; tab-size: 4;
}
.code:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.small { font-size: 12px; margin: 0; }
.grow { flex: 1; }
</style>

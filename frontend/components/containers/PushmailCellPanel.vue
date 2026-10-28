<script setup lang="ts">
// The settings of a Pushmail cell of a flow group: one email through an SMTP
// connection, with one .xlsx per datasource and one PDF or Excel file per report
// (gateway: services/pushmail.py `pushmail_errors`, engine: app/ingest/pushmail.py;
// docs/specs/reports.md, «Sending»). A report takes its parameters' values here, and
// one report may put its summary in the email's body. The subject and the file names
// take the Outputs' date placeholders, previewed as they are typed. More than one
// datasource, or a big one, brings a warning: how large a message the SMTP relay
// takes is known by the administrators.
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Plus, X, Trash2, TriangleAlert, Mail, FileText } from 'lucide-vue-next'
import type { ConnectionInfo } from '~/composables/useConnections'
import type { DatasourceInfo } from '~/composables/useDatasources'
import type { Report } from '~/composables/useReports'
import { usePlaceholderPreview, type PreviewRequest } from '~/composables/usePlaceholders'
import { useFolderPaths } from '~/composables/useFolderPaths'
import TemplatePreview from '~/components/ui/TemplatePreview.vue'
import RdSeg from '~/components/report/designer/RdSeg.vue'
import PushmailReportParams from '~/components/containers/PushmailReportParams.vue'
import {
  PUSHMAIL_MAX_REPORTS, type PushmailAttachment, type PushmailConfig, type PushmailReport,
} from '~/composables/useFlowGroups'


const props = defineProps<{
  config: PushmailConfig
  connections: ConnectionInfo[] // the SMTP ones this person may send through
  datasources: DatasourceInfo[]
  reports: Report[] // the ones this person sees
  groupName: string
  readOnly?: boolean
}>()
const emit = defineEmits<{ (e: 'update', patch: Partial<PushmailConfig>): void; (e: 'delete'): void }>()
const { t } = useI18n()
const folderPaths = useFolderPaths()

const MAX_ATTACHMENTS = 20
const MAX_ROWS = 1_000_000
// a datasource past this many rows in its file is «big»: the warning appears
const BIG_ROWS = 50_000

const dsById = computed(() => new Map(props.datasources.map((d) => [d.id, d])))
const dsOptions = computed(() => props.datasources
  .filter((d) => !!d.key)
  .map((d) => ({ value: d.id, label: d.name, hint: folderPaths.pathOf(d.project_id) ?? undefined })))
const connOptions = computed(() => props.connections.map((c) => ({ value: c.id, label: c.name, hint: c.host })))

function splitAddresses(raw: string): string[] {
  return raw.split(/[,;\s]+/).map((s) => s.trim()).filter(Boolean)
}
// the recipients go to the cell as they are typed (a save with the cursor still in the
// field keeps them), from text of their own: the comma just typed stays where it is
const toText = ref(props.config.to.join(', '))
const ccText = ref(props.config.cc.join(', '))
watch(toText, (v) => emit('update', { to: splitAddresses(v) }))
watch(ccText, (v) => emit('update', { cc: splitAddresses(v) }))

function onRowsTyped(i: number, raw: string) {
  const n = Number(raw)
  if (Number.isInteger(n) && n >= 1 && n <= MAX_ROWS) setAttachment(i, { maxRows: n })
}
function setAttachment(i: number, patch: Partial<PushmailAttachment>) {
  const next = props.config.attachments.map((a, n) => (n === i ? { ...a, ...patch } : a))
  emit('update', { attachments: next })
}
function addAttachment() {
  if (props.config.attachments.length >= MAX_ATTACHMENTS) return
  emit('update', { attachments: [...props.config.attachments, { datasourceId: null, fileName: '', maxRows: 10_000 }] })
}
function removeAttachment(i: number) {
  emit('update', { attachments: props.config.attachments.filter((_, n) => n !== i) })
}
// a datasource chosen gives its file a name, until one is written
function chooseDatasource(i: number, id: number) {
  const a = props.config.attachments[i]
  const ds = dsById.value.get(id)
  const name = a?.fileName.trim() ? a.fileName : `${(ds?.name ?? 'report').replace(/[\\/]/g, '_')}_{today:yyyy-MM-dd}`
  setAttachment(i, { datasourceId: id, fileName: name })
}

// the rows each file will hold, of how many
function rowsOf(a: PushmailAttachment): { total: number | null; sent: number | null } {
  const total = a.datasourceId != null ? dsById.value.get(a.datasourceId)?.rows ?? null : null
  return { total, sent: total == null ? null : Math.min(total, a.maxRows || 0) }
}
// ── reports ─────────────────────────────────────────────────────────────────
const cellReports = computed<PushmailReport[]>(() => props.config.reports ?? [])
const reportById = computed(() => new Map(props.reports.map((r) => [r.id, r])))
const reportOptions = computed(() => props.reports
  .map((r) => ({ value: r.id, label: r.name, hint: folderPaths.pathOf(r.project_id) ?? undefined })))
const FORMATS = computed(() => [{ value: 'pdf', label: t('pushmail.formatPdf') }, { value: 'xlsx', label: t('pushmail.formatXlsx') }])
function setReport(i: number, patch: Partial<PushmailReport>) {
  const next = cellReports.value.map((r, n) => (n === i ? { ...r, ...patch } : r))
  emit('update', { reports: next })
}
function addReport() {
  if (cellReports.value.length >= PUSHMAIL_MAX_REPORTS) return
  emit('update', { reports: [...cellReports.value, { reportId: null, format: 'pdf', params: {}, fileName: '', inBody: false }] })
}
function removeReport(i: number) {
  emit('update', { reports: cellReports.value.filter((_, n) => n !== i) })
}
// a report chosen gives its file a name, until one is written; another report's values
// would not fit it
function chooseReport(i: number, id: number) {
  const r = cellReports.value[i]
  if (!r || r.reportId === id) return
  const name = r.fileName.trim() ? r.fileName : `${(reportById.value.get(id)?.name ?? 'report').replace(/[\\/]/g, '_')}_{today:yyyy-MM-dd}`
  setReport(i, { reportId: id, fileName: name, params: {} })
}
// one summary in the body: checking one unchecks the others
function setInBody(i: number, on: boolean) {
  emit('update', { reports: cellReports.value.map((r, n) => ({ ...r, inBody: n === i ? on : on ? false : r.inBody })) })
}
// a report the list does not have: deleted since, or in a folder one cannot see
const unknownReport = (r: PushmailReport) => r.reportId != null && !reportById.value.has(r.reportId)

const warn = computed(() => {
  const chosen = props.config.attachments.filter((a) => a.datasourceId != null)
  return chosen.length > 1 || chosen.some((a) => (rowsOf(a).sent ?? 0) > BIG_ROWS)
})

const preview = usePlaceholderPreview((): PreviewRequest | null => {
  const templates: Record<string, string> = { subject: props.config.subject ?? '' }
  const targets: Record<string, 'text' | 'file'> = { subject: 'text' }
  props.config.attachments.forEach((a, i) => { templates[`file_${i}`] = (a.fileName ?? '').trim(); targets[`file_${i}`] = 'file' })
  cellReports.value.forEach((r, i) => { templates[`report_${i}`] = (r.fileName ?? '').trim(); targets[`report_${i}`] = 'file' })
  return { kind: 'output', templates, targets, timezone: props.config.timezone ?? '', flow: props.groupName }
})
const fmt = (n: number) => n.toLocaleString()
// braces in a message are its parameters: the placeholder comes in as one
const DATE_EXAMPLE = '{today:yyyy-MM-dd}'
</script>

<template>
  <div class="pm" :class="{ ro: readOnly }">
    <fieldset :disabled="readOnly">
      <label for="pm-label">{{ t('pushmail.labelLabel') }}</label>
      <input id="pm-label" :value="config.label" type="text" maxlength="200" :placeholder="t('pushmail.labelPlaceholder')"
             @input="emit('update', { label: ($event.target as HTMLInputElement).value })" />

      <label>{{ t('pushmail.connectionLabel') }}</label>
      <Select :model-value="config.connectionId" :options="connOptions" searchable :placeholder="t('pushmail.connectionPlaceholder')"
              @update:model-value="(v: any) => emit('update', { connectionId: v })" />
      <p v-if="!connections.length" class="muted hint">{{ t('pushmail.noConnections') }}</p>

      <label for="pm-to">{{ t('pushmail.toLabel') }} <span class="muted">{{ t('pushmail.listHint') }}</span></label>
      <input id="pm-to" v-model="toText" type="text" spellcheck="false" placeholder="anna@acme.example, bruno@acme.example" />
      <label for="pm-cc">{{ t('pushmail.ccLabel') }}</label>
      <input id="pm-cc" v-model="ccText" type="text" spellcheck="false" />

      <label for="pm-subject">{{ t('pushmail.subjectLabel') }}</label>
      <input id="pm-subject" :value="config.subject" type="text" maxlength="500" aria-describedby="tp-pm-subject"
             :placeholder="t('pushmail.subjectPlaceholder', { example: DATE_EXAMPLE })"
             @input="emit('update', { subject: ($event.target as HTMLInputElement).value })" />
      <TemplatePreview id="tp-pm-subject" :preview="preview" name="subject" />

      <label for="pm-body">{{ t('pushmail.bodyLabel') }}</label>
      <textarea id="pm-body" :value="config.body" rows="4" maxlength="20000"
                @input="emit('update', { body: ($event.target as HTMLTextAreaElement).value })" />
      <label class="chk"><input type="checkbox" :checked="config.bodyIsHtml"
             @change="emit('update', { bodyIsHtml: ($event.target as HTMLInputElement).checked })" /> {{ t('pushmail.bodyHtml') }}</label>

      <div class="atthead">
        <span class="lbl">{{ t('pushmail.attachmentsLabel') }}</span>
        <span class="muted">{{ t('pushmail.attachmentsHint') }}</span>
      </div>
      <div v-for="(a, i) in config.attachments" :key="i" class="att">
        <div class="arow">
          <Select class="ads" :model-value="a.datasourceId" :options="dsOptions" searchable :placeholder="t('pushmail.datasourcePlaceholder')"
                  :aria-label="t('pushmail.datasourceAria', { n: i + 1 })"
                  @update:model-value="(v: any) => chooseDatasource(i, v)" />
          <button type="button" class="x" :title="t('pushmail.removeAttachment')" :aria-label="t('pushmail.removeAttachment')"
                  @click="removeAttachment(i)"><X :size="13" /></button>
        </div>
        <div class="arow2">
          <label class="fname">
            <span>{{ t('pushmail.fileNameLabel', { example: DATE_EXAMPLE }) }}</span>
            <span class="withext">
              <input :value="a.fileName" type="text" spellcheck="false" maxlength="255" :aria-describedby="`tp-pm-file-${i}`"
                     @input="setAttachment(i, { fileName: ($event.target as HTMLInputElement).value })" />
              <span class="ext muted">.xlsx</span>
            </span>
          </label>
          <label class="maxrows">
            <span>{{ t('pushmail.maxRowsLabel') }}</span>
            <!-- kept as it is typed (Esc closes the panel without a change event), bounded on change -->
            <input :value="a.maxRows" type="number" min="1" :max="MAX_ROWS" step="1"
                   @input="onRowsTyped(i, ($event.target as HTMLInputElement).value)"
                   @change="setAttachment(i, { maxRows: Math.min(MAX_ROWS, Math.max(1, Math.floor(Number(($event.target as HTMLInputElement).value) || 1))) })" />
          </label>
        </div>
        <TemplatePreview :id="`tp-pm-file-${i}`" :preview="preview" :name="`file_${i}`" />
        <p v-if="rowsOf(a).total != null" class="muted rows">
          <template v-if="(rowsOf(a).total ?? 0) > a.maxRows">{{ t('pushmail.rowsCut', { sent: fmt(rowsOf(a).sent ?? 0), total: fmt(rowsOf(a).total ?? 0) }) }}</template>
          <template v-else>{{ t('pushmail.rowsAll', { total: fmt(rowsOf(a).total ?? 0) }, rowsOf(a).total ?? 0) }}</template>
        </p>
      </div>
      <button type="button" class="add" :disabled="config.attachments.length >= MAX_ATTACHMENTS" @click="addAttachment">
        <Plus :size="13" /> {{ t('pushmail.addAttachment') }}
      </button>

      <div class="atthead">
        <span class="lbl">{{ t('pushmail.reportsLabel') }}</span>
        <span class="muted">{{ t('pushmail.reportsHint') }}</span>
      </div>
      <div v-for="(r, i) in cellReports" :key="`r${i}`" class="att rep" :data-report="i">
        <div class="arow">
          <FileText :size="14" class="ricon" aria-hidden="true" />
          <Select class="ads" :model-value="r.reportId" :options="reportOptions" searchable
                  :placeholder="unknownReport(r) ? t('pushmail.reportHidden', { id: r.reportId }) : t('pushmail.reportPlaceholder')"
                  :aria-label="t('pushmail.reportAria', { n: i + 1 })"
                  @update:model-value="(v: any) => chooseReport(i, v)" />
          <RdSeg class="fmt" :model-value="r.format" :options="FORMATS" :label="t('pushmail.formatAria', { n: i + 1 })"
                 @update:model-value="(v: any) => setReport(i, { format: v })" />
          <button type="button" class="x" :title="t('pushmail.removeReport')" :aria-label="t('pushmail.removeReport')"
                  @click="removeReport(i)"><X :size="13" /></button>
        </div>
        <label class="fname">
          <span>{{ t('pushmail.fileNameLabel', { example: DATE_EXAMPLE }) }}</span>
          <span class="withext">
            <input :value="r.fileName" type="text" spellcheck="false" maxlength="255" :aria-describedby="`tp-pm-report-${i}`"
                   @input="setReport(i, { fileName: ($event.target as HTMLInputElement).value })" />
            <span class="ext muted">.{{ r.format }}</span>
          </span>
        </label>
        <TemplatePreview :id="`tp-pm-report-${i}`" :preview="preview" :name="`report_${i}`" />
        <div v-if="r.reportId != null && !unknownReport(r)" class="params">
          <span class="sub">{{ t('pushmail.paramsLabel') }}</span>
          <PushmailReportParams :report-id="r.reportId" :model-value="r.params" :id-prefix="`pm-r${i}`" :read-only="readOnly"
                                @update:model-value="(v) => setReport(i, { params: v })" />
        </div>
        <label class="chk body">
          <input type="checkbox" :checked="r.inBody" @change="setInBody(i, ($event.target as HTMLInputElement).checked)" />
          <span>{{ t('pushmail.inBody') }} <span class="muted">{{ t('pushmail.inBodyHint') }}</span></span>
        </label>
      </div>
      <p v-if="!reports.length" class="muted hint">{{ t('pushmail.noReports') }}</p>
      <button type="button" class="add" :disabled="cellReports.length >= PUSHMAIL_MAX_REPORTS || !reports.length" @click="addReport">
        <Plus :size="13" /> {{ t('pushmail.addReport') }}
      </button>

      <div v-if="warn" class="warn" role="note">
        <TriangleAlert :size="15" />
        <p>{{ t('pushmail.sizeWarning') }}</p>
      </div>
      <p class="muted hint"><Mail :size="12" /> {{ t('pushmail.domainsHint') }}</p>
    </fieldset>
    <div v-if="!readOnly" class="foot">
      <button type="button" class="danger-ghost" @click="emit('delete')"><Trash2 :size="13" /> {{ t('pushmail.removeCell') }}</button>
    </div>
  </div>
</template>

<style scoped>
.pm fieldset { border: 0; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.pm label { font-size: 12px; color: var(--muted); }
.pm label.chk { display: flex; align-items: center; gap: 6px; color: var(--text); font-size: 13px; }
.pm label.chk input { width: auto; }
.pm textarea { resize: vertical; font-size: 13px; }
.hint { font-size: 12px; margin: 0; display: flex; align-items: center; gap: 5px; }
.atthead { display: flex; align-items: baseline; gap: 8px; margin-top: 8px; }
.atthead .lbl { font-size: 12.5px; font-weight: 600; color: var(--text); }
.atthead .muted { font-size: 11.5px; }
.att { display: flex; flex-direction: column; gap: 6px; padding: 10px; border: 1px solid var(--border); border-radius: 9px; background: var(--panel-2); }
.arow { display: flex; gap: 6px; align-items: center; }
.ads { flex: 1; min-width: 0; }
.arow .x { padding: 2px 8px; flex-shrink: 0; }
.arow2 { display: grid; grid-template-columns: minmax(0, 1fr) 128px; gap: 8px; }
.arow2 label { display: flex; flex-direction: column; gap: 3px; }
.withext { display: flex; align-items: center; gap: 4px; }
.withext input { flex: 1; min-width: 0; font-family: ui-monospace, monospace; font-size: 12.5px; }
.ext { font-family: ui-monospace, monospace; font-size: 12px; }
.maxrows input { font-variant-numeric: tabular-nums; }
.rows { font-size: 11.5px; margin: 0; }
.add { align-self: flex-start; display: inline-flex; align-items: center; gap: 5px; }
.warn { display: flex; gap: 8px; align-items: flex-start; padding: 9px 11px; border-radius: 9px; margin-top: 4px;
  border: 1px solid color-mix(in srgb, #f59e0b 45%, var(--border)); background: color-mix(in srgb, #f59e0b 9%, transparent);
  color: var(--text); font-size: 12.5px; line-height: 1.45; }
.warn svg { color: #f59e0b; flex-shrink: 0; margin-top: 1px; }
.warn p { margin: 0; }
.ricon { color: var(--muted); flex-shrink: 0; }
.fmt { flex-shrink: 0; }
.rep .fname { display: flex; flex-direction: column; gap: 3px; }
.params { display: flex; flex-direction: column; gap: 4px; padding-top: 2px; }
.sub { font-size: 11.5px; font-weight: 600; color: var(--muted); }
.pm label.chk.body { align-items: flex-start; font-size: 12.5px; line-height: 1.45; }
.pm label.chk.body input { margin-top: 2px; flex-shrink: 0; }
.pm label.chk.body .muted { font-size: 11.5px; }
.foot { display: flex; justify-content: flex-start; margin-top: 12px; }
.danger-ghost { display: inline-flex; align-items: center; gap: 5px; color: var(--danger, #ef4444); }
@media (max-width: 520px) { .arow2 { grid-template-columns: 1fr; } }
</style>

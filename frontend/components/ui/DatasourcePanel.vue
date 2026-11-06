<script setup lang="ts">
// A datasource, opened from Explore: what it holds without leaving the folder. Its schema
// (the columns and their types, with their descriptions), its first rows (the preview the
// Viewer runs, on the person's default engine), its contract, and the two ways on: the
// Viewer, or a new flow that starts from it.
//
// A side panel like the folder's settings (veil + slide from the right edge), modal: the
// focus stays inside, Esc closes and gives it back to the row.
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Database, ExternalLink, Workflow, X, ScrollText } from 'lucide-vue-next'
import { errMessage, type PreviewResult } from '~/composables/useApi'
import { useFormat } from '~/composables/useFormat'
import { useDialogA11y } from '~/composables/useDialogA11y'
import { isSuperseded, usePreviewSlots } from '~/composables/usePreviewSlots'
import type { DatasourceInfo } from '~/composables/useDatasources'
import ContractBadge from '~/components/ui/ContractBadge.vue'
import DataGrid from '~/components/ui/DataGrid.vue'

const props = defineProps<{ ds: DatasourceInfo | null; engine: string; viewerHref: string }>()
const emit = defineEmits<{ (e: 'close'): void; (e: 'contract', ds: DatasourceInfo): void; (e: 'new-flow', ds: DatasourceInfo): void }>()

const { t } = useI18n()
const fmt = useFormat()
const panel = ref<HTMLElement | null>(null)
useDialogA11y(panel, () => !!props.ds, () => emit('close'))

const ROWS = 20
const slots = usePreviewSlots('xp-ds')
const result = ref<PreviewResult | null>(null)
const loading = ref(false)
const error = ref('')

watch(() => props.ds?.id, async (id) => {
  result.value = null
  error.value = ''
  const ds = props.ds
  if (id == null || !ds) { slots.cancelAll(); loading.value = false; return }
  if (!ds.key) return // an import not finished yet: nothing to read
  loading.value = true
  try {
    const res = await slots.preview({ bucket: ds.bucket, input_key: ds.key, operations: [], engine: props.engine, limit: ROWS, no_cache: true, sort_keys: ds.sort_keys }, 'rows')
    if (props.ds?.id === id) result.value = res
  } catch (e) {
    if (!isSuperseded(e) && props.ds?.id === id) error.value = errMessage(e)
  } finally {
    if (props.ds?.id === id) loading.value = false
  }
}, { immediate: true })

const meta = computed(() => {
  const ds = props.ds
  if (!ds) return ''
  return [
    ds.rows != null ? t('projectBrowser.rowsCount', { n: fmt.count(ds.rows) }) : '',
    t('projectBrowser.dsColumns', { n: ds.columns.length }, ds.columns.length),
    ds.refreshed_at || ds.updated_at ? fmt.dateTime((ds.refreshed_at ?? ds.updated_at)!) : '',
  ].filter(Boolean).join(' · ')
})
function openViewer() { navigateTo(props.viewerHref) }
const describe = (name: string) => props.ds?.column_descriptions?.[name] ?? props.ds?.columns.find((c) => c.name === name)?.description ?? ''
</script>

<template>
  <Teleport to="body">
    <Transition name="veil">
      <div v-if="ds" class="dsp-veil" @click="emit('close')" />
    </Transition>
    <Transition name="drawer">
      <aside v-if="ds" ref="panel" class="dsp" role="dialog" aria-modal="true" tabindex="-1" :aria-label="ds.name">
        <header class="dsp-head">
          <Database :size="16" class="dsp-icon" />
          <div class="dsp-title">
            <h2>{{ ds.name }}</h2>
            <p class="muted">{{ meta }}</p>
          </div>
          <button class="dsp-close" :aria-label="$t('projectBrowser.closeDrawer')" :title="$t('projectBrowser.closeDrawer')" @click="emit('close')"><X :size="16" /></button>
        </header>
        <p v-if="ds.description" class="dsp-desc">{{ ds.description }}</p>

        <div class="dsp-actions">
          <a class="btn-primary" :href="viewerHref" @click.exact.prevent="openViewer"><ExternalLink :size="14" /> {{ $t('projectBrowser.openInViewer') }}</a>
          <button @click="emit('new-flow', ds)"><Workflow :size="14" /> {{ $t('projectBrowser.newFlowFromHere') }}</button>
        </div>

        <section v-if="ds.contract" class="dsp-sec">
          <h3>{{ $t('projectBrowser.dsContract') }}</h3>
          <div class="dsp-contract">
            <ContractBadge :contract="ds.contract" :size="15" label />
            <button class="mini" @click="emit('contract', ds)"><ScrollText :size="13" /> {{ $t('contracts.open') }}</button>
          </div>
        </section>

        <section class="dsp-sec">
          <h3>{{ $t('projectBrowser.dsSchema') }}</h3>
          <table class="dsp-schema">
            <tbody>
              <tr v-for="c in ds.columns" :key="c.name" :title="describe(c.name) || undefined">
                <td class="cn">{{ c.name }}</td>
                <td class="ct">{{ c.dtype }}</td>
              </tr>
            </tbody>
          </table>
        </section>

        <section class="dsp-sec">
          <h3>{{ $t('projectBrowser.dsFirstRows', { n: ROWS }) }}</h3>
          <div class="dsp-grid">
            <DataGrid :result="result" :loading="loading" :error="error" :note="!ds.key ? $t('projectBrowser.importingLabel') : undefined" />
          </div>
        </section>
      </aside>
    </Transition>
  </Teleport>
</template>

<style scoped>
.dsp-veil { position: fixed; inset: 0; background: var(--scrim); z-index: 120; }
.dsp {
  position: fixed;
  top: 0;
  right: 0;
  bottom: 0;
  z-index: 121;
  width: 600px;
  max-width: 94vw;
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 16px 18px;
  overflow-y: auto;
  background: var(--panel);
  border-left: 1px solid var(--border);
  box-shadow: var(--shadow-2);
  outline: none;
}
.dsp-head { display: flex; align-items: flex-start; gap: 10px; }
.dsp-icon { color: var(--muted); margin-top: 3px; flex-shrink: 0; }
.dsp-title { flex: 1; min-width: 0; }
.dsp-title h2 { margin: 0; font-size: 16px; font-weight: 650; overflow-wrap: anywhere; }
.dsp-title p { margin: 3px 0 0; font-size: 12px; font-variant-numeric: tabular-nums; }
.dsp-close { display: grid; place-items: center; width: 28px; height: 28px; padding: 0; border: 0; border-radius: 8px; background: transparent; color: var(--muted); }
.dsp-close:hover { background: var(--panel-2); color: var(--text); box-shadow: none; }
.dsp-desc { margin: 0; font-size: 13px; line-height: 1.5; color: var(--text); max-width: 70ch; }
.dsp-actions { display: flex; flex-wrap: wrap; gap: 8px; }
.dsp-actions > * { display: inline-flex; align-items: center; gap: 7px; height: 32px; box-sizing: border-box; padding: 0 12px; }
.btn-primary { border-radius: 8px; background: var(--grad-accent); color: var(--on-accent); text-decoration: none; font-size: 13px; font-weight: 500; }
.dsp-sec { display: flex; flex-direction: column; gap: 6px; }
.dsp-sec h3 { margin: 6px 0 0; font-size: 12px; font-weight: 600; color: var(--muted); }
.dsp-contract { display: flex; align-items: center; gap: 10px; }
.dsp-contract .mini { display: inline-flex; align-items: center; gap: 6px; }
.dsp-schema { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.dsp-schema td { padding: 4px 6px; border-bottom: 1px solid var(--border-soft); }
.dsp-schema .cn { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; overflow-wrap: anywhere; }
.dsp-schema .ct { color: var(--muted); text-align: right; white-space: nowrap; font-size: 11.5px; }
.dsp-grid { border: 1px solid var(--border-soft); border-radius: 8px; overflow: auto; max-height: 420px; }
</style>

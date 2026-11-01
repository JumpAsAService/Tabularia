<script setup lang="ts">
// With no tile selected, the right panel is the dashboard's own: the language of its numbers, its
// time zone, how it refreshes, and a reminder of the keys.
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { LayoutDashboard } from 'lucide-vue-next'
import Select from '~/components/ui/Select.vue'
import RdNum from '~/components/report/designer/RdNum.vue'
import RdSeg from '~/components/report/designer/RdSeg.vue'
import { REFRESH_LEAST, REFRESH_MOST, type DBoard } from '~/composables/dashboardDesignModel'
import { useDesign } from '~/composables/reportDesignStore'

const store = useDesign()
const { t } = useI18n()
const doc = computed(() => store.doc.value as DBoard)

const LANGUAGES = [
  { value: 'en', label: 'English' }, { value: 'it', label: 'Italiano' }, { value: 'de', label: 'Deutsch' },
  { value: 'es', label: 'Español' }, { value: 'fr', label: 'Français' },
]
const zones = computed(() => {
  let list: string[] = []
  try { list = (Intl as any).supportedValuesOf?.('timeZone') ?? [] } catch { list = [] }
  if (!list.includes('UTC')) list = ['UTC', ...list]
  const cur = doc.value.timezone
  if (cur && !list.includes(cur)) list = [cur, ...list]
  return list.map((z) => ({ value: z, label: z }))
})

const refreshModes = computed(() => [
  { value: 'hand', label: t('dashboardDesigner.refreshHand') },
  { value: 'every', label: t('dashboardDesigner.refreshEvery') },
])
const mode = computed(() => (doc.value.refresh_seconds ? 'every' : 'hand'))
function setMode(m: string) {
  doc.value.refresh_seconds = m === 'every' ? 300 : 0
}
const refreshProblem = computed(() => store.problemAt(null, 'refresh_seconds') ?? store.liveAt(null, 'refresh_seconds'))
</script>

<template>
  <header class="ddb-head">
    <LayoutDashboard :size="16" class="ddb-icon" />
    <span class="ddb-name">{{ $t('dashboardDesigner.thisDashboard') }}</span>
  </header>
  <div class="ddb-body" data-section="board">
    <label class="rp-f">
      <span>{{ $t('dashboardDesigner.language') }}</span>
      <Select :model-value="doc.locale" :options="LANGUAGES" :aria-label="$t('dashboardDesigner.language')" @update:model-value="doc.locale = $event" />
    </label>
    <label class="rp-f">
      <span>{{ $t('reportDesigner.setup.timezone') }}</span>
      <Select :model-value="doc.timezone" :options="zones" searchable :aria-label="$t('reportDesigner.setup.timezone')" @update:model-value="doc.timezone = $event" />
    </label>
    <div class="rp-f">
      <span>{{ $t('dashboardDesigner.refresh') }}</span>
      <RdSeg :model-value="mode" :options="refreshModes" :label="$t('dashboardDesigner.refresh')" @update:model-value="setMode" />
    </div>
    <label v-if="mode === 'every'" class="rp-f">
      <span>{{ $t('dashboardDesigner.refreshSeconds') }}</span>
      <RdNum v-model="doc.refresh_seconds" data-field="refresh-seconds" :decimals="0" :step="60" :min="REFRESH_LEAST" :max="REFRESH_MOST" unit="s" />
    </label>
    <p class="rp-help">{{ mode === 'every' ? $t('dashboardDesigner.refreshEveryHelp', { least: REFRESH_LEAST, most: REFRESH_MOST }) : $t('dashboardDesigner.refreshHandHelp') }}</p>
    <p v-if="refreshProblem" class="rp-err" role="alert">{{ refreshProblem.message }}</p>

    <p class="rp-help ddb-hint">{{ $t('dashboardDesigner.nothingSelected') }}</p>
    <dl class="ddb-keys">
      <div><dt><kbd>Ctrl</kbd> <kbd>S</kbd></dt><dd>{{ $t('reportDesigner.keys.save') }}</dd></div>
      <div><dt><kbd>Ctrl</kbd> <kbd>Z</kbd></dt><dd>{{ $t('reportDesigner.keys.undo') }}</dd></div>
      <div><dt><kbd>Ctrl</kbd> <kbd>⇧</kbd> <kbd>Z</kbd></dt><dd>{{ $t('reportDesigner.keys.redo') }}</dd></div>
      <div><dt><kbd>←</kbd> <kbd>→</kbd> <kbd>↑</kbd> <kbd>↓</kbd></dt><dd>{{ $t('dashboardDesigner.keysNudge') }}</dd></div>
      <div><dt><kbd>Ctrl</kbd> <kbd>D</kbd></dt><dd>{{ $t('reportDesigner.keys.duplicate') }}</dd></div>
      <div><dt><kbd>Del</kbd></dt><dd>{{ $t('reportDesigner.keys.delete') }}</dd></div>
    </dl>
  </div>
</template>

<style scoped src="~/assets/reportprops.css" />
<style scoped>
.ddb-head { display: flex; align-items: center; gap: 8px; padding: 12px 12px 10px; border-bottom: 1px solid var(--border-soft); }
.ddb-icon { color: var(--accent); flex: none; }
.ddb-name { font-size: 14px; font-weight: 600; }
.ddb-body { flex: 1; min-height: 0; overflow: auto; padding: 10px 12px 16px; display: flex; flex-direction: column; gap: 10px; }
.ddb-body .rp-f { flex: none; }
.ddb-hint { margin-top: 10px; }
.ddb-keys { margin: 0; display: flex; flex-direction: column; gap: 6px; font-size: 12px; }
.ddb-keys div { display: flex; justify-content: space-between; gap: 10px; align-items: center; }
.ddb-keys dt { white-space: nowrap; }
.ddb-keys dd { margin: 0; color: var(--muted); text-align: right; }
kbd {
  font: 500 10.5px/1 ui-monospace, SFMono-Regular, Menlo, monospace; padding: 2px 5px; border-radius: 4px;
  border: 1px solid var(--border); border-bottom-width: 2px; background: var(--bg-soft); color: var(--text);
}
</style>

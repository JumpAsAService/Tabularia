<script setup lang="ts">
import BrandMark from '~/components/ui/BrandMark.vue'
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  Upload,
  Plus,
  Play,
  Save,
  CheckCircle2,
  XCircle,
  LoaderCircle,
  ArrowLeft,
  LogOut,
  Cpu,
} from 'lucide-vue-next'

const { t } = useI18n()

const props = defineProps<{
  status: string
  statusKind?: 'info' | 'ok' | 'error' | 'busy'
  busy?: boolean
  canRun?: boolean
  // saving the flow
  flowName?: string
  projects?: { id: number; name: string }[]
  projectId?: number | null
  // the flow's DEVELOPMENT engine (the one the editor works with)
  engine?: string
  // PRODUCTION engine (scheduled runs); null/equal = no difference to show
  productionEngine?: string | null
  // observer: tries the transformations, does not save, run or upload. The server enforces
  // the same things anyway; here the buttons that would lead to a 403 are removed
  readOnly?: boolean
  // changes not saved yet (the editor decides)
  dirty?: boolean
}>()

const ENGINE_LABELS: Record<string, string> = { polars: 'Polars', duckdb: 'DuckDB', chdb: 'chDB', clickhouse: 'ClickHouse' }
const labelOf = (id?: string | null) => (id ? ENGINE_LABELS[id] ?? id : 'Polars')
// readable label of the current engine
const engineLabel = computed(() => labelOf(props.engine))
const badgeClass = computed(() => (props.engine && props.engine in ENGINE_LABELS ? props.engine : 'polars'))
const hasProdDiff = computed(() => !!props.productionEngine && props.productionEngine !== (props.engine || 'polars'))
const engineTitle = computed(() =>
  hasProdDiff.value
    ? t('toolbar.engineTitleProd', { engine: engineLabel.value, prod: labelOf(props.productionEngine) })
    : t('toolbar.engineTitle', { engine: engineLabel.value }),
)
const emit = defineEmits<{
  (e: 'upload', file: File): void
  (e: 'add-op'): void
  (e: 'add-source'): void
  (e: 'run'): void
  (e: 'save'): void
  (e: 'update:flowName', name: string): void
  (e: 'logout'): void
  (e: 'update:projectId', id: number | null): void
}>()

function onFile(ev: Event) {
  const input = ev.target as HTMLInputElement
  const file = input.files?.[0]
  if (file) emit('upload', file)
  input.value = '' // allows reloading the same file
}

// state icon: spinner while working, green check on success, red X on error
const statusIcon = computed(() => {
  switch (props.statusKind) {
    case 'ok': return CheckCircle2
    case 'error': return XCircle
    case 'busy': return LoaderCircle
    default: return null
  }
})
</script>

<template>
  <div class="toolbar-inner">
    <strong class="brand"><BrandMark :size="20" /> Tabularia</strong>

    <!-- the flow's run engine -->
    <span
      class="enginebadge"
      :class="badgeClass"
      :title="engineTitle"
    >
      <Cpu :size="12" /> {{ engineLabel }}<span v-if="hasProdDiff" class="prod">→ {{ labelOf(productionEngine) }}</span>
    </span>

    <!-- flow name + destination + save -->
    <input
      class="flowname"
      type="text"
      :value="flowName"
      :placeholder="$t('toolbar.flowNamePlaceholder')"
      :aria-label="$t('toolbar.flowNamePlaceholder')"
      @input="emit('update:flowName', ($event.target as HTMLInputElement).value)"
    />
    <span v-if="dirty" class="unsaved" :title="$t('toolbar.unsavedTitle')"><span class="dot" aria-hidden="true" />{{ $t('toolbar.unsaved') }}</span>
    <Select
      v-if="projects?.length && projectId === null"
      class="projsel"
      :model-value="projectId"
      :options="projects.map((p) => ({ value: p.id, label: p.name }))"
      :placeholder="$t('toolbar.folderPlaceholder')"
      :aria-label="$t('toolbar.folderPlaceholder')"
      @update:model-value="emit('update:projectId', $event)"
    />
    <button v-if="!readOnly" :disabled="busy" :title="`${$t('toolbar.saveFlowTitle')} (Ctrl+S)`" @click="emit('save')"><Save :size="15" /> {{ $t('toolbar.save') }}</button>

    <span class="sep" />

    <label v-if="!readOnly" class="filebtn">
      <Upload :size="15" /> {{ $t('toolbar.uploadFile') }}
      <input type="file" accept=".csv,.tsv,.txt,.json,.ndjson,.jsonl,.xlsx,.xls,.parquet" @change="onFile" />
    </label>

    <button v-if="!readOnly" @click="emit('add-source')"><Plus :size="15" /> {{ $t('toolbar.source') }}</button>
    <button @click="emit('add-op')"><Plus :size="15" /> {{ $t('toolbar.operation') }}</button>
    <button v-if="!readOnly" class="primary" :disabled="!canRun || busy" @click="emit('run')"><Play :size="15" /> {{ $t('toolbar.run') }}</button>

    <span class="status muted" :class="statusKind">
      <component
        :is="statusIcon"
        v-if="statusIcon"
        :size="15"
        :class="{ spin: statusKind === 'busy' }"
      />
      {{ status }}
    </span>

    <!-- navigation: INSIDE the bar, so it never covers the run's state -->
    <span class="sep" />
    <MemoryGauge compact />
    <span class="sep" />
    <NuxtLink :to="projectId ? `/?folder=${projectId}` : '/'" class="navbtn"><ArrowLeft :size="13" /> {{ $t('nav.explore') }}</NuxtLink>
    <button class="navbtn" :title="$t('toolbar.logoutTitle')" :aria-label="$t('toolbar.logoutTitle')" @click="emit('logout')"><LogOut :size="13" /></button>
  </div>
</template>

<style scoped>
/* unsaved changes: a dot and a word, next to the flow's name */
.unsaved { display: inline-flex; align-items: center; gap: 5px; font-size: 11.5px; color: var(--warning); white-space: nowrap; }
.unsaved .dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
.toolbar-inner {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
}
.brand { display: inline-flex; align-items: center; gap: 6px; }
/* run engine badge */
.enginebadge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.02em;
  border: 1px solid transparent;
  white-space: nowrap;
  cursor: default;
}
.enginebadge.polars { color: color-mix(in srgb, #6ee7b7 var(--enc-text-mix), var(--text)); background: rgba(110, 231, 183, 0.12); border-color: rgba(110, 231, 183, 0.35); }
.enginebadge.duckdb { color: color-mix(in srgb, #fbbf24 var(--enc-text-mix), var(--text)); background: rgba(251, 191, 36, 0.12); border-color: rgba(251, 191, 36, 0.35); }
.enginebadge.chdb { color: color-mix(in srgb, #fb923c var(--enc-text-mix), var(--text)); background: rgba(251, 146, 60, 0.12); border-color: rgba(251, 146, 60, 0.35); }
.enginebadge.clickhouse { color: color-mix(in srgb, #60a5fa var(--enc-text-mix), var(--text)); background: rgba(96, 165, 250, 0.12); border-color: rgba(96, 165, 250, 0.35); }
.enginebadge .prod { margin-left: 5px; opacity: 0.8; font-weight: 500; }
.flowname { width: 170px; }
.projsel { width: 140px; }
.sep { width: 1px; align-self: stretch; background: var(--border); }
.status {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.status.ok { color: var(--accent-2); }
.status.error { color: var(--danger); }
.navbtn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  padding: 5px 10px;
  background: var(--panel-2);
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text);
  text-decoration: none;
  white-space: nowrap;
  cursor: pointer;
}
.navbtn:hover { border-color: var(--accent); }
.filebtn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: var(--panel-2);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 12px;
  cursor: pointer;
  white-space: nowrap;
}
.filebtn:hover { border-color: var(--accent); }
.filebtn input { display: none; }
</style>

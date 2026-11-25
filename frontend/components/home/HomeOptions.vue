<script setup lang="ts">
// A widget's options, in a dialog: the fields of its type, with the gateway's limits, on a copy
// that is saved only by «Save». A place of the bar the person does not have is not offered.
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Modal from '~/components/ui/Modal.vue'
import { NAV_SECTIONS } from '~/composables/appKeys'
import type { HomeWidget } from '~/composables/homeLayout'
import { useKeyHelp } from '~/composables/useKeyHelp'
import { useDatasources } from '~/composables/useDatasources'
import { useFlows } from '~/composables/useFlows'
import { useFolderPaths } from '~/composables/useFolderPaths'

const props = defineProps<{ widget: HomeWidget | null }>()
const emit = defineEmits<{ (e: 'save', options: Record<string, any>): void; (e: 'close'): void }>()
const { t } = useI18n()
const keyHelp = useKeyHelp()

const draft = ref<Record<string, any>>({})
// what a widget does when an option is not set, so the form shows it: a widget just added has
// none, and an empty required field would refuse «Save» until every one was typed
const DEFAULTS: Record<string, Record<string, any>> = {
  recent: { limit: 8 }, failed_runs: { scope: 'all', days: 7 }, next_runs: { scope: 'all', hours: 24 },
  flow_runs: { runs: 20 }, runs_by_day: { days: 30 }, clock: { zone: '' },
}
watch(() => props.widget, (w) => { draft.value = w ? { ...DEFAULTS[w.type], ...JSON.parse(JSON.stringify(w.options ?? {})) } : {} }, { immediate: true })

const KINDS = ['flow', 'group', 'datasource', 'view', 'sql', 'script', 'notebook', 'report', 'dashboard', 'dataapi']
const zones = computed<string[]>(() => {
  try { return (Intl as any).supportedValuesOf('timeZone') as string[] } catch { return ['UTC', 'Europe/Rome', 'Europe/London', 'America/New_York', 'Asia/Tokyo'] }
})
const browserZone = Intl.DateTimeFormat().resolvedOptions().timeZone

// shortcuts: the places of this person's bar, ticked or not; a path of another kind stays listed
// not the home itself: a shortcut on the page to the page
const places = computed(() => keyHelp.sections.value.filter((s) => s.to !== '/').map((s) => ({ to: s.to, label: t(s.label) })))
const others = computed(() => ((draft.value.items ?? []) as string[]).filter((p) => !NAV_SECTIONS.some((s) => s.to === p)))
function togglePlace(to: string, on: boolean) {
  const now = new Set<string>(draft.value.items ?? [])
  if (on) now.add(to); else now.delete(to)
  // in the bar's order, then the others as they were
  draft.value.items = [...NAV_SECTIONS.map((s) => s.to).filter((p) => now.has(p)), ...others.value.filter((p) => now.has(p))]
}
function toggleKind(kind: string, on: boolean) {
  const now = new Set<string>(draft.value.kinds ?? [])
  if (on) now.add(kind); else now.delete(kind)
  draft.value.kinds = KINDS.filter((k) => now.has(k))
}
// flow_runs and datasources: up to a few objects picked from those the person may see
const pickKey = computed(() => (props.widget?.type === 'flow_runs' ? 'flow_ids' : 'ids'))
const pickMax = computed(() => (props.widget?.type === 'flow_runs' ? 5 : 10))
const pickFrom = ref<{ id: number; name: string; project_id: number }[] | null>(null)
const pickFilter = ref('')
const { pathOf } = useFolderPaths()
watch(() => props.widget?.type, async (type) => {
  pickFrom.value = null
  pickFilter.value = ''
  try {
    if (type === 'flow_runs') pickFrom.value = (await useFlows().list()).map((f: any) => ({ id: f.id, name: f.name, project_id: f.project_id }))
    else if (type === 'datasources') pickFrom.value = (await useDatasources().list()).map((d: any) => ({ id: d.id, name: d.name, project_id: d.project_id }))
  } catch { pickFrom.value = [] }
}, { immediate: true })
const picked = computed<number[]>(() => draft.value[pickKey.value] ?? [])
const pickShown = computed(() => {
  const q = pickFilter.value.trim().toLowerCase()
  return (pickFrom.value ?? []).filter((o) => !q || o.name.toLowerCase().includes(q) || picked.value.includes(o.id)).slice(0, 60)
})
function togglePick(id: number, on: boolean) {
  const now = picked.value.filter((x) => x !== id)
  if (on && now.length < pickMax.value) now.push(id)
  draft.value[pickKey.value] = now
}
const clamp = (v: unknown, def: number, min: number, max: number) => {
  const n = Math.round(Number(v))
  return Number.isFinite(n) ? Math.min(max, Math.max(min, n)) : def
}
function save() {
  const w = props.widget
  if (!w) return
  const d = draft.value
  const out: Record<string, any> =
    w.type === 'clock' ? { zone: d.zone ?? '', hour12: !!d.hour12, seconds: !!d.seconds }
    : w.type === 'recent' ? { limit: clamp(d.limit, 8, 5, 20), kinds: d.kinds ?? [] }
    : w.type === 'failed_runs' ? { scope: d.scope === 'mine' ? 'mine' : 'all', days: clamp(d.days, 7, 1, 30) }
    : w.type === 'next_runs' ? { scope: d.scope === 'mine' ? 'mine' : 'all', hours: clamp(d.hours, 24, 1, 168) }
    : w.type === 'flow_runs' ? { flow_ids: d.flow_ids ?? [], runs: clamp(d.runs, 20, 5, 50) }
    : w.type === 'runs_by_day' ? { days: clamp(d.days, 30, 7, 90) }
    : w.type === 'datasources' ? { ids: d.ids ?? [] }
    : w.type === 'shortcuts' ? { items: d.items ?? [] }
    : w.type === 'note' ? { text: String(d.text ?? '').slice(0, 4000) }
    : {}
  emit('save', out)
}
</script>

<template>
  <Modal :open="!!widget" :title="widget ? $t('home.optionsOf', { name: $t(`home.widgets.${widget.type}`) }) : ''" :width="520" @close="emit('close')">
    <form v-if="widget" id="home-options" class="opts" @submit.prevent="save">
      <template v-if="widget.type === 'clock'">
        <label class="field">
          <span>{{ $t('home.clock.zone') }}</span>
          <select v-model="draft.zone">
            <option value="">{{ $t('home.clock.browserZone', { zone: browserZone }) }}</option>
            <option v-for="z in zones" :key="z" :value="z">{{ z }}</option>
          </select>
        </label>
        <label class="sw"><input v-model="draft.hour12" type="checkbox" role="switch" /> {{ $t('home.clock.hour12') }}</label>
        <label class="sw"><input v-model="draft.seconds" type="checkbox" role="switch" /> {{ $t('home.clock.seconds') }}</label>
      </template>

      <template v-else-if="widget.type === 'recent'">
        <label class="field">
          <span>{{ $t('home.recent.limit') }}</span>
          <input v-model.number="draft.limit" type="number" min="5" max="20" required />
        </label>
        <fieldset>
          <legend>{{ $t('home.recent.kinds') }}</legend>
          <p class="hint">{{ $t('home.recent.kindsHint') }}</p>
          <label v-for="k in KINDS" :key="k" class="chk">
            <input type="checkbox" :checked="(draft.kinds ?? []).includes(k)" @change="toggleKind(k, ($event.target as HTMLInputElement).checked)" />
            {{ $t(`home.kinds.${k}`) }}
          </label>
        </fieldset>
      </template>

      <template v-else-if="widget.type === 'failed_runs'">
        <fieldset>
          <legend>{{ $t('home.failed.scope') }}</legend>
          <label class="chk"><input v-model="draft.scope" type="radio" value="all" /> {{ $t('home.failed.scopeAll') }}</label>
          <label class="chk"><input v-model="draft.scope" type="radio" value="mine" /> {{ $t('home.failed.scopeMine') }}</label>
        </fieldset>
        <label class="field">
          <span>{{ $t('home.failed.days') }}</span>
          <input v-model.number="draft.days" type="number" min="1" max="30" required />
        </label>
      </template>

      <template v-else-if="widget.type === 'next_runs'">
        <fieldset>
          <legend>{{ $t('home.failed.scope') }}</legend>
          <label class="chk"><input v-model="draft.scope" type="radio" value="all" /> {{ $t('home.failed.scopeAll') }}</label>
          <label class="chk"><input v-model="draft.scope" type="radio" value="mine" /> {{ $t('home.nextRuns.scopeMine') }}</label>
        </fieldset>
        <label class="field">
          <span>{{ $t('home.nextRuns.hours') }}</span>
          <input v-model.number="draft.hours" type="number" min="1" max="168" required />
        </label>
      </template>

      <template v-else-if="widget.type === 'runs_by_day'">
        <label class="field">
          <span>{{ $t('home.byDay.days') }}</span>
          <input v-model.number="draft.days" type="number" min="7" max="90" required />
        </label>
      </template>

      <template v-else-if="widget.type === 'flow_runs' || widget.type === 'datasources'">
        <fieldset class="pick">
          <legend>{{ $t(widget.type === 'flow_runs' ? 'home.flowRuns.pick' : 'home.datasources.pick', { n: pickMax }) }}</legend>
          <p class="hint">{{ $t(widget.type === 'flow_runs' ? 'home.flowRuns.pickHint' : 'home.datasources.pickHint') }}</p>
          <input v-model="pickFilter" type="search" class="filter" :placeholder="$t('home.filter')" :aria-label="$t('home.filter')" />
          <p v-if="pickFrom === null" class="hint">…</p>
          <p v-else-if="!pickShown.length" class="hint">{{ $t('home.nothingFound') }}</p>
          <div v-else class="picklist">
            <label v-for="o in pickShown" :key="o.id" class="chk">
              <input type="checkbox" :checked="picked.includes(o.id)" :disabled="!picked.includes(o.id) && picked.length >= pickMax" @change="togglePick(o.id, ($event.target as HTMLInputElement).checked)" />
              <span class="pn">{{ o.name }}</span> <span class="pw">{{ pathOf(o.project_id) }}</span>
            </label>
          </div>
        </fieldset>
        <label v-if="widget.type === 'flow_runs'" class="field">
          <span>{{ $t('home.flowRuns.runs') }}</span>
          <input v-model.number="draft.runs" type="number" min="5" max="50" required />
        </label>
      </template>

      <template v-else-if="widget.type === 'shortcuts'">
        <fieldset>
          <legend>{{ $t('home.shortcuts.places') }}</legend>
          <label v-for="p in places" :key="p.to" class="chk">
            <input type="checkbox" :checked="(draft.items ?? []).includes(p.to)" @change="togglePlace(p.to, ($event.target as HTMLInputElement).checked)" />
            {{ p.label }}
          </label>
          <label v-for="p in others" :key="p" class="chk">
            <input type="checkbox" checked @change="togglePlace(p, ($event.target as HTMLInputElement).checked)" /> <code>{{ p }}</code>
          </label>
        </fieldset>
      </template>

      <template v-else-if="widget.type === 'note'">
        <label class="field">
          <span>{{ $t('home.note.text') }}</span>
          <textarea v-model="draft.text" rows="9" maxlength="4000" :placeholder="$t('home.note.placeholder')" />
        </label>
        <p class="hint">{{ $t('home.note.hint') }}</p>
      </template>
    </form>
    <template #footer>
      <span style="flex: 1" />
      <button type="button" @click="emit('close')">{{ $t('home.cancel') }}</button>
      <button type="submit" form="home-options" class="primary">{{ $t('home.save') }}</button>
    </template>
  </Modal>
</template>

<style scoped>
.opts { display: flex; flex-direction: column; gap: 14px; }
.field { display: flex; flex-direction: column; gap: 6px; font-size: 13px; }
.field > span, legend { font-weight: 600; font-size: 13px; }
.field input[type="number"] { width: 110px; }
.field textarea { width: 100%; resize: vertical; font: inherit; }
fieldset { border: 1px solid var(--border-soft); border-radius: var(--radius); padding: 10px 12px; margin: 0; display: flex; flex-wrap: wrap; gap: 6px 18px; }
legend { padding: 0 6px; }
.chk, .sw { display: inline-flex; align-items: center; gap: 8px; font-size: 13px; min-height: 28px; }
.hint { margin: 0; color: var(--muted); font-size: 12.5px; flex-basis: 100%; }
.pick { flex-direction: column; flex-wrap: nowrap; }
.filter { width: 100%; }
.picklist { display: flex; flex-direction: column; max-height: 240px; overflow: auto; margin: 0 -4px; padding: 0 4px; }
.pn { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pw { color: var(--muted); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>

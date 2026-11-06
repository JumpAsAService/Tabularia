<script setup lang="ts">
// «Customize» of the flow editor's right-click menu: up to eight transformations, in their
// order (the first at 12 o'clock, the others clockwise). Saved per person on the gateway
// (PUT /users/me/flow-shortcuts); «Restore defaults» saves null, so the person follows the
// defaults again.
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ArrowDown, ArrowUp, Minus, Plus } from 'lucide-vue-next'
import Modal from '~/components/ui/Modal.vue'
import { opMeta } from '~/composables/useOpIcons'
import { FLOW_SHORTCUTS_DEFAULT, FLOW_SHORTCUTS_MAX, flowShortcutsShown } from '~/composables/flowShortcuts'

const props = defineProps<{ open: boolean; available: string[]; current: string[]; saving?: boolean }>()
const emit = defineEmits<{ (e: 'save', list: string[] | null): void; (e: 'close'): void }>()
const { t } = useI18n()

const chosen = ref<string[]>([])
const restored = ref(false) // «Restore defaults» and nothing changed since: null is saved
watch(() => props.open, (o) => {
  if (!o) return
  chosen.value = [...props.current]
  restored.value = false
}, { immediate: true })

const offered = computed(() => props.available.filter((o) => o !== 'foreach'))
const rest = computed(() => offered.value.filter((o) => !chosen.value.includes(o)))
const full = computed(() => chosen.value.length >= FLOW_SHORTCUTS_MAX)

function changed(list: string[]) {
  chosen.value = list
  restored.value = false
}
function move(i: number, by: number) {
  const j = i + by
  if (j < 0 || j >= chosen.value.length) return
  const list = [...chosen.value]
  ;[list[i], list[j]] = [list[j], list[i]]
  changed(list)
}
function remove(i: number) { changed(chosen.value.filter((_, k) => k !== i)) }
function add(op: string) { if (!full.value) changed([...chosen.value, op]) }
function restore() {
  chosen.value = flowShortcutsShown(FLOW_SHORTCUTS_DEFAULT, props.available)
  restored.value = true
}
function save() { emit('save', restored.value ? null : [...chosen.value]) }
</script>

<template>
  <Modal :open="open" :title="t('quickMenu.customizeTitle')" :width="460" @close="emit('close')">
    <p class="qs-help">{{ t('quickMenu.customizeHelp', { max: FLOW_SHORTCUTS_MAX }) }}</p>

    <div class="qs-head">
      <h3>{{ t('quickMenu.chosen') }}</h3>
      <span class="qs-count" :class="{ full }" aria-live="polite">{{ t('quickMenu.count', { n: chosen.length, max: FLOW_SHORTCUTS_MAX }) }}</span>
    </div>
    <ol v-if="chosen.length" class="qs-list" data-test="qs-chosen">
      <li v-for="(op, i) in chosen" :key="op" :style="{ '--c': opMeta(op).color }">
        <component :is="opMeta(op).icon" :size="15" class="qs-icon" aria-hidden="true" />
        <span class="qs-name">{{ t(opMeta(op).label) }}</span>
        <button type="button" class="mini qs-btn" :disabled="i === 0" :aria-label="`${t('quickMenu.moveUp')}: ${t(opMeta(op).label)}`" :title="t('quickMenu.moveUp')" @click="move(i, -1)"><ArrowUp :size="14" /></button>
        <button type="button" class="mini qs-btn" :disabled="i === chosen.length - 1" :aria-label="`${t('quickMenu.moveDown')}: ${t(opMeta(op).label)}`" :title="t('quickMenu.moveDown')" @click="move(i, 1)"><ArrowDown :size="14" /></button>
        <button type="button" class="mini qs-btn" :aria-label="`${t('quickMenu.remove')}: ${t(opMeta(op).label)}`" :title="t('quickMenu.remove')" @click="remove(i)"><Minus :size="14" /></button>
      </li>
    </ol>
    <p v-else class="qs-empty">{{ t('quickMenu.empty') }}</p>

    <div class="qs-head">
      <h3>{{ t('quickMenu.available') }}</h3>
      <span v-if="full" class="qs-note">{{ t('quickMenu.full', { max: FLOW_SHORTCUTS_MAX }) }}</span>
    </div>
    <ul class="qs-list qs-rest" data-test="qs-available">
      <li v-for="op in rest" :key="op" :style="{ '--c': opMeta(op).color }">
        <component :is="opMeta(op).icon" :size="15" class="qs-icon" aria-hidden="true" />
        <span class="qs-name">{{ t(opMeta(op).label) }}</span>
        <button type="button" class="mini qs-btn" :disabled="full" :aria-label="`${t('quickMenu.add')}: ${t(opMeta(op).label)}`" :title="t('quickMenu.add')" @click="add(op)"><Plus :size="14" /></button>
      </li>
    </ul>

    <template #footer>
      <button type="button" class="qs-restore" @click="restore">{{ t('quickMenu.restore') }}</button>
      <span class="qs-spacer" />
      <button type="button" @click="emit('close')">{{ t('quickMenu.cancel') }}</button>
      <button type="button" class="primary" :disabled="saving" @click="save">{{ t('quickMenu.save') }}</button>
    </template>
  </Modal>
</template>

<style scoped>
.qs-help { margin: 0 0 12px; color: var(--muted); font-size: 13px; line-height: 1.45; }
.qs-head { display: flex; align-items: baseline; gap: 8px; margin: 14px 0 6px; }
.qs-head:first-of-type { margin-top: 0; }
.qs-head h3 { margin: 0; font-size: 13px; font-weight: 600; flex: 1; }
.qs-count { font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }
.qs-count.full { color: var(--text); }
.qs-note { font-size: 12px; color: var(--muted); }
.qs-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
.qs-rest { max-height: 220px; overflow-y: auto; }
.qs-list li {
  display: flex; align-items: center; gap: 8px; padding: 4px 6px; border-radius: 6px; font-size: 13px;
}
.qs-list li:hover { background: var(--panel-2); }
.qs-icon { color: var(--c); flex: none; }
.qs-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.qs-btn { display: inline-grid; place-items: center; padding: 3px 6px; }
.qs-empty { margin: 0; font-size: 13px; color: var(--muted); }
.qs-spacer { flex: 1; }
</style>

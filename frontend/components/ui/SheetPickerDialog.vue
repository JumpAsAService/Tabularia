<script setup lang="ts">
// After uploading a workbook with several sheets: which ones to import. The sheet
// already read (the first visible one) comes ticked; every sheet ticked becomes a
// source on the canvas, read from the same file. Closing keeps what was read.
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { FileSpreadsheet, EyeOff } from 'lucide-vue-next'
import Modal from '~/components/ui/Modal.vue'

const props = defineProps<{
  open: boolean
  filename: string
  sheets: string[]
  hidden: string[]
  read: string
}>()
const emit = defineEmits<{ (e: 'choose', sheets: string[]): void; (e: 'close'): void }>()

const { t } = useI18n()
const chosen = ref<string[]>([])
watch(() => props.open, (o) => { if (o) chosen.value = [props.read] }, { immediate: true })

function toggle(name: string) {
  chosen.value = chosen.value.includes(name) ? chosen.value.filter((n) => n !== name) : [...chosen.value, name]
}
// in the workbook's order, whatever the order of the clicks
const ordered = computed(() => props.sheets.filter((s) => chosen.value.includes(s)))
const onlyRead = computed(() => ordered.value.length === 1 && ordered.value[0] === props.read)
</script>

<template>
  <Modal :open="open" :title="t('sheetPicker.title')" :width="460" @close="emit('close')">
    <p class="intro">{{ t('sheetPicker.intro', { file: filename, n: sheets.length, read }) }}</p>
    <ul class="sheets">
      <li v-for="s in sheets" :key="s">
        <label :class="{ on: chosen.includes(s) }">
          <input type="checkbox" :checked="chosen.includes(s)" @change="toggle(s)" />
          <FileSpreadsheet :size="14" class="ico" />
          <span class="name">{{ s }}</span>
          <span v-if="s === read" class="tag">{{ t('sheetPicker.read') }}</span>
          <span v-if="hidden.includes(s)" class="tag muted"><EyeOff :size="11" /> {{ t('sheetPicker.hidden') }}</span>
        </label>
      </li>
    </ul>
    <template #footer>
      <button type="button" class="keep" @click="emit('close')">{{ t('sheetPicker.keep', { read }) }}</button>
      <button type="button" class="primary" :disabled="!ordered.length || onlyRead" @click="emit('choose', ordered)">
        {{ t('sheetPicker.import', { n: ordered.length }, ordered.length) }}
      </button>
    </template>
  </Modal>
</template>

<style scoped>
.keep { margin-left: auto; }
.intro { margin: 0 0 12px; font-size: 13px; line-height: 1.5; color: var(--muted); }
.sheets { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; max-height: 320px; overflow-y: auto; }
.sheets label {
  display: flex; align-items: center; gap: 8px; padding: 8px 10px;
  border: 1px solid var(--border); border-radius: 8px; cursor: pointer;
  font-size: 13px; color: var(--text); transition: border-color 0.12s, background 0.12s;
}
.sheets label:hover { border-color: var(--accent); }
.sheets label.on { border-color: color-mix(in srgb, var(--accent) 60%, var(--border)); background: color-mix(in srgb, var(--accent) 7%, transparent); }
.sheets input { width: auto; margin: 0; }
.ico { color: var(--muted); flex-shrink: 0; }
.name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tag { display: inline-flex; align-items: center; gap: 3px; flex-shrink: 0; font-size: 11px; padding: 1px 7px; border-radius: 999px; background: var(--panel-2); color: var(--text); }
.tag.muted { color: var(--muted); }
</style>

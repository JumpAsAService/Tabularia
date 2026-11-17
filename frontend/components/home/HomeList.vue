<script setup lang="ts">
// The shell of a widget that lists things read from the gateway: loading, failed (with «Try
// again»), empty, or the rows. Read again every `every` seconds while the tab shows, never while
// it is hidden; one widget's failure stays in that widget.
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = defineProps<{ load: () => Promise<unknown[]>; every?: number; watchKey?: string; emptyText: string; failedText: string }>()
const rows = ref<unknown[] | null>(null)
const failed = ref(false)
let timer: ReturnType<typeof setInterval> | undefined

async function read() {
  try {
    rows.value = await props.load()
    failed.value = false
  } catch {
    failed.value = true
    if (rows.value === null) rows.value = []
  }
}
const whenShown = () => { if (!document.hidden) read() }
onMounted(() => {
  read()
  if (props.every) timer = setInterval(whenShown, props.every * 1000)
  document.addEventListener('visibilitychange', whenShown)
})
onBeforeUnmount(() => {
  clearInterval(timer)
  document.removeEventListener('visibilitychange', whenShown)
})
watch(() => props.watchKey, () => read())
</script>

<template>
  <div class="hl" :aria-busy="rows === null">
    <p v-if="rows === null" class="msg"><span class="sk" style="width: 55%" /></p>
    <p v-else-if="failed && !rows.length" class="msg" role="alert">
      {{ failedText }} <button type="button" class="retry" @click="read">{{ $t('home.retry') }}</button>
    </p>
    <p v-else-if="!rows.length" class="msg">{{ emptyText }}</p>
    <ul v-else class="rows"><slot :rows="rows" /></ul>
  </div>
</template>

<style scoped>
.hl { height: 100%; overflow: auto; }
.msg { margin: 2px 0; color: var(--muted); font-size: 13px; }
.msg .sk { display: inline-block; height: 12px; }
.retry { min-height: 0; padding: 0 2px; border: none; background: none; color: var(--accent); font-size: 13px; text-decoration: underline; }
.rows { list-style: none; margin: 0; padding: 0; }
.rows :deep(.row) {
  display: flex; align-items: center; gap: 8px; min-height: 30px; padding: 3px 6px; margin: 0 -6px;
  border-radius: 7px; color: var(--text); text-decoration: none; font-size: 13px;
}
.rows :deep(.row:hover), .rows :deep(.row:focus-visible) { background: var(--tint-accent); }
.rows :deep(.ic) { color: var(--muted); flex-shrink: 0; }
.rows :deep(.ic.ko) { color: var(--danger); }
.rows :deep(.name) { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; flex: 0 1 auto; }
.rows :deep(.sub) { color: var(--muted); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; flex: 1 1 0; }
.rows :deep(.sub.err) { color: var(--danger-text, var(--danger)); }
.rows :deep(.when) { margin-left: auto; color: var(--muted); font-size: 12px; white-space: nowrap; font-variant-numeric: tabular-nums; flex-shrink: 0; }
</style>

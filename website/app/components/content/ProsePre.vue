<script setup lang="ts">
// A code block of the docs with a copy button. The button shows only when scripts run
// (<html data-js>, set in the head): without them the block is just the code.
import { commandText } from '~/utils/code-copy'

const props = defineProps({
  code: { type: String, default: '' },
  language: { type: String, default: null },
  filename: { type: String, default: null },
  highlights: { type: Array, default: () => [] },
  meta: { type: String, default: null },
  class: { type: String, default: null },
})
const t = useCopy().docs
const state = ref<'idle' | 'copied' | 'failed'>('idle')
let timer: ReturnType<typeof setTimeout> | undefined

async function copy () {
  const text = commandText(props.code)
  let ok = false
  try {
    await navigator.clipboard.writeText(text)
    ok = true
  } catch {
    // no clipboard API (plain http, an old browser): the selection way
    try {
      const area = document.createElement('textarea')
      area.value = text
      area.setAttribute('readonly', '')
      area.style.cssText = 'position:fixed;top:0;left:0;opacity:0'
      document.body.append(area)
      area.select()
      ok = document.execCommand('copy')
      area.remove()
    } catch { ok = false }
  }
  state.value = ok ? 'copied' : 'failed'
  clearTimeout(timer)
  timer = setTimeout(() => { state.value = 'idle' }, 2000)
}
onBeforeUnmount(() => clearTimeout(timer))
</script>

<template>
  <div class="code">
    <div class="code__bar">
      <button type="button" class="code__copy" :aria-label="t.copyLabel" @click="copy">
        <svg v-if="state === 'copied'" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5" /></svg>
        <svg v-else width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2" /><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" /></svg>
        <span aria-hidden="true">{{ state === 'copied' ? t.copied : t.copy }}</span>
      </button>
      <span class="sr-only" role="status">{{ state === 'copied' ? t.copied : state === 'failed' ? t.copyFailed : '' }}</span>
    </div>
    <pre :class="props.class"><slot /></pre>
  </div>
</template>

<style>
pre code .line { display: block; }
.prose .code { position: relative; }
.prose .code > pre { margin: 0; }
.prose .code__bar { display: none; }
html[data-js] .prose .code__bar {
  display: flex; align-items: center; justify-content: flex-end; gap: 12px;
  min-height: 36px; padding: 4px 6px 0 16px;
  background: var(--code-bg); border-radius: var(--radius) var(--radius) 0 0;
  font-size: 13px; color: var(--soft);
}
html[data-js] .prose .code__bar + pre { border-radius: 0 0 var(--radius) var(--radius); padding-top: 6px; }
.code__copy {
  display: inline-flex; align-items: center; gap: 6px; min-height: 32px; padding: 0 10px;
  border: 0; border-radius: 6px; background: none; color: var(--soft);
  font: inherit; font-size: 13px; font-weight: 600; cursor: pointer;
}
.code__copy:hover { color: var(--text); background: var(--tile-2); }
.code__copy:focus-visible { outline-offset: 0; }
</style>

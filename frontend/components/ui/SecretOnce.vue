<script setup lang="ts">
// A secret seen ONCE (an API token, a webhook's secret): in clear, with the button to copy
// it and the warning that it will not be seen again. Whoever uses it removes it with the
// `done` event when the person has finished.
import { ref } from 'vue'
import { Check, Copy, KeyRound } from 'lucide-vue-next'

const props = defineProps<{ value: string; label: string }>()
const emit = defineEmits<{ (e: 'done'): void }>()
const copied = ref(false)

async function copy() {
  try {
    await navigator.clipboard.writeText(props.value)
    copied.value = true
    setTimeout(() => (copied.value = false), 2000)
  } catch {
    // clipboard not available (http, permissions): the text stays to select by hand
  }
}
</script>

<template>
  <div class="secret" role="status">
    <p class="secret-head"><KeyRound :size="14" /> {{ label }}</p>
    <div class="secret-row">
      <code class="secret-value">{{ value }}</code>
      <button type="button" class="mini" :aria-label="$t('integrations.copy')" @click="copy">
        <Check v-if="copied" :size="13" /><Copy v-else :size="13" />
        {{ copied ? $t('integrations.copied') : $t('integrations.copy') }}
      </button>
    </div>
    <p class="secret-note">{{ $t('integrations.secretOnce') }}</p>
    <button type="button" class="secret-done" @click="emit('done')">{{ $t('integrations.secretDone') }}</button>
  </div>
</template>

<style scoped>
.secret {
  display: flex; flex-direction: column; gap: 8px;
  padding: 12px 14px; border: 1px solid color-mix(in srgb, var(--accent) 45%, var(--border));
  border-radius: 10px; background: var(--tint-accent);
}
.secret-head { margin: 0; font-size: 13px; font-weight: 600; display: inline-flex; align-items: center; gap: 7px; }
.secret-row { display: flex; align-items: center; gap: 8px; }
.secret-value {
  flex: 1; min-width: 0; padding: 7px 9px; border-radius: 7px; background: var(--bg-soft);
  border: 1px solid var(--control-border); font-size: 12.5px; overflow-wrap: anywhere; user-select: all;
}
.secret-note { margin: 0; font-size: 12px; color: var(--muted); }
.secret-done { align-self: flex-start; font-size: 12.5px; }
</style>

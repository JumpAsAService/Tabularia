<script setup lang="ts">
// Un segreto che si vede UNA volta (un token API, il segreto di un webhook): in
// chiaro, con il bottone per copiarlo e l'avviso che dopo non si rivedrà. Chi lo
// usa lo toglie con l'evento `done` quando la persona ha finito.
import { ref } from 'vue'
import { Check, Copy, KeyRound } from 'lucide-vue-next'

const props = defineProps<{ value: string; label: string }>()
const emit = defineEmits<{ (e: 'done'): void }>()
const copiato = ref(false)

async function copia() {
  try {
    await navigator.clipboard.writeText(props.value)
    copiato.value = true
    setTimeout(() => (copiato.value = false), 2000)
  } catch {
    // appunti non disponibili (http, permessi): resta il testo da selezionare a mano
  }
}
</script>

<template>
  <div class="secret" role="status">
    <p class="secret-head"><KeyRound :size="14" /> {{ label }}</p>
    <div class="secret-row">
      <code class="secret-value">{{ value }}</code>
      <button type="button" class="mini" :aria-label="$t('integrations.copy')" @click="copia">
        <Check v-if="copiato" :size="13" /><Copy v-else :size="13" />
        {{ copiato ? $t('integrations.copied') : $t('integrations.copy') }}
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

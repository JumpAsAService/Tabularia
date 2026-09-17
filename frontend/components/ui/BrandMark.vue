<script setup lang="ts">
// Il marchio di Tabularia: una griglia attraversata da un flusso — la tabella e
// la trasformazione. Ridisegnato in vettoriale dal logo raster originale (stessa
// idea, stessa sagoma), con due differenze volute:
//  - la GRIGLIA prende il colore del testo e il FLUSSO quello dell'accento: il
//    flusso è ciò che il prodotto fa, ed è lo stesso blu dei bottoni. Segue i
//    quattro temi da solo, quindi non serve più la tessera bianca che il PNG
//    (fondo bianco, navy fuori palette) si portava dietro ovunque;
//  - sotto i 24 px la griglia 3×3 diventa un grumo: lì si usa la variante
//    RIDOTTA (2×2, tratti più pieni), come fanno i marchi che devono vivere
//    anche in una favicon.
// Il vuoto attorno al flusso è una maschera, non un tratto del colore di fondo:
// il marchio sta su qualunque superficie (barra, pannello, card).
import { computed, useId } from 'vue'

const props = withDefaults(defineProps<{ size?: number; title?: string }>(), { size: 28, title: '' })
const small = computed(() => props.size < 24)
// id della maschera unico per istanza e identico fra server e browser
const maskId = `bm-${useId()}`
</script>

<template>
  <svg
    class="brand-mark-svg"
    :width="size"
    :height="size"
    viewBox="0 0 32 32"
    fill="none"
    :role="title ? 'img' : undefined"
    :aria-label="title || undefined"
    :aria-hidden="title ? undefined : 'true'"
    focusable="false"
  >
    <template v-if="small">
      <mask :id="maskId" maskUnits="userSpaceOnUse" x="0" y="0" width="32" height="32">
        <rect width="32" height="32" fill="#fff" />
        <path d="M0 12C13 12 16.5 25.5 32 25.5" stroke="#000" stroke-width="7.4" />
      </mask>
      <g :mask="`url(#${maskId})`" class="grid" stroke-width="3.1" stroke-linejoin="round">
        <rect x="3.6" y="3.6" width="24.8" height="24.8" rx="4.4" />
        <path d="M16 3.6v24.8M3.6 16h24.8" />
      </g>
      <path class="flow" d="M2 12C13 12 16.5 25.5 30 25.5" stroke-width="4.1" />
    </template>
    <template v-else>
      <mask :id="maskId" maskUnits="userSpaceOnUse" x="0" y="0" width="32" height="32">
        <rect width="32" height="32" fill="#fff" />
        <path d="M1 12.6C13 12.6 16.4 26.2 31 26.2" stroke="#000" stroke-width="7.2" />
      </mask>
      <g :mask="`url(#${maskId})`" class="grid" stroke-width="2.4" stroke-linejoin="round">
        <rect x="3.2" y="3.2" width="25.6" height="25.6" rx="4.6" />
        <path d="M11.73 3.2v25.6M20.27 3.2v25.6M3.2 11.73h25.6M3.2 20.27h25.6" />
      </g>
      <path class="flow" d="M2 12.6C13 12.6 16.4 26.2 30 26.2" stroke-width="3.4" />
    </template>
  </svg>
</template>

<style scoped>
.brand-mark-svg { display: block; flex: none; }
.grid { stroke: var(--text); }
.flow { stroke: var(--accent); }
</style>

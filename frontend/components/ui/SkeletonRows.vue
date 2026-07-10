<script setup lang="ts">
// Righe shimmer generiche per il caricamento delle pagine-lista.
withDefaults(defineProps<{ rows?: number }>(), { rows: 4 })

const WIDTHS = ['34%', '52%', '41%', '61%', '38%', '47%']
</script>

<template>
  <div class="skrows" aria-busy="true">
    <div v-for="i in rows" :key="i" class="skrow">
      <span class="sk" style="width: 15%" />
      <span class="sk" :style="{ width: WIDTHS[(i - 1) % WIDTHS.length] }" />
      <span class="sk skend" style="width: 9%" />
    </div>
  </div>
</template>

<style scoped>
.skrows { display: flex; flex-direction: column; gap: 16px; padding: 10px 0; }
.skrow { display: flex; gap: 16px; align-items: center; }
.skend { margin-left: auto; }
</style>

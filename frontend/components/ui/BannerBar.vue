<script setup lang="ts">
// Banner di avvertimento decisi dall'amministratore, in cima all'Explore e
// visibili a TUTTI. Non sono chiudibili di proposito: se un admin lo ha messo
// deve restare sotto gli occhi finché non lo toglie lui.
import { ref, onMounted } from 'vue'
import { Info, TriangleAlert, CircleAlert } from 'lucide-vue-next'
import { useBanners, type Banner } from '~/composables/useBanners'

const api = useBanners()
const banners = ref<Banner[]>([])

const ICONS: Record<string, any> = { info: Info, warning: TriangleAlert, danger: CircleAlert }
const LABELS: Record<string, string> = {
  info: 'banners.levelInfo',
  warning: 'banners.levelWarning',
  danger: 'banners.levelDanger',
}
const icona = (livello: string) => ICONS[livello] ?? Info
const etichetta = (livello: string) => LABELS[livello] ?? LABELS.info

onMounted(async () => {
  // un banner che non si carica non deve rompere l'Explore: si resta senza
  try {
    banners.value = await api.list()
  } catch {
    banners.value = []
  }
})
</script>

<template>
  <div v-if="banners.length" class="banners">
    <div v-for="b in banners" :key="b.id" class="banner" :class="b.level" role="status">
      <component :is="icona(b.level)" :size="16" :aria-label="$t(etichetta(b.level))" />
      <span>{{ b.message }}</span>
    </div>
  </div>
</template>

<style scoped>
.banners { display: flex; flex-direction: column; gap: 8px; margin-bottom: 12px; }
.banner {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 9px 13px;
  font-size: 13px;
  line-height: 1.45;
  border: 1px solid;
  border-left-width: 3px;
  border-radius: var(--radius);
  background: var(--panel);
}
/* i fallback tengono il banner leggibile anche se il tema non definisce la variabile */
.banner.info { border-color: var(--accent-hi, #4c8dff); color: var(--accent-hi, #4c8dff); }
.banner.warning { border-color: var(--warning, #d08700); color: var(--warning, #d08700); }
.banner.danger { border-color: var(--danger, #e5484d); color: var(--danger, #e5484d); }
.banner span { color: var(--text, inherit); }
</style>

<script setup lang="ts">
// Warning banners decided by the administrator, at the top of the Explore and visible to
// EVERYONE. They cannot be closed on purpose: if an admin put one up it must stay in sight
// until they take it down.
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
const iconOf = (level: string) => ICONS[level] ?? Info
const label = (level: string) => LABELS[level] ?? LABELS.info

onMounted(async () => {
  // a banner that does not load must not break the Explore: one stays without
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
      <component :is="iconOf(b.level)" :size="16" :aria-label="$t(label(b.level))" />
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
/* the fallbacks keep the banner readable even when the theme does not define the variable */
.banner.info { border-color: var(--accent-hi, #4c8dff); color: var(--accent-hi, #4c8dff); }
.banner.warning { border-color: var(--warning, #d08700); color: var(--warning, #d08700); }
.banner.danger { border-color: var(--danger, #e5484d); color: var(--danger, #e5484d); }
.banner span { color: var(--text, inherit); }
</style>

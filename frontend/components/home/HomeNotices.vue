<script setup lang="ts">
// The «Notices» widget: what the administrators posted for everyone (the banners of Explore).
import { Megaphone } from 'lucide-vue-next'
import HomeList from '~/components/home/HomeList.vue'
import { useBanners, type Banner } from '~/composables/useBanners'
import { useFormat } from '~/composables/useFormat'

defineProps<{ options: Record<string, any> }>()
const api = useBanners()
const fmt = useFormat()
</script>

<template>
  <HomeList v-slot="{ rows }" :load="() => api.list()" :every="300" :empty-text="$t('home.notices.empty')" :failed-text="$t('home.notices.failed')">
    <li v-for="b in (rows as Banner[])" :key="b.id" class="notice" :class="`lv-${b.level}`">
      <Megaphone :size="14" class="ic" aria-hidden="true" />
      <span class="text">{{ b.message }}</span>
      <span class="when">{{ fmt.ago(b.created_at) }}</span>
    </li>
  </HomeList>
</template>

<style scoped>
.notice { display: flex; align-items: flex-start; gap: 8px; padding: 5px 0; font-size: 13px; }
.notice .ic { margin-top: 3px; color: var(--accent); flex-shrink: 0; }
.notice.lv-warning .ic { color: var(--warning); }
.notice.lv-danger .ic, .notice.lv-error .ic { color: var(--danger); }
.text { flex: 1; min-width: 0; overflow-wrap: anywhere; white-space: pre-line; }
.when { color: var(--muted); font-size: 12px; white-space: nowrap; }
</style>

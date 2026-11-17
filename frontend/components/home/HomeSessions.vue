<script setup lang="ts">
// The «Sessions» widget, for administrators: who is signed in, those online first (the Audit
// page's list of open sessions).
import HomeList from '~/components/home/HomeList.vue'
import { useApi, type ActiveSession } from '~/composables/useApi'
import { useFormat } from '~/composables/useFormat'

defineProps<{ options: Record<string, any> }>()
const api = useApi()
const fmt = useFormat()
async function load(): Promise<ActiveSession[]> {
  const page = await api.auditSessions(50)
  const seen = new Map<number, ActiveSession>()
  for (const s of page.items as ActiveSession[]) { const had = seen.get(s.user_id); if (!had || (s.online && !had.online)) seen.set(s.user_id, s) }
  return [...seen.values()].sort((a, b) => Number(b.online) - Number(a.online)).slice(0, 20)
}
</script>

<template>
  <HomeList v-slot="{ rows }" :load="load" :every="60" :empty-text="$t('home.sessions.empty')" :failed-text="$t('home.sessions.failed')">
    <li v-for="s in (rows as ActiveSession[])" :key="s.user_id" class="who">
      <span class="dot" :class="{ on: s.online }" aria-hidden="true" />
      <span class="name">{{ s.full_name || s.email }}</span>
      <span class="sub">{{ s.full_name ? s.email : '' }}</span>
      <span class="when">{{ s.online ? $t('home.sessions.online') : fmt.ago((s as any).last_seen_at) }}</span>
    </li>
    <li><NuxtLink to="/audit" class="row all">{{ $t('home.sessions.open') }}</NuxtLink></li>
  </HomeList>
</template>

<style scoped>
.who { display: flex; align-items: center; gap: 8px; min-height: 30px; font-size: 13px; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--border); flex-shrink: 0; }
.dot.on { background: var(--accent-2); }
.who .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
.who .sub { color: var(--muted); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; flex: 1 1 0; }
.who .when { margin-left: auto; color: var(--muted); font-size: 12px; white-space: nowrap; }
</style>

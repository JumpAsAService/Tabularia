<script setup lang="ts">
// Shell dell'app: navbar con brand, sezioni e utente. Le pagine la usano come
// wrapper (<AppShell>…contenuto…</AppShell>); l'editor resta a tutto schermo.
import { computed, onMounted } from 'vue'
import {
  Table2,
  LogOut,
  FolderTree,
  Workflow,
  Database,
  Plug,
  Shield,
} from 'lucide-vue-next'

const { user, fetchMe, logout } = useAuth()
const route = useRoute()

const isSuper = computed(() => !!user.value?.is_superuser)

const links = computed(() => [
  { to: '/', label: 'Explore', icon: FolderTree },
  { to: '/flows', label: 'Flows', icon: Workflow },
  { to: '/datasources', label: 'Datasources', icon: Database },
  { to: '/connections', label: 'Connections', icon: Plug },
  ...(isSuper.value ? [{ to: '/admin', label: 'Admin', icon: Shield }] : []),
])

onMounted(async () => {
  if (!user.value) await fetchMe()
})
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <NuxtLink to="/" class="brand">
        <span class="brand-mark"><Table2 :size="15" /></span> Tabularia
      </NuxtLink>

      <nav class="mainnav">
        <NuxtLink
          v-for="l in links"
          :key="l.to"
          :to="l.to"
          class="navlink"
          :class="{ on: route.path === l.to }"
        >
          <component :is="l.icon" :size="14" /> {{ l.label }}
        </NuxtLink>
      </nav>

      <span class="spacer" />
      <span v-if="user" class="who muted">
        {{ user.email }}<template v-if="isSuper"> · admin</template>
      </span>
      <button class="logout" title="Sign out" @click="logout"><LogOut :size="14" /></button>
    </header>

    <main class="content">
      <slot />
    </main>
  </div>
</template>

<style scoped>
.shell { min-height: 100vh; display: flex; flex-direction: column; }
.topbar {
  position: sticky;
  top: 0;
  z-index: 100;
  display: flex;
  align-items: center;
  gap: 22px;
  padding: 0 20px;
  height: 52px;
  border-bottom: 1px solid var(--border-soft);
  background: rgba(20, 25, 38, 0.92);
  backdrop-filter: blur(8px);
}
.brand {
  font-size: 16.5px;
  font-weight: 700;
  letter-spacing: 0.3px;
  display: inline-flex;
  align-items: center;
  gap: 9px;
  color: var(--text);
  text-decoration: none;
  white-space: nowrap;
}
.brand-mark {
  width: 27px;
  height: 27px;
  border-radius: 8px;
  background: var(--grad-accent);
  color: #fff;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.mainnav { display: flex; align-items: center; gap: 2px; height: 100%; }
.navlink {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  height: 100%;
  padding: 0 13px;
  font-size: 13px;
  font-weight: 500;
  color: var(--muted);
  text-decoration: none;
  border-bottom: 2px solid transparent;
  border-top: 2px solid transparent; /* compensa: il testo resta centrato */
  transition: color 0.15s;
}
.navlink:hover { color: var(--text); }
.navlink.on { color: var(--text); border-bottom-color: var(--accent); }
.spacer { flex: 1; }
.who { font-size: 12.5px; white-space: nowrap; }
.logout { padding: 5px 9px; }

.content {
  flex: 1;
  display: flex;
  flex-direction: column;
  padding: 22px 24px;
  max-width: 1440px;
  width: 100%;
  margin: 0 auto;
}
</style>

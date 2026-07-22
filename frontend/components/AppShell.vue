<script setup lang="ts">
// Shell dell'app: navbar con brand, sezioni e utente. Le pagine la usano come
// wrapper (<AppShell>…contenuto…</AppShell>); l'editor resta a tutto schermo.
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  LogOut,
  FolderTree,
  Workflow,
  Database,
  Plug,
  Shield,
  Activity,
  History,
  Cpu,
  PieChart,
  Share2,
  ScrollText,
  Settings,
  Moon,
  Sun,
} from 'lucide-vue-next'

// fluid = contenuto a larghezza piena (no max-width centrato): per pagine come il
// Viewer che sfruttano tutta la larghezza. Le liste restano centrate (default).
defineProps<{ fluid?: boolean }>()

const { t } = useI18n()
const { user, fetchMe, logout } = useAuth()
const { theme, setTheme } = useTheme()
const { preferredEngine, setPreferredEngine, engineCatalog, loadCatalog } = usePreferredEngine()
const { locale, setLocale, locales } = useLocale()
const route = useRoute()

const isSuper = computed(() => !!user.value?.is_superuser)

// opzioni motore per il selettore (solo quelli disponibili)
const engineOptions = computed(() =>
  engineCatalog.value.filter((e) => e.available).map((e) => ({ value: e.id, label: e.label })),
)
// opzioni lingua per il selettore
const localeOptions = computed(() => locales.map((l) => ({ value: l.code, label: l.label })))

// menù impostazioni (ingranaggio in alto a destra): nome utente + tema + motore + logout
const menuOpen = ref(false)
function toggleMenu() {
  menuOpen.value = !menuOpen.value
  if (menuOpen.value) loadCatalog() // carica il catalogo engine alla prima apertura
}
function closeMenu() {
  menuOpen.value = false
}

const links = computed(() => [
  { to: '/', label: t('nav.explore'), icon: FolderTree },
  { to: '/flows', label: t('nav.flows'), icon: Workflow },
  { to: '/datasources', label: t('nav.datasources'), icon: Database },
  { to: '/lineage', label: t('nav.lineage'), icon: Share2 },
  { to: '/viewer', label: t('nav.viewer'), icon: PieChart },
  { to: '/connections', label: t('nav.connections'), icon: Plug },
  { to: '/runs', label: t('nav.runs'), icon: History },
  ...(isSuper.value
    ? [
        { to: '/queue', label: t('nav.queue'), icon: Cpu },
        { to: '/monitoring', label: t('nav.monitoring'), icon: Activity },
        { to: '/audit', label: t('nav.audit'), icon: ScrollText },
        { to: '/admin', label: t('nav.admin'), icon: Shield },
      ]
    : []),
])

onMounted(async () => {
  if (!user.value) await fetchMe()
})
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <NuxtLink to="/" class="brand">
        <span class="brand-mark"><img src="/logo.png" alt="Tabularia" /></span> Tabularia
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
      <MemoryGauge />

      <!-- impostazioni: ingranaggio → nome utente completo, tema, logout -->
      <div class="settings">
        <button
          class="gear"
          :class="{ on: menuOpen }"
          :title="t('settings.title')"
          @click="toggleMenu"
        >
          <Settings :size="16" />
        </button>

        <template v-if="menuOpen">
          <div class="menu-backdrop" @click="closeMenu" />
          <div class="menu">
            <div v-if="user" class="menu-user">
              <span class="menu-email">{{ user.email }}</span>
              <span v-if="isSuper" class="menu-role">{{ t('settings.admin') }}</span>
            </div>

            <div class="menu-sep" />

            <div class="menu-label">{{ t('settings.theme') }}</div>
            <div class="theme-switch">
              <button
                :class="{ active: theme === 'light' }"
                @click="setTheme('light')"
              >
                <Sun :size="14" /> {{ t('settings.light') }}
              </button>
              <button
                :class="{ active: theme === 'dark' }"
                @click="setTheme('dark')"
              >
                <Moon :size="14" /> {{ t('settings.dark') }}
              </button>
            </div>

            <div class="menu-sep" />

            <div class="menu-label">{{ t('settings.language') }}</div>
            <Select
              :model-value="locale"
              :options="localeOptions"
              class="engpref"
              @update:model-value="setLocale($event as any)"
            />

            <div class="menu-sep" />

            <div class="menu-label">{{ t('settings.engine') }}</div>
            <Select
              :model-value="preferredEngine"
              :options="engineOptions"
              class="engpref"
              @update:model-value="setPreferredEngine($event as string)"
            />
            <span class="menu-hint">{{ t('settings.engineHint') }}</span>

            <div class="menu-sep" />

            <button class="menu-item" @click="closeMenu(); logout()">
              <LogOut :size="14" /> {{ t('settings.signOut') }}
            </button>
          </div>
        </template>
      </div>
    </header>

    <main class="content" :class="{ fluid }">
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
  background: var(--topbar-bg);
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
  background: #fff;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
}
.brand-mark img { width: 100%; height: 100%; object-fit: contain; }
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

/* ── Impostazioni (ingranaggio + menù) ──────────────────────────────────── */
.settings { position: relative; }
.gear { padding: 6px 8px; color: var(--muted); }
.gear:hover, .gear.on { color: var(--text); border-color: var(--accent); }
/* backdrop invisibile: click fuori → chiude (niente listener globali) */
.menu-backdrop { position: fixed; inset: 0; z-index: 200; }
.menu {
  position: absolute;
  right: 0;
  top: calc(100% + 8px);
  z-index: 201;
  min-width: 230px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  box-shadow: var(--shadow-2);
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.menu-user { display: flex; flex-direction: column; gap: 3px; padding: 4px 6px; }
.menu-email { font-size: 12.5px; font-weight: 600; word-break: break-all; }
.menu-role {
  align-self: flex-start;
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 0.03em;
  text-transform: uppercase;
  color: var(--accent-2);
  background: var(--tint-accent);
  border-radius: 5px;
  padding: 1px 6px;
}
.menu-sep { height: 1px; background: var(--border-soft); margin: 2px 0; }
.menu-label { font-size: 11px; color: var(--muted); padding: 0 6px; }
.menu-hint { font-size: 10.5px; color: var(--muted); padding: 0 6px; line-height: 1.35; opacity: 0.85; }
.engpref { width: 100%; }
.engpref :deep(.sel-trigger) { width: 100%; }
.theme-switch { display: flex; gap: 6px; }
.theme-switch button {
  flex: 1;
  font-size: 12px;
  padding: 6px 8px;
  gap: 5px;
}
.theme-switch button.active {
  border-color: var(--accent);
  background: var(--tint-accent);
  color: var(--text);
}
.menu-item {
  justify-content: flex-start;
  font-size: 12.5px;
  padding: 7px 8px;
  width: 100%;
}

.content {
  flex: 1;
  display: flex;
  flex-direction: column;
  padding: 22px 24px;
  max-width: 1440px;
  width: 100%;
  margin: 0 auto;
}
.content.fluid { max-width: none; }
</style>

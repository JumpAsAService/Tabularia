<script setup lang="ts">
import BrandMark from '~/components/ui/BrandMark.vue'
import SessionsDialog from '~/components/ui/SessionsDialog.vue'
import ApiTokensDialog from '~/components/ui/ApiTokensDialog.vue'
import PasswordDialog from '~/components/ui/PasswordDialog.vue'
import { useAppInfo } from '~/composables/useAppInfo'
import { useScriptingEditor } from '~/composables/useScripting'
import { useRegistriesEditor } from '~/composables/useRegistries'
// The app's shell: navbar with brand, sections and user. The pages use it as a wrapper
// (<AppShell>…content…</AppShell>); the editor stays full screen.
import { computed, onMounted, ref, nextTick } from 'vue'
import { usePopover } from '~/composables/usePopover'
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
  Gauge, Sparkles,
  ChevronDown,
  MonitorSmartphone,
  KeyRound,
  LockKeyhole,
  Terminal,
  Server,
} from 'lucide-vue-next'

// fluid = full-width content (no centred max-width): for pages like the Viewer that use
// the whole width. The lists stay centred (default).
defineProps<{ fluid?: boolean }>()

const { t } = useI18n()
const { user, fetchMe, logout } = useAuth()
const { theme, setTheme } = useTheme()
// name and version from the gateway: it is the release really running, not the bundle's
const { info: appInfo, load: loadAppInfo } = useAppInfo()
const { preferredEngine, setPreferredEngine, engineCatalog, loadCatalog } = usePreferredEngine()
const { locale, setLocale, locales } = useLocale()
const route = useRoute()

const isSuper = computed(() => !!user.value?.is_superuser)
// Who may SEE the panels: administrator or observer. The menu item looks at this, the
// commands inside the panels look at `isSuper`.
const canObserve = computed(() => isSuper.value || !!user.value?.is_observer)

// engine options for the selector (only the available ones)
const engineOptions = computed(() =>
  engineCatalog.value.filter((e) => e.available).map((e) => ({ value: e.id, label: e.label })),
)
// language options for the selector
const localeOptions = computed(() => locales.map((l) => ({ value: l.code, label: l.label })))
// theme options: light/dark translated, Dracula/Monokai proper names
const themeOptions = computed(() => [
  { value: 'dark', label: t('settings.dark') },
  { value: 'light', label: t('settings.light') },
  { value: 'dracula', label: 'Dracula' },
  { value: 'monokai', label: 'Monokai' },
  // the colours of Rome: marble and Tyrian purple (a proper name, as the two above)
  { value: 'imperium', label: 'Imperium' },
])

// settings menu (gear at the top right): user name + theme + engine + logout
const menuOpen = ref(false)
function toggleMenu() {
  menuOpen.value = !menuOpen.value
  if (menuOpen.value) loadCatalog() // loads the engines' catalog at the first opening
}
function closeMenu() {
  menuOpen.value = false
}
const settingsPanel = ref<HTMLElement | null>(null)
usePopover(menuOpen, settingsPanel, closeMenu)
// «Your sessions»: where one is connected, and closing the others
const sessionsOpen = ref(false)
// «API tokens»: the credentials of one's own scripts (when the installation allows them)
const tokensOpen = ref(false)
// «Change password»: one's own. Not for an observer only: the guest of a public
// demo is one account many people share (the gateway refuses it too)
const passwordOpen = ref(false)
const canChangePassword = computed(() => !!user.value && !(user.value.is_observer && !user.value.is_superuser))

// the Scripting tab (pages/scripting.vue): shown when there is something to show in it
const scriptingEditor = useScriptingEditor()
const registriesEditor = useRegistriesEditor()
onMounted(() => { scriptingEditor.load(); registriesEditor.load() })
const scriptingTab = computed(() => scriptingEditor.available.value || registriesEditor.available.value)

const links = computed(() => [
  { to: '/', label: t('nav.explore'), icon: FolderTree },
  { to: '/flows', label: t('nav.flows'), icon: Workflow },
  { to: '/datasources', label: t('nav.datasources'), icon: Database },
  { to: '/lineage', label: t('nav.lineage'), icon: Share2 },
  { to: '/viewer', label: t('nav.viewer'), icon: PieChart },
  { to: '/chat', label: t('nav.assistant'), icon: Sparkles },
  { to: '/connections', label: t('nav.connections'), icon: Plug },
  // only to whoever may use a scripting environment or a container registry
  ...(scriptingTab.value ? [{ to: '/scripting', label: t('nav.scripting'), icon: Terminal }] : []),
  { to: '/runs', label: t('nav.runs'), icon: History },
])

/* Administration: five destinations that in the product are ONE place, and in the bar
   were five items at the same level as Flows or Datasources. Gathered under one item
   with its menu, like the settings gear: the bar goes from thirteen elements to nine and
   tells a structure that exists already. */
const adminLinks = computed(() => [
  { to: '/admin', label: t('nav.admin'), icon: Shield },
  { to: '/queue', label: t('nav.queue'), icon: Cpu },
  { to: '/monitoring', label: t('nav.monitoring'), icon: Activity },
  { to: '/performance', label: t('nav.performance'), icon: Gauge },
  { to: '/cluster', label: t('nav.cluster'), icon: Server },
  { to: '/audit', label: t('nav.audit'), icon: ScrollText },
])
const adminOpen = ref(false)
const adminBtn = ref<HTMLElement | null>(null)
const adminStyle = ref<Record<string, string>>({})

/**
 * The menu is on the body: its position must be computed from the button, which may
 * have moved because the bar scrolls.
 */
function placeAdmin() {
  const r = adminBtn.value?.getBoundingClientRect()
  if (!r) return
  // anchored to the button's left, but never off screen on the right
  const left = Math.min(r.left, window.innerWidth - 226)
  adminStyle.value = { left: `${Math.max(8, left)}px`, top: `${r.bottom + 6}px` }
}

/**
 * The vertical wheel scrolls the bar horizontally: without it, with a mouse the hidden
 * items are reached only by dragging, which nobody discovers.
 */
function navWheel(ev: WheelEvent) {
  const el = ev.currentTarget as HTMLElement
  if (el.scrollWidth <= el.clientWidth || ev.deltaX) return
  el.scrollLeft += ev.deltaY
  ev.preventDefault()
}

const adminMenu = ref<HTMLElement | null>(null)
function toggleAdmin() {
  adminOpen.value = !adminOpen.value
  if (adminOpen.value) nextTick(() => { placeAdmin(); adminItem(0) })
}
// a real menu (role=menu): the arrows go through the items, Esc closes and goes back to
// the button
function adminItem(i: number) {
  const items = [...(adminMenu.value?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? [])]
  if (items.length) items[(i + items.length) % items.length].focus()
}
function onAdminKey(e: KeyboardEvent) {
  const items = [...(adminMenu.value?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? [])]
  const here = items.indexOf(document.activeElement as HTMLElement)
  if (e.key === 'ArrowDown') { e.preventDefault(); adminItem(here + 1) }
  else if (e.key === 'ArrowUp') { e.preventDefault(); adminItem(here - 1) }
  else if (e.key === 'Home') { e.preventDefault(); adminItem(0) }
  else if (e.key === 'End') { e.preventDefault(); adminItem(-1) }
  else if (e.key === 'Escape') { e.preventDefault(); adminOpen.value = false; adminBtn.value?.focus() }
  else if (e.key === 'Tab') adminOpen.value = false
}
// the item stays lit while in any of its pages
const inAdmin = computed(() => adminLinks.value.some((l) => route.path === l.to))

onMounted(async () => {
  if (!user.value) await fetchMe()
  await loadAppInfo() // only cosmetic here: it never fails
})
</script>

<template>
  <div v-label-fields class="app-shell">
    <header class="topbar">
      <NuxtLink to="/" class="brand">
        <BrandMark :size="28" /> <span class="brand-name">Tabularia</span>
      </NuxtLink>

      <nav class="mainnav" @wheel="navWheel">
        <NuxtLink
          v-for="l in links"
          :key="l.to"
          :to="l.to"
          class="navlink"
          :class="{ on: route.path === l.to }"
        >
          <component :is="l.icon" :size="14" /> {{ l.label }}
        </NuxtLink>

        <div v-if="canObserve" class="adminwrap">
          <button
            type="button"
            class="navlink"
            :class="{ on: inAdmin || adminOpen }"
            aria-haspopup="menu"
            :aria-expanded="adminOpen"
            ref="adminBtn"
            @click="toggleAdmin"
          >
            <Shield :size="14" /> {{ t('nav.administration') }}
            <ChevronDown :size="13" class="caret" :class="{ up: adminOpen }" />
          </button>

          <!-- teleported to the body: the navigation SCROLLS, and a container with overflow clips
               the positioned children — the menu ended up cut and seemed to open upwards. The same
               solution as Select.vue, which teleports its panel for the same reason. -->
          <Teleport v-if="adminOpen" to="body">
            <div class="menu-backdrop" @click="adminOpen = false" />
            <div ref="adminMenu" class="menu adminmenu" role="menu" :style="adminStyle" @keydown="onAdminKey">
              <NuxtLink
                v-for="l in adminLinks"
                :key="l.to"
                :to="l.to"
                class="menu-item"
                :class="{ on: route.path === l.to }"
                role="menuitem"
                @click="adminOpen = false"
              >
                <component :is="l.icon" :size="14" /> {{ l.label }}
              </NuxtLink>
            </div>
          </Teleport>
        </div>
      </nav>

      <span class="spacer" />
      <GlobalSearch />
      <MemoryGauge />

      <!-- settings: gear → full user name, theme, logout -->
      <div class="settings">
        <button
          class="gear"
          :class="{ on: menuOpen }"
          :title="t('settings.title')"
          :aria-label="t('settings.title')"
          :aria-expanded="menuOpen"
          @click="toggleMenu"
        >
          <Settings :size="16" />
        </button>

        <template v-if="menuOpen">
          <div class="menu-backdrop" @click="closeMenu" />
          <div ref="settingsPanel" class="menu">
            <div v-if="user" class="menu-user">
              <span class="menu-email">{{ user.email }}</span>
              <span v-if="isSuper" class="menu-role">{{ t('settings.admin') }}</span>
            </div>

            <div class="menu-sep" />

            <label class="menu-label">{{ t('settings.theme') }}</label>
            <Select
              :model-value="theme"
              :options="themeOptions"
              class="engpref"
              @update:model-value="setTheme($event as any)"
            />

            <div class="menu-sep" />

            <label class="menu-label">{{ t('settings.language') }}</label>
            <Select
              :model-value="locale"
              :options="localeOptions"
              class="engpref"
              @update:model-value="setLocale($event as any)"
            />

            <div class="menu-sep" />

            <label class="menu-label">{{ t('settings.engine') }}</label>
            <Select
              :model-value="preferredEngine"
              :options="engineOptions"
              class="engpref"
              @update:model-value="setPreferredEngine($event as string)"
            />
            <span class="menu-hint">{{ t('settings.engineHint') }}</span>

            <div class="menu-sep" />

            <button class="menu-item" @click="closeMenu(); sessionsOpen = true">
              <MonitorSmartphone :size="14" /> {{ t('settings.sessions') }}
            </button>
            <button v-if="canChangePassword" class="menu-item" @click="closeMenu(); passwordOpen = true">
              <LockKeyhole :size="14" /> {{ t('settings.changePassword') }}
            </button>
            <button v-if="user?.personal_tokens" class="menu-item" @click="closeMenu(); tokensOpen = true">
              <KeyRound :size="14" /> {{ t('settings.apiTokens') }}
            </button>
            <button class="menu-item" @click="closeMenu(); logout()">
              <LogOut :size="14" /> {{ t('settings.signOut') }}
            </button>
            <div v-if="appInfo" class="menu-version">{{ appInfo.name }} {{ appInfo.version }}<template v-if="appInfo.codename"> · {{ appInfo.codename }}</template></div>
          </div>
        </template>
      </div>
    </header>

    <main class="content" :class="{ fluid }">
      <slot />
    </main>
    <SessionsDialog :open="sessionsOpen" @close="sessionsOpen = false" />
    <ApiTokensDialog :open="tokensOpen" @close="tokensOpen = false" />
    <PasswordDialog :open="passwordOpen" @close="passwordOpen = false" />
  </div>
</template>

<style scoped>
/* `app-shell`, not a generic name: the ROOT element of a child component carries
   this component's scope attribute too, so a scoped rule here also styles any
   child whose root has the same class. The memory gauge's root had `shell` and
   became a 100vh-tall column — an invisible strip over the page that swallowed
   the clicks (folder «Create» button, 1.2.0 getting-started run). */
.app-shell { min-height: 100vh; display: flex; flex-direction: column; }
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
  flex: none; /* the navigation gives way, as it can scroll: not the brand */
}
/* The navigation scrolls WHEN NEEDED, not under a width decided by hand. The adaptation
   sat in a media query at 760px, but the room taken depends on the NUMBER of items — as
   admin there are thirteen — not on the window: between 760px and the width that would
   be needed, the items left the bar. It is the case of the browser's zoom, which reduces
   the CSS pixels without being «a small screen». `min-width: 0` is essential: without
   it, a flex child does not shrink under its content and the document ends up
   scrolling. */
.mainnav {
  display: flex;
  align-items: center;
  gap: 2px;
  height: 100%;
  min-width: 0;
  overflow-x: auto;
  overscroll-behavior-x: contain;
  /* A thin scrollbar, as everywhere in the product, and visible ONLY when the items do not
     fit: without it, with a mouse one does not understand the bar can scroll (with the
     finger one does, and that is why it was hidden before).
     Declared cost: on Firefox `scrollbar-width: thin` takes space, so when the bar is
     crowded the links lose a few pixels in height and the active item's underline goes up
     as much. It happens only in the crowded state, and it is the price for not hiding the
     only handhold. */
  scrollbar-width: thin;
  scrollbar-color: var(--border) transparent;
  scrollbar-gutter: auto;
}
.mainnav::-webkit-scrollbar { height: 6px; }
.mainnav::-webkit-scrollbar-track { background: transparent; }
.mainnav::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
.mainnav::-webkit-scrollbar-thumb:hover { background: var(--control-border); }
.navlink {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  height: 100%;
  padding: 0 13px;
  white-space: nowrap;
  flex: none;
  font-size: 13px;
  font-weight: 500;
  color: var(--muted);
  text-decoration: none;
  border-bottom: 2px solid transparent;
  border-top: 2px solid transparent; /* compensates: the text stays centred */
  transition: color 0.15s;
}
.navlink:hover { color: var(--text); }
.navlink.on { color: var(--text); border-bottom-color: var(--accent); }
.spacer { flex: 1 1 0; min-width: 0; }

/* ── Settings (gear + menu) ──────────────────────────────────────────── */
.settings { position: relative; }
.gear { padding: 6px 8px; color: var(--muted); }
.gear:hover, .gear.on { color: var(--text); border-color: var(--accent); }
/* invisible backdrop: click outside → closes (no global listeners) */
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
.menu-version { padding: 6px 6px 2px; font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums; }
.menu-label { display: block; font-size: 11px; color: var(--muted); padding: 0 6px; }
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

/* Narrow screens: the bar has up to eleven items plus the controls, and at a reduced
   width it pushed the page to scroll horizontally. The navigation scrolls inside its own
   bar instead of widening the document; the hamburger menu would be a redesign, not an
   adaptation. */
/* the administration menu reuses the settings panel: the same surface, the same radius,
   the same shadow — only where it hooks changes */
.adminwrap { position: relative; display: inline-flex; height: 100%; flex: none; }
.adminwrap .caret { opacity: 0.7; transition: transform 0.15s ease; }
.adminwrap .caret.up { transform: rotate(180deg); }
.adminmenu {
  position: fixed;
  right: auto;
  top: auto;
  min-width: 210px;
  padding: 4px;
}
.menu-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 9px;
  border-radius: 6px;
  font-size: 13px;
  color: var(--text);
  text-decoration: none;
}
.menu-item:hover { background: var(--panel-2); }
.menu-item.on { background: var(--tint-accent); color: var(--accent-hi); }

/* The navigation takes precedence: first the spaces shrink, then the brand's name leaves
   only the mark (it stays for the screen readers). The search and the memory indicator
   shrink by themselves. Measured: with nine admin items, at 1440px Connections, Runs and
   Administration ended up out of the visible part. */
@media (max-width: 1600px) {
  .topbar { gap: 16px; }
  .navlink { padding: 0 10px; }
}
@media (max-width: 1100px) {
  .brand-name {
    position: absolute; width: 1px; height: 1px; margin: -1px; overflow: hidden;
    clip: rect(0 0 0 0); clip-path: inset(50%); white-space: nowrap;
  }
}
@media (max-width: 760px) {
  .topbar { gap: 12px; padding: 0 12px; }
  .mainnav { flex: 1; }
  .spacer { display: none; } /* otherwise it splits the free space in half with the navigation */
  .navlink { padding: 0 9px; }
  .content { padding: 16px 14px; }
}
</style>

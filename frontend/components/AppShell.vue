<script setup lang="ts">
import BrandMark from '~/components/ui/BrandMark.vue'
import SessionsDialog from '~/components/ui/SessionsDialog.vue'
import ApiTokensDialog from '~/components/ui/ApiTokensDialog.vue'
import PasswordDialog from '~/components/ui/PasswordDialog.vue'
import GridKeys from '~/components/ui/GridKeys.vue'
import { useKeyHelp } from '~/composables/useKeyHelp'
import { useAppInfo } from '~/composables/useAppInfo'
import { useScriptingEditor } from '~/composables/useScripting'
import { useRegistriesEditor } from '~/composables/useRegistries'
// The app's shell: navbar with brand, sections and user. The pages use it as a wrapper
// (<AppShell>…content…</AppShell>); the editor stays full screen.
import { computed, onMounted, ref, nextTick, type Component } from 'vue'
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
  FileChartColumn,
  LayoutDashboard,
  ChartNoAxesCombined,
  Code2,
} from 'lucide-vue-next'

// fluid = full-width content (no centred max-width): for pages like the Viewer that use
// the whole width. The lists stay centred (default).
defineProps<{ fluid?: boolean }>()

const { t } = useI18n()
const { user, fetchMe, logout } = useAuth()
const runtimeConfig = useRuntimeConfig()
const sourceUrl = runtimeConfig.public.sourceUrl as string
const { theme, setTheme } = useTheme()
// name and version from the gateway: it is the release really running, not the bundle's
const { info: appInfo, load: loadAppInfo } = useAppInfo()
const { preferredEngine, setPreferredEngine, engineCatalog, loadCatalog } = usePreferredEngine()
const { locale, setLocale, locales } = useLocale()
const route = useRoute()

const isSuper = computed(() => !!user.value?.is_superuser)
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
// opened from the keyboard (a click with no pointer count): it shows and hides at once
const menuByKey = ref(false)
function toggleMenu(ev?: MouseEvent) {
  if (!menuOpen.value) menuByKey.value = ev?.detail === 0
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

// The list of keys (the keyboard button beside the gear, and «?»): the keys of everywhere
// and whatever the page on screen declared. This bar's button is where the list opens.
const keyHelp = useKeyHelp()
const appKeysBtn = ref<InstanceType<typeof GridKeys> | null>(null)
keyHelp.host(() => appKeysBtn.value?.toggle())

type NavLink = { to: string; label: string; icon: Component; keys: string }
type NavMenu = { menu: 'analysis' | 'admin'; label: string; icon: Component; links: NavLink[] }

// The places are one table (composables/appKeys.ts, NAV_SECTIONS), shared with the keys
// that lead to them: «g» then a place's letter goes only where this bar has a link.
const ICONS: Record<string, Component> = {
  explore: FolderTree, flows: Workflow, datasources: Database, lineage: Share2,
  viewer: PieChart, reports: FileChartColumn, dashboards: LayoutDashboard,
  assistant: Sparkles, connections: Plug, scripting: Terminal, runs: History,
  admin: Shield, queue: Cpu, monitoring: Activity, performance: Gauge, cluster: Server, audit: ScrollText,
}
/* Analysis: the places where data is read rather than prepared, the Viewer, the Reports
   and the Dashboards tabs. The bar had no room for one more item (measured on Chrome and Firefox: at
   1440px only the English bar fitted, with 44px to spare, and the item takes 83 to 98px
   by language), so the Viewer's place became a menu with both, as Administration.
   Administration: destinations that in the product are ONE place, and in the bar were
   items at the same level as Flows or Datasources. Gathered under one item with its menu,
   like the settings gear: the bar tells a structure that exists already. */
const MENUS: Record<NavMenu['menu'], { label: string; icon: Component }> = {
  analysis: { label: 'nav.analysis', icon: ChartNoAxesCombined },
  admin: { label: 'nav.administration', icon: Shield },
}

const items = computed<(NavLink | NavMenu)[]>(() => {
  const out: (NavLink | NavMenu)[] = []
  for (const s of keyHelp.sections.value) {
    const link: NavLink = { to: s.to, label: t(s.label), icon: ICONS[s.id], keys: `g ${s.key}` }
    if (!s.menu) { out.push(link); continue }
    const which = s.menu
    let menu = out.find((i): i is NavMenu => 'menu' in i && i.menu === which)
    if (!menu) out.push(menu = { menu: which, label: t(MENUS[which].label), icon: MENUS[which].icon, links: [] })
    menu.links.push(link)
  }
  return out
})
const isMenu = (i: NavLink | NavMenu): i is NavMenu => 'menu' in i

// one menu open at a time, its panel on the body
const openMenu = ref<NavMenu['menu'] | null>(null)
const openLinks = computed(() => items.value.find((i): i is NavMenu => isMenu(i) && i.menu === openMenu.value)?.links ?? [])
const menuBtns: Partial<Record<NavMenu['menu'], HTMLElement>> = {}
const menuStyle = ref<Record<string, string>>({})

/**
 * The menu is on the body: its position must be computed from the button, which may
 * have moved because the bar scrolls.
 */
function placeMenu() {
  const r = openMenu.value ? menuBtns[openMenu.value]?.getBoundingClientRect() : undefined
  if (!r) return
  // anchored to the button's left, but never off screen on the right
  const left = Math.min(r.left, window.innerWidth - 226)
  menuStyle.value = { left: `${Math.max(8, left)}px`, top: `${r.bottom + 6}px` }
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

const navMenu = ref<HTMLElement | null>(null)
function toggleNavMenu(which: NavMenu['menu']) {
  openMenu.value = openMenu.value === which ? null : which
  if (openMenu.value) nextTick(() => { placeMenu(); menuItem(0) })
}
function closeNavMenu(refocus = false) {
  const which = openMenu.value
  openMenu.value = null
  if (refocus && which) menuBtns[which]?.focus()
}
// a real menu (role=menu): the arrows go through the items, Esc closes and goes back to
// the button
function menuItem(i: number) {
  const links = [...(navMenu.value?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? [])]
  if (links.length) links[(i + links.length) % links.length].focus()
}
function onMenuKey(e: KeyboardEvent) {
  const links = [...(navMenu.value?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? [])]
  const here = links.indexOf(document.activeElement as HTMLElement)
  if (e.key === 'ArrowDown') { e.preventDefault(); menuItem(here + 1) }
  else if (e.key === 'ArrowUp') { e.preventDefault(); menuItem(here - 1) }
  else if (e.key === 'Home') { e.preventDefault(); menuItem(0) }
  else if (e.key === 'End') { e.preventDefault(); menuItem(-1) }
  else if (e.key === 'Escape') { e.preventDefault(); closeNavMenu(true) }
  else if (e.key === 'Tab') closeNavMenu()
}
// the item stays lit while in any of its pages
const inMenu = (m: NavMenu) => m.links.some((l) => route.path === l.to)

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
        <template v-for="i in items" :key="isMenu(i) ? i.menu : i.to">
          <div v-if="isMenu(i)" class="menuwrap">
            <button
              :ref="(el) => { if (el) menuBtns[i.menu] = el as HTMLElement }"
              type="button"
              class="navlink"
              :class="{ on: inMenu(i) || openMenu === i.menu }"
              aria-haspopup="menu"
              :aria-expanded="openMenu === i.menu"
              @click="toggleNavMenu(i.menu)"
            >
              <component :is="i.icon" :size="14" /> {{ i.label }}
              <ChevronDown :size="13" class="caret" :class="{ up: openMenu === i.menu }" />
            </button>
          </div>
          <NuxtLink v-else :to="i.to" class="navlink" :class="{ on: route.path === i.to }" :aria-keyshortcuts="i.keys">
            <component :is="i.icon" :size="14" /> {{ i.label }}
          </NuxtLink>
        </template>

        <!-- teleported to the body: the navigation SCROLLS, and a container with overflow clips
             the positioned children — the menu ended up cut and seemed to open upwards. The same
             solution as Select.vue, which teleports its panel for the same reason. -->
        <Teleport v-if="openMenu" to="body">
          <div class="menu-backdrop" @click="closeNavMenu()" />
          <div ref="navMenu" class="menu navmenu" role="menu" :style="menuStyle" @keydown="onMenuKey">
            <NuxtLink
              v-for="l in openLinks"
              :key="l.to"
              :to="l.to"
              class="menu-item"
              :class="{ on: route.path === l.to }"
              role="menuitem"
              :aria-keyshortcuts="l.keys"
              @click="closeNavMenu()"
            >
              <component :is="l.icon" :size="14" /> {{ l.label }}
            </NuxtLink>
          </div>
        </Teleport>
      </nav>

      <span class="spacer" />
      <GlobalSearch />
      <MemoryGauge />
      <!-- every key of the app and of this page; «?» opens it too -->
      <GridKeys
        ref="appKeysBtn"
        class="appkeys"
        wide
        fixed
        :groups="keyHelp.groups.value"
        :title="t('appKeys.title')"
        :label="t('appKeys.button')"
      />

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

        <div v-if="menuOpen" class="menu-backdrop" @click="closeMenu" />
        <Transition name="pop" :css="!menuByKey">
          <div v-if="menuOpen" ref="settingsPanel" class="menu">
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
            <a class="menu-item" :href="sourceUrl" target="_blank" rel="noopener" @click="closeMenu()">
              <Code2 :size="14" /> {{ t('settings.sourceCode') }}
            </a>
            <a class="menu-item" :href="`${runtimeConfig.app.baseURL}third-party-notices.txt`" target="_blank" rel="noopener" @click="closeMenu()">
              <ScrollText :size="14" /> {{ t('settings.licences') }}
            </a>
            <button class="menu-item" @click="closeMenu(); logout()">
              <LogOut :size="14" /> {{ t('settings.signOut') }}
            </button>
            <div v-if="appInfo" class="menu-version">{{ appInfo.name }} {{ appInfo.version }}<template v-if="appInfo.codename"> · {{ appInfo.codename }}</template></div>
          </div>
        </Transition>
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
  transition: color var(--dur-2);
}
.navlink:hover { color: var(--text); }
.navlink.on { color: var(--text); border-bottom-color: var(--accent); }
/* hover: the current section's underline grows from the centre, at half strength, under
   the link the pointer is on (a preview of where a click lands) */
.navlink { position: relative; }
@media (hover: hover) and (pointer: fine) {
  .navlink:not(.on)::after {
    content: "";
    position: absolute;
    left: 13px;
    right: 13px;
    bottom: -2px;
    height: 2px;
    background: var(--accent);
    opacity: 0.5;
    transform: scaleX(0);
    transition: transform var(--dur-hover) var(--ease-out);
    pointer-events: none;
  }
  .navlink:not(.on):hover::after { transform: scaleX(1); }
}
@media (prefers-reduced-motion: reduce) {
  .navlink:not(.on)::after { transform: none; opacity: 0; transition: opacity var(--dur-hover); }
  .navlink:not(.on):hover::after { opacity: 0.5; }
}
.spacer { flex: 1 1 0; min-width: 0; }

/* ── Settings (gear + menu) ──────────────────────────────────────────── */
.settings { position: relative; }
/* the keyboard button stands close to the gear and the gauge: the bar's gap on both of its
   sides would take from the navigation the room of a whole link (it scrolls when crowded) */
.appkeys { margin-inline: -10px; }
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
/* the Analysis and Administration menus reuse the settings panel: the same surface, the
   same radius, the same shadow — only where it hooks changes */
.menuwrap { position: relative; display: inline-flex; height: 100%; flex: none; }
/* a menu's button reads as the links beside it: without this it took the app's button
   (a chip with a fill, borders and a shadow on hover), a box in the middle of the bar */
.menuwrap .navlink {
  background: transparent;
  border-left-color: transparent;
  border-right-color: transparent;
  border-radius: 0;
  box-shadow: none;
  transform: none;
}
.menuwrap .navlink:not(.on) { border-bottom-color: transparent; border-top-color: transparent; }
.menuwrap .caret { opacity: 0.7; transition: transform var(--dur-2) ease; }
.menuwrap .caret.up { transform: rotate(180deg); }
.navmenu {
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
/* 9px, not 10: the keyboard button took 20px of the bar, and eleven links give them back a
   pixel a side (measured at 1500px: the Italian bar fits again, as before the button) */
@media (max-width: 1600px) {
  .topbar { gap: 16px; }
  .navlink { padding: 0 9px; }
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

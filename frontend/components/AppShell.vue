<script setup lang="ts">
import BrandMark from '~/components/ui/BrandMark.vue'
import { useAppInfo } from '~/composables/useAppInfo'
// Shell dell'app: navbar con brand, sezioni e utente. Le pagine la usano come
// wrapper (<AppShell>…contenuto…</AppShell>); l'editor resta a tutto schermo.
import { computed, onMounted, ref, nextTick } from 'vue'
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
} from 'lucide-vue-next'

// fluid = contenuto a larghezza piena (no max-width centrato): per pagine come il
// Viewer che sfruttano tutta la larghezza. Le liste restano centrate (default).
defineProps<{ fluid?: boolean }>()

const { t } = useI18n()
const { user, fetchMe, logout } = useAuth()
const { theme, setTheme } = useTheme()
// nome e versione dal gateway: e' la release che gira davvero, non quella del bundle
const { info: appInfo, load: loadAppInfo } = useAppInfo()
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
// opzioni tema: chiaro/scuro tradotti, Dracula/Monokai nomi propri
const themeOptions = computed(() => [
  { value: 'dark', label: t('settings.dark') },
  { value: 'light', label: t('settings.light') },
  { value: 'dracula', label: 'Dracula' },
  { value: 'monokai', label: 'Monokai' },
])

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
  { to: '/chat', label: t('nav.assistant'), icon: Sparkles },
  { to: '/connections', label: t('nav.connections'), icon: Plug },
  { to: '/runs', label: t('nav.runs'), icon: History },
])

/* Amministrazione: cinque destinazioni che nel prodotto sono UN posto, e nella
   barra erano cinque voci allo stesso livello di Flussi o Datasource. Raccolte
   sotto una sola voce col suo menù, come l'ingranaggio delle impostazioni: la
   barra passa da tredici elementi a nove e dice una struttura che esiste già. */
const adminLinks = computed(() => [
  { to: '/admin', label: t('nav.admin'), icon: Shield },
  { to: '/queue', label: t('nav.queue'), icon: Cpu },
  { to: '/monitoring', label: t('nav.monitoring'), icon: Activity },
  { to: '/performance', label: t('nav.performance'), icon: Gauge },
  { to: '/audit', label: t('nav.audit'), icon: ScrollText },
])
const adminOpen = ref(false)
const adminBtn = ref<HTMLElement | null>(null)
const adminStyle = ref<Record<string, string>>({})

/** Il menù è sul body: la sua posizione va calcolata dal bottone, che può
 *  essersi spostato perché la barra scorre. */
function posizionaAdmin() {
  const r = adminBtn.value?.getBoundingClientRect()
  if (!r) return
  // ancorato a sinistra del bottone, ma mai fuori dallo schermo a destra
  const left = Math.min(r.left, window.innerWidth - 226)
  adminStyle.value = { left: `${Math.max(8, left)}px`, top: `${r.bottom + 6}px` }
}

/** La rotellina verticale scorre la barra in orizzontale: senza, con un mouse
 *  le voci nascoste si raggiungono solo trascinando, che non si scopre. */
function navWheel(ev: WheelEvent) {
  const el = ev.currentTarget as HTMLElement
  if (el.scrollWidth <= el.clientWidth || ev.deltaX) return
  el.scrollLeft += ev.deltaY
  ev.preventDefault()
}

function toggleAdmin() {
  adminOpen.value = !adminOpen.value
  if (adminOpen.value) nextTick(posizionaAdmin)
}
// la voce resta accesa mentre si è in una qualsiasi delle sue pagine
const inAdmin = computed(() => adminLinks.value.some((l) => route.path === l.to))

onMounted(async () => {
  if (!user.value) await fetchMe()
  await loadAppInfo() // solo cosmetico qui: non fallisce mai
})
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <NuxtLink to="/" class="brand">
        <BrandMark :size="26" /> Tabularia
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

        <div v-if="isSuper" class="adminwrap">
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

          <!-- teleportato sul body: la navigazione SCORRE, e un contenitore
               con overflow ritaglia i figli posizionati — il menù finiva
               tagliato e sembrava aprirsi verso l'alto. Stessa soluzione di
               Select.vue, che per lo stesso motivo teleporta il suo pannello. -->
          <Teleport v-if="adminOpen" to="body">
            <div class="menu-backdrop" @click="adminOpen = false" />
            <div class="menu adminmenu" role="menu" :style="adminStyle">
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

      <!-- impostazioni: ingranaggio → nome utente completo, tema, logout -->
      <div class="settings">
        <button
          class="gear"
          :class="{ on: menuOpen }"
          :title="t('settings.title')"
          :aria-label="t('settings.title')"
          aria-haspopup="menu"
          :aria-expanded="menuOpen"
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
            <Select
              :model-value="theme"
              :options="themeOptions"
              class="engpref"
              @update:model-value="setTheme($event as any)"
            />

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
            <div v-if="appInfo" class="menu-version">{{ appInfo.name }} {{ appInfo.version }}<template v-if="appInfo.codename"> · {{ appInfo.codename }}</template></div>
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
  flex: none; /* a cedere è la navigazione, che sa scorrere: non il marchio */
}
/* La navigazione scorre QUANDO SERVE, non sotto una larghezza decisa a mano.
   L'adattamento stava in una media query a 760px, ma l'ingombro dipende dal
   NUMERO di voci — da amministratore sono tredici — non dalla finestra: fra i
   760px e la larghezza che servirebbe, le voci uscivano dalla barra. È il caso
   dello zoom del browser, che riduce i pixel CSS senza essere «uno schermo
   piccolo». `min-width: 0` è indispensabile: senza, un figlio flex non si
   restringe sotto il proprio contenuto e a scorrere finisce il documento. */
.mainnav {
  display: flex;
  align-items: center;
  gap: 2px;
  height: 100%;
  min-width: 0;
  overflow-x: auto;
  overscroll-behavior-x: contain;
  /* Scrollbar sottile, come ovunque nel prodotto, e visibile SOLO quando le voci
     non entrano: senza, con un mouse non si capisce che la barra si può
     scorrere (con il dito sì, e per questo prima era nascosta).
     Costo dichiarato: su Firefox `scrollbar-width: thin` occupa spazio, quindi
     quando la barra è affollata i link perdono qualche pixel in altezza e la
     sottolineatura della voce attiva sale di altrettanto. Succede solo nello
     stato affollato, ed è il prezzo per non nascondere l'unico appiglio. */
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
  border-top: 2px solid transparent; /* compensa: il testo resta centrato */
  transition: color 0.15s;
}
.navlink:hover { color: var(--text); }
.navlink.on { color: var(--text); border-bottom-color: var(--accent); }
.spacer { flex: 1 1 0; min-width: 0; }

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
.menu-version { padding: 6px 6px 2px; font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums; }
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

/* Schermi stretti: la barra ha fino a undici voci più i controlli, e a larghezza
   ridotta spingeva la pagina a scorrere in orizzontale. La navigazione scorre
   dentro la propria barra invece di allargare il documento; il menù a panino
   sarebbe una riprogettazione, non un adattamento. */
/* il menù dell'amministrazione riusa il pannello delle impostazioni: stessa
   superficie, stesso raggio, stessa ombra — cambia solo dove si aggancia */
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

@media (max-width: 760px) {
  .topbar { gap: 12px; padding: 0 12px; }
  .mainnav { flex: 1; }
  .navlink { padding: 0 9px; }
  .content { padding: 16px 14px; }
}
</style>

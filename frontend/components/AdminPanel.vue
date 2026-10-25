<script setup lang="ts">
// Amministrazione (solo superuser), divisa in SEZIONI con navigazione a sinistra:
// utenti, gruppi, banner. Prima era tutto impilato in un'unica schermata e le
// azioni distruttive stavano accanto ai form di creazione. Il feedback passa dai
// toast; le conferme dai confirm() del browser, come nel resto dell'app.
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useFormat } from '~/composables/useFormat'
import {
  CheckCircle2, Cpu, Eye, Plus, Search, Shield, ShieldCheck, Trash2, TriangleAlert,
  User as UserIcon, Users as UsersIcon, X, XCircle, Sparkles, Bot, Webhook as WebhookIcon,
  SlidersHorizontal, Radar, Server, KeyRound, Code2, Boxes, DatabaseBackup,
} from 'lucide-vue-next'
// import espliciti: nel container i componenti nuovi non vengono scansionati
import ServiceAccountsSection from '~/components/admin/ServiceAccountsSection.vue'
import WebhooksSection from '~/components/admin/WebhooksSection.vue'
import SettingsSection from '~/components/admin/SettingsSection.vue'
import SiemSection from '~/components/admin/SiemSection.vue'
import BackupsSection from '~/components/admin/BackupsSection.vue'
import WorkersSection from '~/components/admin/WorkersSection.vue'
import ScriptingSection from '~/components/admin/ScriptingSection.vue'
import RegistriesSection from '~/components/admin/RegistriesSection.vue'
import PasswordDialog from '~/components/ui/PasswordDialog.vue'
import { passwordProblem, usePasswordRules } from '~/composables/usePasswordRules'
import { useSiem } from '~/composables/useSiem'
import { useBackups } from '~/composables/useBackups'
import { isSettingsCategory, useEngineSettings } from '~/composables/useEngineSettings'
import { usePanelSettings } from '~/composables/usePanelSettings'
import { useWorkers } from '~/composables/useWorkers'
import { useScripting } from '~/composables/useScripting'
import { useRegistries } from '~/composables/useRegistries'
import { errMessage } from '~/composables/useApi'
import { useProjects, type GroupOut, type Project, type UserOut } from '~/composables/useProjects'
import { useBanners, type Banner, type BannerLevel } from '~/composables/useBanners'
import { usePrivacy, type PrivacyNotice } from '~/composables/usePrivacy'
import { useEnginePolicy, type EnginePolicy } from '~/composables/useEnginePolicy'
import { useAi, type AiModel } from '~/composables/useAi'

const api = useProjects()
const bannersApi = useBanners()
const privacyApi = usePrivacy()
const engineApi = useEnginePolicy()
const aiApi = useAi()
const integr = useIntegrations()
const impostazioniApi = useEngineSettings()
const pannelloApi = usePanelSettings()
const siemApi = useSiem()
const backupsApi = useBackups()
const workersApi = useWorkers()
const scriptingApi = useScripting()
const registriesApi = useRegistries()
// modelli AI: catalogo del provider + scelta dell'amministratore. `null` =
// assistente non configurato o provider irraggiungibile (la sezione lo dice)
const modelli = ref<AiModel[] | null>(null)
const modelliErrore = ref('')
const toast = useToast()
const { user: me } = useAuth()
// Un OSSERVATORE apre i pannelli e non li tocca. Si nasconde invece di
// disabilitare: un modulo grigio invita a provarci e poi frustra, mentre il
// server risponderebbe 403 comunque. I dati personali che si vedono qui sono
// già mascherati dal server (services/masking.py).
const canWrite = computed(() => !!me.value?.is_superuser)
const { t } = useI18n()
const fmtx = useFormat()

type Sezione = 'users' | 'groups' | 'service' | 'webhooks' | 'banners' | 'privacy' | 'engines' | 'settings' | 'workers' | 'scripting' | 'registries' | 'siem' | 'backups' | 'ai'
const sezione = ref<Sezione>('users')
// the Settings section with changes not saved: asked before going to another section
const impostazioniDaSalvare = ref(false)
function vaiA(id: Sezione) {
  if (id === sezione.value) return
  if (sezione.value === 'settings' && impostazioniDaSalvare.value && !confirm(t('panelSettings.leaveConfirm'))) return
  sezione.value = id
}

const users = ref<UserOut[]>([])
const groups = ref<GroupOut[]>([])
const banners = ref<Banner[]>([])
const privacy = ref<PrivacyNotice | null>(null)
const salvandoPrivacy = ref(false)
const motori = ref<EnginePolicy[]>([])

const nu = ref({ email: '', password: '', full_name: '', is_superuser: false, is_observer: false })
const ng = ref({ name: '', description: '' })
const nb = ref<{ message: string; level: BannerLevel }>({ message: '', level: 'warning' })
const gruppoSelezionato = ref<number | null>(null)
const daAggiungere = ref<number | null>(null)

// ricerca: sottostringa, senza distinzione fra maiuscole e minuscole
const qUsers = ref('')
const qGroups = ref('')

function combacia(q: string, ...campi: (string | null | undefined)[]): boolean {
  const ago = q.trim().toLowerCase()
  if (!ago) return true
  return campi.filter(Boolean).join(' ').toLowerCase().includes(ago)
}

// le PERSONE: gli account di servizio hanno la loro sezione
const persone = computed(() => users.value.filter((u) => !u.is_service))
const utentiFiltrati = computed(() =>
  persone.value.filter((u) => combacia(qUsers.value, u.email, u.full_name, u.groups.join(' '))),
)
// conteggi delle due sezioni che caricano da sé (account di servizio, webhook)
const nServizio = ref(0)
const nWebhook = ref(0)
// quante impostazioni del motore sono cambiate dal pannello (le altre valgono dall'ambiente)
const nImpostazioni = ref(0)
const nWorker = ref(0)
// the scripting environments defined
const nAmbienti = ref(0)
const nRegistri = ref(0)
// without Kubernetes (Docker Compose) the two sections are shown greyed out, not opened
const scriptingOff = ref(false)
const registriesOff = ref(false)
// l'inoltro dell'audit al SIEM: acceso o no
const siemAcceso = ref(false)
// the database backups in the object storage
const nBackup = ref(0)
// on Docker Compose the backups are greyed out like the other features of a cluster
const backupsOff = ref(false)
// le cartelle, per il filtro dei webhook
const cartelle = ref<Project[]>([])
const gruppiFiltrati = computed(() =>
  groups.value.filter((g) => combacia(qGroups.value, g.name, g.description)),
)

const sezioni = computed(() => [
  { id: 'users' as Sezione, label: t('adminPanel.usersTitle'), icon: UserIcon, badge: persone.value.length },
  { id: 'groups' as Sezione, label: t('adminPanel.groupsTitle'), icon: UsersIcon, badge: groups.value.length },
  { id: 'service' as Sezione, label: t('integrations.serviceAccountsTitle'), icon: Bot, badge: nServizio.value },
  { id: 'webhooks' as Sezione, label: t('integrations.webhooksTitle'), icon: WebhookIcon, badge: nWebhook.value },
  { id: 'banners' as Sezione, label: t('adminPanel.bannersTitle'), icon: TriangleAlert, badge: banners.value.length },
  { id: 'privacy' as Sezione, label: t('adminPanel.privacyTitle'), icon: ShieldCheck, badge: 0 },
  // il conteggio è quanti motori sono CONSENTITI: è il numero che descrive lo
  // stato dell'installazione, non quanti ne esistono
  { id: 'engines' as Sezione, label: t('adminPanel.enginesTitle'), icon: Cpu, badge: motori.value.filter((m) => m.allowed).length },
  { id: 'settings' as Sezione, label: t('engineSettings.navTitle'), icon: SlidersHorizontal, badge: nImpostazioni.value },
  { id: 'backups' as Sezione, label: t('backups.navTitle'), icon: DatabaseBackup, badge: nBackup.value, off: backupsOff.value },
  // the workers alive now
  { id: 'workers' as Sezione, label: t('workers.navTitle'), icon: Server, badge: nWorker.value },
  { id: 'scripting' as Sezione, label: t('scripting.navTitle'), icon: Code2, badge: nAmbienti.value, off: scriptingOff.value },
  { id: 'registries' as Sezione, label: t('registries.navTitle'), icon: Boxes, badge: nRegistri.value, off: registriesOff.value },
  { id: 'siem' as Sezione, label: t('siem.navTitle'), icon: Radar, badge: siemAcceso.value ? 1 : 0 },
  // quanti modelli sono ABILITATI: zero = l'assistente non e' usabile da nessuno
  { id: 'ai' as Sezione, label: t('adminPanel.aiTitle'), icon: Sparkles, badge: (modelli.value ?? []).filter((m) => m.enabled).length },
])

// stessa convenzione delle altre pagine: formattatore locale, tollerante
function quando(iso: string | null): string {
  return fmtx.dateTime(iso)
}

const gruppoCorrente = computed(() => groups.value.find((g) => g.id === gruppoSelezionato.value) ?? null)
// i membri si ricavano dagli utenti già caricati: nessuna chiamata in più
const membri = computed(() =>
  gruppoCorrente.value ? users.value.filter((u) => u.groups.includes(gruppoCorrente.value!.name)) : [],
)
const nonMembri = computed(() =>
  gruppoCorrente.value ? users.value.filter((u) => !u.groups.includes(gruppoCorrente.value!.name)) : [],
)

async function loadAll() {
  try {
    users.value = await api.users()
    groups.value = await api.groups()
    banners.value = await bannersApi.list()
    try { privacy.value = await privacyApi.get() } catch { privacy.value = null }
    motori.value = await engineApi.list()
    cartelle.value = await api.list()
  } catch (e) {
    toast.error(errMessage(e))
  }
  // i conteggi delle due sezioni che caricano da sé, per la navigazione
  try {
    ;[nServizio.value, nWebhook.value] = (await Promise.all([integr.serviceAccounts(), integr.webhooks()])).map((x) => x.length)
  } catch { /* le sezioni lo diranno quando si aprono */ }
  try {
    // the processes per worker are engine settings too, but they have their own section
    const [gw, eng] = await Promise.allSettled([pannelloApi.gateway(), impostazioniApi.get()])
    nImpostazioni.value = (gw.status === 'fulfilled' ? gw.value.items.filter((v) => v.value !== null).length : 0)
      + (eng.status === 'fulfilled' ? eng.value.items.filter((v) => v.value !== null && isSettingsCategory(v.category)).length : 0)
  } catch { /* la sezione lo dirà quando si apre */ }
  try {
    siemAcceso.value = (await siemApi.get()).enabled
  } catch { /* la sezione lo dirà quando si apre */ }
  try {
    const bk = await backupsApi.get()
    nBackup.value = bk.items.length
    backupsOff.value = bk.disabled_reason === 'kubernetes'
  } catch { /* the section says why when it opens */ }
  try {
    nWorker.value = (await workersApi.get()).groups.reduce((n, g) => n + g.workers.length, 0)
  } catch { /* the section says why when it opens */ }
  try {
    const sc = await scriptingApi.get()
    nAmbienti.value = sc.environments.length
    scriptingOff.value = sc.disabled_reason === 'kubernetes'
  } catch { /* the section says why when it opens */ }
  try {
    const rg = await registriesApi.get()
    nRegistri.value = rg.registries.length
    registriesOff.value = rg.disabled_reason === 'kubernetes'
  } catch { /* the section says why when it opens */ }
  // a parte: l'assistente puo' non essere configurato, e non e' un errore del pannello
  try {
    modelli.value = await aiApi.models()
    modelliErrore.value = ''
  } catch (e) {
    modelli.value = null
    modelliErrore.value = errMessage(e)
  }
}
onMounted(loadAll)

// flussi rimasti su un motore non più consentito: è la lista di lavoro della
// migrazione. Disabilitare non ferma nulla, quindi senza questo numero il
// cambiamento non avrebbe alcuna conseguenza visibile.
const daMigrare = computed(() =>
  motori.value.filter((m) => !m.allowed).reduce((n, m) => n + m.flows_using, 0),
)

// ── utenti ──────────────────────────────────────────────────────────────────
// the one rule of a new password (gateway core/passwords.py), checked before sending
const { rules: passwordRules, load: loadPasswordRules } = usePasswordRules()
onMounted(loadPasswordRules)

async function createUser() {
  if (!nu.value.email) {
    toast.error(t('adminPanel.emailPasswordRequired', { n: passwordRules.value.min_length }))
    return
  }
  const bad = passwordProblem(nu.value.password, passwordRules.value)
  if (bad) {
    toast.error(t(bad.key, bad.values))
    return
  }
  try {
    await api.createUser({ ...nu.value })
    toast.success(t('adminPanel.userCreated', { email: nu.value.email }))
    nu.value = { email: '', password: '', full_name: '', is_superuser: false, is_observer: false }
    await loadAll()
  } catch (e) {
    toast.error(errMessage(e))
  }
}

async function toggleActive(u: UserOut) {
  try {
    await api.updateUser(u.id, { is_active: !u.is_active })
    toast.success(t(u.is_active ? 'adminPanel.userDisabled' : 'adminPanel.userEnabled', { email: u.email }))
    await loadAll()
  } catch (e) {
    toast.error(errMessage(e))
  }
}

// Admin PERSONALE. Chi lo è già tramite un gruppo resta admin anche senza: per
// questo l'interruttore su sé stessi è fermo solo quando nessun gruppo lo copre
// (il server lo rifiuterebbe comunque: non ci si chiude fuori da soli).
const selfLockout = (u: UserOut) => u.id === me.value?.id && u.is_superuser && !u.admin_groups.length

async function toggleAdmin(u: UserOut) {
  if (!u.is_superuser && !confirm(t('adminPanel.confirmPromoteUser', { email: u.email }))) return
  try {
    await api.updateUser(u.id, { is_superuser: !u.is_superuser })
    toast.success(t(u.is_superuser ? 'adminPanel.userDemoted' : 'adminPanel.userPromoted', { email: u.email }))
    await loadAll()
  } catch (e) {
    toast.error(errMessage(e))
  }
}

// Osservatore: apre i pannelli di amministrazione in LETTURA (audit, sessioni,
// utenti, motori, prestazioni), con i dati personali mascherati e nessun
// comando. È il flag dell'account dimostrativo: un ospite può guardare tutto
// senza toccare niente. Indipendente da admin, che legge già per definizione.
async function toggleObserver(u: UserOut) {
  if (!u.is_observer && !confirm(t('adminPanel.confirmObserveUser', { email: u.email }))) return
  try {
    await api.updateUser(u.id, { is_observer: !u.is_observer })
    toast.success(t(u.is_observer ? 'adminPanel.observerRevoked' : 'adminPanel.observerGranted', { email: u.email }))
    await loadAll()
  } catch (e) {
    toast.error(errMessage(e))
  }
}

// A password for another user (one's own goes through the settings menu, which
// asks for the current one). The user's sessions and API tokens close.
const passwordFor = ref<UserOut | null>(null)

async function deleteUser(u: UserOut) {
  if (!confirm(t('adminPanel.confirmDeleteUser', { email: u.email }))) return
  try {
    await api.deleteUser(u.id)
    toast.success(t('adminPanel.userDeleted', { email: u.email }))
    await loadAll()
  } catch (e) {
    toast.error(errMessage(e))
  }
}

// ── gruppi ──────────────────────────────────────────────────────────────────
async function createGroup() {
  if (!ng.value.name.trim()) {
    toast.error(t('adminPanel.groupNameRequired'))
    return
  }
  try {
    await api.createGroup({ ...ng.value })
    toast.success(t('adminPanel.groupCreated', { name: ng.value.name }))
    ng.value = { name: '', description: '' }
    await loadAll()
  } catch (e) {
    toast.error(errMessage(e))
  }
}

// Gruppo di amministratori: tutti i membri, presenti e futuri, sono admin
async function toggleGroupAdmin(g: GroupOut) {
  if (!g.is_admin && !confirm(t('adminPanel.confirmPromoteGroup', { name: g.name, n: g.member_count }))) return
  try {
    await api.updateGroup(g.id, { is_admin: !g.is_admin })
    toast.success(t(g.is_admin ? 'adminPanel.groupDemoted' : 'adminPanel.groupPromoted', { name: g.name }))
    await loadAll()
  } catch (e) {
    toast.error(errMessage(e))
  }
}

// Come sopra, per tutti i membri presenti e futuri: è la via da usare con l'SSO,
// dove il flag personale viene riscritto a ogni login dall'IdP
async function toggleGroupObserver(g: GroupOut) {
  if (!g.is_observer && !confirm(t('adminPanel.confirmObserveGroup', { name: g.name, n: g.member_count }))) return
  try {
    await api.updateGroup(g.id, { is_observer: !g.is_observer })
    toast.success(t(g.is_observer ? 'adminPanel.groupObserverRevoked' : 'adminPanel.groupObserverGranted', { name: g.name }))
    await loadAll()
  } catch (e) {
    toast.error(errMessage(e))
  }
}

async function deleteGroup(g: GroupOut) {
  if (!confirm(t('adminPanel.confirmDeleteGroup', { name: g.name }))) return
  try {
    await api.deleteGroup(g.id)
    if (gruppoSelezionato.value === g.id) gruppoSelezionato.value = null
    toast.success(t('adminPanel.groupDeleted', { name: g.name }))
    await loadAll()
  } catch (e) {
    toast.error(errMessage(e))
  }
}

async function addMember() {
  if (!daAggiungere.value || !gruppoCorrente.value) return
  try {
    await api.addToGroup(daAggiungere.value, gruppoCorrente.value.id)
    toast.success(t('adminPanel.userAddedToGroup'))
    daAggiungere.value = null
    await loadAll()
  } catch (e) {
    toast.error(errMessage(e))
  }
}

async function removeMember(u: UserOut) {
  if (!gruppoCorrente.value) return
  try {
    await api.removeFromGroup(u.id, gruppoCorrente.value.id)
    toast.success(t('adminPanel.userRemovedFromGroup'))
    await loadAll()
  } catch (e) {
    toast.error(errMessage(e))
  }
}

// ── informativa sulla privacy ───────────────────────────────────────────────
// Una sola, si modifica in posto. Il testo di partenza descrive quello che il
// prodotto raccoglie davvero: chi installa resta libero di riscriverlo — è lui
// il titolare — ma parte da qualcosa di vero.
async function salvaPrivacy() {
  if (!privacy.value) return
  salvandoPrivacy.value = true
  try {
    privacy.value = await privacyApi.update({
      enabled: privacy.value.enabled,
      summary: privacy.value.summary_template,
      body: privacy.value.body_template,
      url: privacy.value.url,
    })
    toast.success(t('adminPanel.privacySaved'))
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    salvandoPrivacy.value = false
  }
}

// ── banner ──────────────────────────────────────────────────────────────────
async function createBanner() {
  if (!nb.value.message.trim()) {
    toast.error(t('adminPanel.bannerMessageRequired'))
    return
  }
  try {
    await bannersApi.create({ message: nb.value.message.trim(), level: nb.value.level })
    toast.success(t('adminPanel.bannerCreated'))
    nb.value = { message: '', level: 'warning' }
    await loadAll()
  } catch (e) {
    toast.error(errMessage(e))
  }
}

async function deleteBanner(b: Banner) {
  if (!confirm(t('adminPanel.confirmDeleteBanner'))) return
  try {
    await bannersApi.remove(b.id)
    banners.value = banners.value.filter((x) => x.id !== b.id)
    toast.success(t('adminPanel.bannerDeleted'))
  } catch (e) {
    toast.error(errMessage(e))
  }
}

function etichettaLivello(l: string): string {
  return t(`banners.level${l.charAt(0).toUpperCase()}${l.slice(1)}`)
}

// ── modelli AI ──────────────────────────────────────────────────────────────
async function toggleModello(m: AiModel) {
  try {
    const aggiornato = await aiApi.setModel(m.model_id, !m.enabled)
    modelli.value = (modelli.value ?? []).map((x) => (x.model_id === aggiornato.model_id ? aggiornato : x))
    toast.success(t(aggiornato.enabled ? 'adminPanel.aiModelEnabled' : 'adminPanel.aiModelDisabled', { model: m.model_id }))
  } catch (e) {
    toast.error(errMessage(e))
  }
}

// ── motori ──────────────────────────────────────────────────────────────────
async function toggleMotore(m: EnginePolicy) {
  try {
    const aggiornato = await engineApi.set(m.engine_id, !m.allowed)
    motori.value = motori.value.map((x) => (x.engine_id === aggiornato.engine_id ? aggiornato : x))
    toast.success(
      t(aggiornato.allowed ? 'adminPanel.engineAllowed' : 'adminPanel.engineDisallowed', { engine: m.engine_id }),
    )
  } catch (e) {
    toast.error(errMessage(e))
  }
}
</script>

<template>
  <div class="admin">
    <div class="page-head">
      <h1><Shield :size="18" /> {{ $t('adminPanel.title') }}</h1>
    </div>

    <div class="layout">
      <!-- navigazione di sezione: solo per l'amministrazione -->
      <nav class="sidenav">
        <button
          v-for="s in sezioni"
          :key="s.id"
          class="navitem"
          :class="{ on: sezione === s.id, spenta: 'off' in s && s.off }"
          :aria-disabled="'off' in s && s.off ? 'true' : undefined"
          :title="'off' in s && s.off ? $t(s.id === 'backups' ? 'backups.kubernetesOnly' : 'scripting.kubernetesOnly') : undefined"
          @click="!('off' in s && s.off) && vaiA(s.id)"
        >
          <component :is="s.icon" :size="15" />
          <span class="lbl">{{ s.label }}</span>
          <span class="count">{{ s.badge }}</span>
        </button>
      </nav>

      <section class="content">
        <!-- ── UTENTI ─────────────────────────────────────────────────────── -->
        <template v-if="sezione === 'users'">
          <div class="card">
            <h4><UserIcon :size="14" /> {{ $t('adminPanel.usersTitle') }} <span class="muted">{{ persone.length }}</span></h4>

            <div class="rowsearch">
              <Search :size="13" />
              <input v-model="qUsers" type="text" :placeholder="$t('adminPanel.searchPlaceholder')" :aria-label="$t('adminPanel.searchPlaceholder')" />
              <span class="muted count">{{ utentiFiltrati.length }}/{{ persone.length }}</span>
              <button v-if="qUsers" class="x" :title="$t('adminPanel.clearSearch')" @click="qUsers = ''">
                <X :size="12" />
              </button>
            </div>

            <!-- la tabella cresce: resta scorrevole, con l'intestazione ferma in cima -->
            <div class="tablewrap">
              <table class="rows">
                <thead>
                  <tr>
                    <th>{{ $t('adminPanel.colUser') }}</th>
                    <th>{{ $t('adminPanel.colGroups') }}</th>
                    <th>{{ $t('adminPanel.colCreated') }}</th>
                    <th>{{ $t('adminPanel.colLastSeen') }}</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="u in utentiFiltrati" :key="u.id">
                    <td>
                      {{ u.email }}
                      <span v-if="u.is_superuser" class="tag">{{ $t('adminPanel.adminTag') }}</span>
                      <span
                        v-else-if="u.admin_groups.length"
                        class="tag via"
                        :title="$t('adminPanel.adminViaGroupTitle', { groups: u.admin_groups.join(', ') })"
                      >{{ $t('adminPanel.adminViaGroupTag', { group: u.admin_groups[0] }) }}</span>
                      <span
                        v-if="!u.is_superuser && !u.admin_groups.length && (u.is_observer || u.observer_groups.length)"
                        class="tag obs"
                        :class="{ via: !u.is_observer }"
                        :title="u.observer_groups.length
                          ? $t('adminPanel.observerViaGroupTitle', { groups: u.observer_groups.join(', ') })
                          : $t('adminPanel.observerTagTitle')"
                      >{{ $t('adminPanel.observerTag') }}</span>
                      <span v-if="!u.is_active" class="tag off">{{ $t('adminPanel.disabledTag') }}</span>
                      <span v-if="u.sso_only" class="tag sso">{{ $t('adminPanel.ssoTag') }}</span>
                      <div v-if="u.full_name" class="muted small">{{ u.full_name }}</div>
                    </td>
                    <td>
                      <span v-for="g in u.groups" :key="g" class="chip" :class="{ adm: u.admin_groups.includes(g) }">{{ g }}</span>
                      <span v-if="!u.groups.length" class="muted small">—</span>
                    </td>
                    <td class="muted small nowrap">{{ quando(u.created_at) }}</td>
                    <td class="muted small nowrap">
                      {{ u.last_seen_at ? quando(u.last_seen_at) : $t('adminPanel.neverSeen') }}
                    </td>
                    <td class="right nowrap">
                      <button
                        class="mini"
                        v-if="canWrite"
                        :class="{ on: u.is_observer }"
                        :aria-pressed="u.is_observer"
                        :title="u.is_superuser || u.admin_groups.length
                          ? $t('adminPanel.observerImpliedTitle')
                          : u.is_observer ? $t('adminPanel.revokeObserverTitle') : $t('adminPanel.grantObserverTitle')"
                        @click="toggleObserver(u)"
                      ><Eye :size="13" /></button>
                      <button
                        class="mini"
                        v-if="canWrite"
                        :class="{ on: u.is_superuser }"
                        :aria-pressed="u.is_superuser"
                        :disabled="selfLockout(u)"
                        :title="selfLockout(u) ? $t('adminPanel.cannotDemoteSelf') : u.is_superuser ? $t('adminPanel.demoteUserTitle') : $t('adminPanel.promoteUserTitle')"
                        @click="toggleAdmin(u)"
                      ><ShieldCheck :size="13" /></button>
                      <button
                        class="mini"
                        v-if="canWrite"
                        :disabled="u.id === me?.id"
                        :title="u.is_active ? $t('adminPanel.disableUserTitle') : $t('adminPanel.enableUserTitle')"
                        @click="toggleActive(u)"
                      >
                        <component :is="u.is_active ? XCircle : CheckCircle2" :size="13" />
                      </button>
                      <button
                        class="mini"
                        v-if="canWrite"
                        :disabled="u.id === me?.id || u.sso_only"
                        :title="u.id === me?.id ? $t('password.ownFromMenu') : u.sso_only ? $t('password.ssoFor') : $t('password.setTitle')"
                        :aria-label="$t('password.setTitle')"
                        @click="passwordFor = u"
                      ><KeyRound :size="13" /></button>
                      <button
                        class="mini danger"
                        v-if="canWrite"
                        :disabled="u.id === me?.id"
                        :title="u.id === me?.id ? $t('adminPanel.cannotDeleteSelf') : $t('adminPanel.deleteUserTitle')"
                        @click="deleteUser(u)"
                      ><Trash2 :size="13" /></button>
                    </td>
                  </tr>
                  <tr v-if="!utentiFiltrati.length">
                    <td colspan="5" class="muted">
                      {{ qUsers ? $t('adminPanel.noSearchResults') : $t('adminPanel.noUsers') }}
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>

          <div v-if="canWrite" class="card form">
            <h4><Plus :size="13" /> {{ $t('adminPanel.newUserTitle') }}</h4>
            <input v-model="nu.email" type="email" :placeholder="$t('adminPanel.emailPlaceholder')" :aria-label="$t('adminPanel.emailPlaceholder')" />
            <input v-model="nu.password" type="password" :placeholder="$t('adminPanel.passwordPlaceholder', { n: passwordRules.min_length })" :aria-label="$t('adminPanel.passwordPlaceholder', { n: passwordRules.min_length })" autocomplete="new-password" />
            <input v-model="nu.full_name" type="text" :placeholder="$t('adminPanel.fullNamePlaceholder')" :aria-label="$t('adminPanel.fullNamePlaceholder')" />
            <label class="chk"><input v-model="nu.is_superuser" type="checkbox" /> {{ $t('adminPanel.superuserLabel') }}</label>
            <label class="chk"><input v-model="nu.is_observer" type="checkbox" /> {{ $t('adminPanel.observerLabel') }}</label>
            <button class="primary" @click="createUser">{{ $t('adminPanel.createUserButton') }}</button>
          </div>
        </template>

        <!-- ── GRUPPI ─────────────────────────────────────────────────────── -->
        <template v-else-if="sezione === 'groups'">
          <div class="card">
            <h4><UsersIcon :size="14" /> {{ $t('adminPanel.groupsTitle') }} <span class="muted">{{ groups.length }}</span></h4>

            <div class="rowsearch">
              <Search :size="13" />
              <input v-model="qGroups" type="text" :placeholder="$t('adminPanel.searchPlaceholder')" :aria-label="$t('adminPanel.searchPlaceholder')" />
              <span class="muted count">{{ gruppiFiltrati.length }}/{{ groups.length }}</span>
              <button v-if="qGroups" class="x" :title="$t('adminPanel.clearSearch')" @click="qGroups = ''">
                <X :size="12" />
              </button>
            </div>

            <div class="tablewrap">
              <table class="rows">
                <thead>
                  <tr>
                    <th>{{ $t('adminPanel.colName') }}</th>
                    <th>{{ $t('adminPanel.colMembers') }}</th>
                    <th>{{ $t('adminPanel.colCreated') }}</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="g in gruppiFiltrati"
                    :key="g.id"
                    class="clickable"
                    :class="{ sel: g.id === gruppoSelezionato }"
                    @click="gruppoSelezionato = g.id"
                  >
                    <td>
                      <!-- la riga si sceglie col mouse; questo bottone è la via da tastiera -->
                      <button type="button" class="rowpick" :aria-pressed="g.id === gruppoSelezionato" @click.stop="gruppoSelezionato = g.id">{{ g.name }}</button>
                      <span v-if="g.is_admin" class="tag">{{ $t('adminPanel.adminTag') }}</span>
                      <span v-else-if="g.is_observer" class="tag obs">{{ $t('adminPanel.observerTag') }}</span>
                      <div v-if="g.description" class="muted small">{{ g.description }}</div>
                    </td>
                    <td class="muted small">{{ g.member_count }}</td>
                    <td class="muted small nowrap">{{ quando(g.created_at) }}</td>
                    <td class="right nowrap">
                      <button
                        class="mini"
                        v-if="canWrite"
                        :class="{ on: g.is_observer }"
                        :aria-pressed="g.is_observer"
                        :title="g.is_admin
                          ? $t('adminPanel.observerImpliedTitle')
                          : g.is_observer ? $t('adminPanel.revokeGroupObserverTitle') : $t('adminPanel.grantGroupObserverTitle')"
                        @click.stop="toggleGroupObserver(g)"
                      ><Eye :size="13" /></button>
                      <button
                        class="mini"
                        v-if="canWrite"
                        :class="{ on: g.is_admin }"
                        :aria-pressed="g.is_admin"
                        :title="g.is_admin ? $t('adminPanel.demoteGroupTitle') : $t('adminPanel.promoteGroupTitle')"
                        @click.stop="toggleGroupAdmin(g)"
                      ><ShieldCheck :size="13" /></button>
                      <button class="mini danger" v-if="canWrite" :title="$t('adminPanel.deleteGroupTitle')" @click.stop="deleteGroup(g)">
                        <Trash2 :size="13" />
                      </button>
                    </td>
                  </tr>
                  <tr v-if="!gruppiFiltrati.length">
                    <td colspan="4" class="muted">
                      {{ qGroups ? $t('adminPanel.noSearchResults') : $t('adminPanel.noGroups') }}
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>

          <!-- membri del gruppo scelto: aggiunta e rimozione stanno qui, dove
               si vede subito chi c'è dentro -->
          <div v-if="gruppoCorrente" class="card">
            <h4><UsersIcon :size="14" /> {{ $t('adminPanel.membersTitle', { name: gruppoCorrente.name }) }}</h4>
            <div class="tablewrap">
              <table class="rows">
                <tbody>
                  <tr v-for="u in membri" :key="u.id">
                    <td>{{ u.email }}</td>
                    <td class="right">
                      <button class="mini" v-if="canWrite" :title="$t('adminPanel.removeFromGroupTitle')" @click="removeMember(u)">
                        <X :size="13" />
                      </button>
                    </td>
                  </tr>
                  <tr v-if="!membri.length"><td class="muted">{{ $t('adminPanel.noMembers') }}</td></tr>
                </tbody>
              </table>
            </div>

            <template v-if="canWrite">
            <h4 class="subhead"><Plus :size="13" /> {{ $t('adminPanel.addToGroupTitle') }}</h4>
            <Select
              v-model="daAggiungere"
              :options="nonMembri.map((u) => ({ value: u.id, label: u.email }))"
              :placeholder="$t('adminPanel.userPlaceholder')"
              :aria-label="$t('adminPanel.userPlaceholder')"
            />
            <button :disabled="!daAggiungere" @click="addMember">{{ $t('adminPanel.addButton') }}</button>
            </template>
          </div>

          <div v-if="canWrite" class="card form">
            <h4><Plus :size="13" /> {{ $t('adminPanel.newGroupTitle') }}</h4>
            <input v-model="ng.name" type="text" :placeholder="$t('adminPanel.groupNamePlaceholder')" :aria-label="$t('adminPanel.groupNamePlaceholder')" />
            <input v-model="ng.description" type="text" :placeholder="$t('adminPanel.descriptionPlaceholder')" :aria-label="$t('adminPanel.descriptionPlaceholder')" />
            <button class="primary" @click="createGroup">{{ $t('adminPanel.createGroupButton') }}</button>
          </div>
        </template>

        <!-- ── MOTORI ─────────────────────────────────────────────────────── -->
        <template v-else-if="sezione === 'privacy'">
          <div class="card">
            <h4><ShieldCheck :size="14" /> {{ $t('adminPanel.privacyTitle') }}</h4>
            <p class="muted small hint">{{ $t('adminPanel.privacyHint') }}</p>
            <template v-if="privacy">
              <label class="chk">
                <input v-model="privacy.enabled" type="checkbox" :disabled="!canWrite" />
                {{ $t('adminPanel.privacyEnabled') }}
              </label>
              <label class="dlabel" for="pv-sum">{{ $t('adminPanel.privacySummary') }}</label>
              <input id="pv-sum" v-model="privacy.summary_template" type="text" :disabled="!canWrite" />
              <label class="dlabel" for="pv-body">{{ $t('adminPanel.privacyBody') }}</label>
              <p class="muted small hint">{{ $t('adminPanel.privacyPlaceholders') }}</p>
              <textarea id="pv-body" v-model="privacy.body_template" rows="16" :disabled="!canWrite" />
              <label class="dlabel" for="pv-url">{{ $t('adminPanel.privacyUrl') }}</label>
              <input id="pv-url" v-model="privacy.url" type="url" :disabled="!canWrite"
                     placeholder="https://" />
              <button v-if="canWrite" class="primary" :disabled="salvandoPrivacy" @click="salvaPrivacy">
                {{ $t('adminPanel.privacySave') }}
              </button>
            </template>
          </div>
        </template>

        <template v-else-if="sezione === 'engines'">
          <div class="card">
            <h4><Cpu :size="14" /> {{ $t('adminPanel.enginesTitle') }}</h4>
            <p class="muted small hint">{{ $t('adminPanel.enginesHint') }}</p>
            <p class="muted small hint">
              {{ daMigrare ? $t('adminPanel.enginesToMigrate', { count: daMigrare }) : $t('adminPanel.enginesAllCompliant') }}
            </p>
            <div class="tablewrap">
              <table class="rows">
                <thead>
                  <tr>
                    <th>{{ $t('adminPanel.colEngine') }}</th>
                    <th>{{ $t('adminPanel.colFlowsUsing') }}</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="m in motori" :key="m.engine_id">
                    <td>
                      {{ m.engine_id }}
                      <span v-if="!m.allowed" class="tag off">{{ $t('adminPanel.notAllowedTag') }}</span>
                    </td>
                    <!-- evidenziato solo quando il motore NON è consentito: lì
                         il numero è lavoro da fare, altrove è solo un fatto -->
                    <td class="small" :class="!m.allowed && m.flows_using ? 'warn' : 'muted'">{{ m.flows_using }}</td>
                    <td class="right">
                      <button
                        class="mini"
                        v-if="canWrite"
                        :title="m.allowed ? $t('adminPanel.disallowEngineTitle') : $t('adminPanel.allowEngineTitle')"
                        :aria-label="m.allowed ? $t('adminPanel.disallowEngineTitle') : $t('adminPanel.allowEngineTitle')"
                        @click="toggleMotore(m)"
                      >
                        <component :is="m.allowed ? CheckCircle2 : XCircle" :size="13" />
                      </button>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </template>
        <template v-else-if="sezione === 'siem'">
          <SiemSection @state="siemAcceso = $event" />
        </template>

        <template v-else-if="sezione === 'backups'">
          <BackupsSection @count="nBackup = $event" />
        </template>
        <template v-else-if="sezione === 'settings'">
          <SettingsSection @count="nImpostazioni = $event" @dirty="impostazioniDaSalvare = $event" />
        </template>

        <template v-else-if="sezione === 'workers'">
          <WorkersSection @count="nWorker = $event" />
        </template>

        <template v-else-if="sezione === 'scripting'">
          <ScriptingSection :groups="groups" @count="nAmbienti = $event" />
        </template>
        <template v-else-if="sezione === 'registries'">
          <RegistriesSection :groups="groups" :users="users" @count="nRegistri = $event" />
        </template>

        <template v-else-if="sezione === 'service'">
          <ServiceAccountsSection :can-write="canWrite" :groups="groups" @count="nServizio = $event" />
        </template>

        <template v-else-if="sezione === 'webhooks'">
          <WebhooksSection :can-write="canWrite" :projects="cartelle" @count="nWebhook = $event" />
        </template>

        <template v-else-if="sezione === 'ai'">
          <div class="card">
            <h4><Sparkles :size="14" /> {{ $t('adminPanel.aiTitle') }}</h4>
            <p class="muted small hint">{{ $t('adminPanel.aiHint') }}</p>
            <p v-if="modelli === null" class="muted small hint">{{ modelliErrore || $t('adminPanel.aiUnavailable') }}</p>
            <div v-else class="tablewrap">
              <table class="rows">
                <thead>
                  <tr>
                    <th>{{ $t('adminPanel.colModel') }}</th>
                    <th>{{ $t('adminPanel.colModelState') }}</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="m in modelli" :key="m.model_id">
                    <td>
                      {{ m.model_id }}
                      <span v-if="!m.chat" class="tag off">{{ $t('adminPanel.aiNotChatTag') }}</span>
                      <span v-else-if="!m.available" class="tag off">{{ $t('adminPanel.aiGoneTag') }}</span>
                    </td>
                    <td class="small" :class="m.enabled ? '' : 'muted'">{{ m.enabled ? $t('adminPanel.aiEnabled') : $t('adminPanel.aiDisabled') }}</td>
                    <td class="right">
                      <button
                        v-if="m.chat && canWrite"
                        class="mini"
                        :title="m.enabled ? $t('adminPanel.aiDisableTitle') : $t('adminPanel.aiEnableTitle')"
                        :aria-label="m.enabled ? $t('adminPanel.aiDisableTitle') : $t('adminPanel.aiEnableTitle')"
                        @click="toggleModello(m)"
                      >
                        <component :is="m.enabled ? CheckCircle2 : XCircle" :size="13" />
                      </button>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </template>

        <!-- ── BANNER ─────────────────────────────────────────────────────── -->
        <template v-else>
          <div class="card">
            <h4><TriangleAlert :size="14" /> {{ $t('adminPanel.bannersTitle') }} <span class="muted">{{ banners.length }}</span></h4>
            <p class="muted small hint">{{ $t('adminPanel.bannersHint') }}</p>
            <div class="tablewrap">
              <table class="rows">
                <thead>
                  <tr>
                    <th>{{ $t('adminPanel.colLevel') }}</th>
                    <th>{{ $t('adminPanel.colMessage') }}</th>
                    <th>{{ $t('adminPanel.colCreated') }}</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="b in banners" :key="b.id">
                    <td><span class="tag" :class="b.level">{{ etichettaLivello(b.level) }}</span></td>
                    <td>{{ b.message }}</td>
                    <td class="muted small nowrap">{{ quando(b.created_at) }}</td>
                    <td class="right">
                      <button class="mini danger" v-if="canWrite" :title="$t('adminPanel.deleteBannerTitle')" @click="deleteBanner(b)">
                        <Trash2 :size="13" />
                      </button>
                    </td>
                  </tr>
                  <tr v-if="!banners.length"><td colspan="4" class="muted">{{ $t('adminPanel.noBanners') }}</td></tr>
                </tbody>
              </table>
            </div>
          </div>

          <div v-if="canWrite" class="card form">
            <h4><Plus :size="13" /> {{ $t('adminPanel.newBannerTitle') }}</h4>
            <input v-model="nb.message" type="text" :placeholder="$t('adminPanel.bannerMessagePlaceholder')" @keyup.enter="createBanner" />
            <Select
              v-model="nb.level"
              :options="[
                { value: 'info', label: $t('banners.levelInfo') },
                { value: 'warning', label: $t('banners.levelWarning') },
                { value: 'danger', label: $t('banners.levelDanger') },
              ]"
            />
            <button class="primary" @click="createBanner">{{ $t('adminPanel.createBannerButton') }}</button>
          </div>
        </template>
      </section>
    </div>
    <PasswordDialog :open="!!passwordFor" :target="passwordFor" @close="passwordFor = null" />
  </div>
</template>

<style scoped>
.navitem.spenta { opacity: .45; cursor: not-allowed; }
.rowpick { padding: 0; border: 0; background: none; font: inherit; color: inherit; text-align: left; cursor: pointer; }
.rowpick:hover { color: var(--accent); box-shadow: none; }
.page-head { margin-bottom: 16px; }
.page-head h1 {
  margin: 0;
  font-size: 19px;
  font-weight: 700;
  display: inline-flex;
  align-items: center;
  gap: 9px;
}
.layout { display: grid; grid-template-columns: 210px 1fr; gap: 16px; align-items: start; }
.sidenav {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  position: sticky;
  top: 12px;
}
.navitem {
  display: flex;
  align-items: center;
  gap: 9px;
  width: 100%;
  padding: 8px 10px;
  border: none;
  background: none;
  color: var(--muted);
  font-size: 13px;
  text-align: left;
  border-radius: 7px;
  cursor: pointer;
}
.navitem:hover { background: var(--panel-2); }
.navitem.on { background: var(--panel-2); color: var(--accent-hi); font-weight: 600; }
.navitem .lbl { flex: 1; }
.navitem .count { font-size: 11px; color: var(--muted); }
.content { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
.card {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 16px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
}
.card.form { max-width: 420px; }
.card h4 { margin: 0; display: inline-flex; align-items: center; gap: 7px; }
.subhead { margin-top: 14px !important; color: var(--muted); font-size: 12.5px; }
.hint { margin: 0; }
/* stessa barra di ricerca del group by */
.rowsearch {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 8px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--panel-2);
  color: var(--muted);
}
.rowsearch input {
  flex: 1;
  border: none;
  background: transparent;
  outline: none;
  color: var(--text);
  font-size: 12.5px;
  min-width: 0;
}
.rowsearch .count { font-size: 11px; font-variant-numeric: tabular-nums; }
.rowsearch .x { padding: 1px 6px; }
/* la tabella scorre invece di allungare la pagina all'infinito */
.tablewrap { max-height: 420px; overflow-y: auto; }
table.rows { width: 100%; border-collapse: collapse; font-size: 13px; }
table.rows th {
  text-align: left;
  padding: 4px;
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--muted);
  border-bottom: 1px solid var(--border);
  position: sticky;
  top: 0;
  background: var(--panel);
  z-index: 1;
}
table.rows td { padding: 6px 4px; border-bottom: 1px solid var(--border-soft); vertical-align: top; }
table.rows tr:last-child td { border-bottom: none; }
tr.clickable { cursor: pointer; }
tr.clickable:hover td { background: var(--panel-2); }
tr.sel td { background: var(--panel-2); }
td.right { text-align: right; width: 80px; }
.nowrap { white-space: nowrap; }
.small { font-size: 11.5px; }
.chip {
  display: inline-block;
  font-size: 11px;
  padding: 1px 7px;
  margin: 0 4px 2px 0;
  border-radius: 8px;
  background: var(--panel-2);
  border: 1px solid var(--border);
}
.tag {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding: 1px 7px;
  border-radius: 8px;
  background: var(--panel-2);
  border: 1px solid var(--border);
  color: var(--accent-hi);
  margin-left: 6px;
}
.tag.off { color: var(--muted); }
/* admin per appartenenza a un gruppo: stesso colore, ma tratteggiato — il
   privilegio non è suo, è del gruppo */
.tag.via { border-style: dashed; text-transform: none; letter-spacing: 0; }
.chip.adm { border-color: var(--accent-hi); color: var(--accent-hi); }
td.warn { color: var(--warning, #d08700); font-weight: 600; }
/* osservatore: grigio, non nel colore d'accento — è un permesso di sola
   lettura, non un privilegio da evidenziare come l'amministrazione */
.tag.obs { color: var(--muted); }
.tag.sso { color: var(--muted); }
.tag.info { color: var(--accent-hi, #4c8dff); }
.tag.warning { color: var(--warning, #d08700); }
.tag.danger { color: var(--danger, #e5484d); }
.chk { display: flex; align-items: center; gap: 6px; }
.chk input { width: auto; }
button.mini { padding: 3px 8px; min-height: 24px; }
button.mini + button.mini { margin-left: 4px; }
.mini.on { border-color: var(--accent-hi); color: var(--accent-hi); background: var(--panel-2); }
.mini.danger { border-color: var(--danger); color: var(--danger); }
.mini.danger:hover:not(:disabled) { background: var(--danger-fill); color: var(--on-danger); }
.mini:disabled { opacity: 0.4; cursor: not-allowed; }
@media (max-width: 900px) {
  .layout { grid-template-columns: 1fr; }
  .sidenav { position: static; flex-direction: row; overflow-x: auto; }
}
</style>

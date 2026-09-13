<script setup lang="ts">
// Amministrazione (solo superuser), divisa in SEZIONI con navigazione a sinistra:
// utenti, gruppi, banner. Prima era tutto impilato in un'unica schermata e le
// azioni distruttive stavano accanto ai form di creazione. Il feedback passa dai
// toast; le conferme dai confirm() del browser, come nel resto dell'app.
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  CheckCircle2, Plus, Search, Shield, Trash2, TriangleAlert,
  User as UserIcon, Users as UsersIcon, X, XCircle,
} from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useProjects, type GroupOut, type UserOut } from '~/composables/useProjects'
import { useBanners, type Banner, type BannerLevel } from '~/composables/useBanners'

const api = useProjects()
const bannersApi = useBanners()
const toast = useToast()
const { user: me } = useAuth()
const { t } = useI18n()

type Sezione = 'users' | 'groups' | 'banners'
const sezione = ref<Sezione>('users')

const users = ref<UserOut[]>([])
const groups = ref<GroupOut[]>([])
const banners = ref<Banner[]>([])

const nu = ref({ email: '', password: '', full_name: '', is_superuser: false })
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

const utentiFiltrati = computed(() =>
  users.value.filter((u) => combacia(qUsers.value, u.email, u.full_name, u.groups.join(' '))),
)
const gruppiFiltrati = computed(() =>
  groups.value.filter((g) => combacia(qGroups.value, g.name, g.description)),
)

const sezioni = computed(() => [
  { id: 'users' as Sezione, label: t('adminPanel.usersTitle'), icon: UserIcon, badge: users.value.length },
  { id: 'groups' as Sezione, label: t('adminPanel.groupsTitle'), icon: UsersIcon, badge: groups.value.length },
  { id: 'banners' as Sezione, label: t('adminPanel.bannersTitle'), icon: TriangleAlert, badge: banners.value.length },
])

// stessa convenzione delle altre pagine: formattatore locale, tollerante
function quando(iso: string | null): string {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString('it-IT', { dateStyle: 'short', timeStyle: 'short' })
  } catch {
    return iso
  }
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
  } catch (e) {
    toast.error(errMessage(e))
  }
}
onMounted(loadAll)

// ── utenti ──────────────────────────────────────────────────────────────────
async function createUser() {
  if (!nu.value.email || nu.value.password.length < 6) {
    toast.error(t('adminPanel.emailPasswordRequired'))
    return
  }
  try {
    await api.createUser({ ...nu.value })
    toast.success(t('adminPanel.userCreated', { email: nu.value.email }))
    nu.value = { email: '', password: '', full_name: '', is_superuser: false }
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
          :class="{ on: sezione === s.id }"
          @click="sezione = s.id"
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
            <h4><UserIcon :size="14" /> {{ $t('adminPanel.usersTitle') }} <span class="muted">{{ users.length }}</span></h4>

            <div class="rowsearch">
              <Search :size="13" />
              <input v-model="qUsers" type="text" :placeholder="$t('adminPanel.searchPlaceholder')" />
              <span class="muted count">{{ utentiFiltrati.length }}/{{ users.length }}</span>
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
                      <span v-if="!u.is_active" class="tag off">{{ $t('adminPanel.disabledTag') }}</span>
                      <span v-if="u.sso_only" class="tag sso">{{ $t('adminPanel.ssoTag') }}</span>
                      <div v-if="u.full_name" class="muted small">{{ u.full_name }}</div>
                    </td>
                    <td>
                      <span v-for="g in u.groups" :key="g" class="chip">{{ g }}</span>
                      <span v-if="!u.groups.length" class="muted small">—</span>
                    </td>
                    <td class="muted small nowrap">{{ quando(u.created_at) }}</td>
                    <td class="muted small nowrap">
                      {{ u.last_seen_at ? quando(u.last_seen_at) : $t('adminPanel.neverSeen') }}
                    </td>
                    <td class="right nowrap">
                      <button
                        class="mini"
                        :disabled="u.id === me?.id"
                        :title="u.is_active ? $t('adminPanel.disableUserTitle') : $t('adminPanel.enableUserTitle')"
                        @click="toggleActive(u)"
                      >
                        <component :is="u.is_active ? XCircle : CheckCircle2" :size="13" />
                      </button>
                      <button
                        class="mini danger"
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

          <div class="card form">
            <h4><Plus :size="13" /> {{ $t('adminPanel.newUserTitle') }}</h4>
            <input v-model="nu.email" type="email" :placeholder="$t('adminPanel.emailPlaceholder')" />
            <input v-model="nu.password" type="password" :placeholder="$t('adminPanel.passwordPlaceholder')" autocomplete="new-password" />
            <input v-model="nu.full_name" type="text" :placeholder="$t('adminPanel.fullNamePlaceholder')" />
            <label class="chk"><input v-model="nu.is_superuser" type="checkbox" /> {{ $t('adminPanel.superuserLabel') }}</label>
            <button class="primary" @click="createUser">{{ $t('adminPanel.createUserButton') }}</button>
          </div>
        </template>

        <!-- ── GRUPPI ─────────────────────────────────────────────────────── -->
        <template v-else-if="sezione === 'groups'">
          <div class="card">
            <h4><UsersIcon :size="14" /> {{ $t('adminPanel.groupsTitle') }} <span class="muted">{{ groups.length }}</span></h4>

            <div class="rowsearch">
              <Search :size="13" />
              <input v-model="qGroups" type="text" :placeholder="$t('adminPanel.searchPlaceholder')" />
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
                      {{ g.name }}
                      <div v-if="g.description" class="muted small">{{ g.description }}</div>
                    </td>
                    <td class="muted small">{{ g.member_count }}</td>
                    <td class="muted small nowrap">{{ quando(g.created_at) }}</td>
                    <td class="right">
                      <button class="mini danger" :title="$t('adminPanel.deleteGroupTitle')" @click.stop="deleteGroup(g)">
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
                      <button class="mini" :title="$t('adminPanel.removeFromGroupTitle')" @click="removeMember(u)">
                        <X :size="13" />
                      </button>
                    </td>
                  </tr>
                  <tr v-if="!membri.length"><td class="muted">{{ $t('adminPanel.noMembers') }}</td></tr>
                </tbody>
              </table>
            </div>

            <h4 class="subhead"><Plus :size="13" /> {{ $t('adminPanel.addToGroupTitle') }}</h4>
            <Select
              v-model="daAggiungere"
              :options="nonMembri.map((u) => ({ value: u.id, label: u.email }))"
              :placeholder="$t('adminPanel.userPlaceholder')"
            />
            <button :disabled="!daAggiungere" @click="addMember">{{ $t('adminPanel.addButton') }}</button>
          </div>

          <div class="card form">
            <h4><Plus :size="13" /> {{ $t('adminPanel.newGroupTitle') }}</h4>
            <input v-model="ng.name" type="text" :placeholder="$t('adminPanel.groupNamePlaceholder')" />
            <input v-model="ng.description" type="text" :placeholder="$t('adminPanel.descriptionPlaceholder')" />
            <button class="primary" @click="createGroup">{{ $t('adminPanel.createGroupButton') }}</button>
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
                      <button class="mini danger" :title="$t('adminPanel.deleteBannerTitle')" @click="deleteBanner(b)">
                        <Trash2 :size="13" />
                      </button>
                    </td>
                  </tr>
                  <tr v-if="!banners.length"><td colspan="4" class="muted">{{ $t('adminPanel.noBanners') }}</td></tr>
                </tbody>
              </table>
            </div>
          </div>

          <div class="card form">
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
  </div>
</template>

<style scoped>
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
.tag.sso { color: var(--muted); }
.tag.info { color: var(--accent-hi, #4c8dff); }
.tag.warning { color: var(--warning, #d08700); }
.tag.danger { color: var(--danger, #e5484d); }
.chk { display: flex; align-items: center; gap: 6px; }
.chk input { width: auto; }
button.mini { padding: 3px 8px; min-height: 24px; }
button.mini + button.mini { margin-left: 4px; }
.mini.danger { border-color: var(--danger); color: var(--danger); }
.mini.danger:hover:not(:disabled) { background: var(--danger); color: #fff; }
.mini:disabled { opacity: 0.4; cursor: not-allowed; }
@media (max-width: 900px) {
  .layout { grid-template-columns: 1fr; }
  .sidenav { position: static; flex-direction: row; overflow-x: auto; }
}
</style>

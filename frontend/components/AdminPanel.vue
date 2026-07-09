<script setup lang="ts">
// Amministrazione (solo superuser): utenti (lista + crea + elimina), gruppi e
// appartenenze. Vive nella pagina /admin; il feedback passa dai toast.
import { ref, onMounted } from 'vue'
import { Shield, Trash2, User as UserIcon, Users as UsersIcon, Plus } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useProjects, type UserOut, type GroupOut } from '~/composables/useProjects'

const api = useProjects()
const toast = useToast()
const { user: me } = useAuth()

const users = ref<UserOut[]>([])
const groups = ref<GroupOut[]>([])

const nu = ref({ email: '', password: '', full_name: '', is_superuser: false })
const ng = ref({ name: '', description: '' })
const member = ref<{ user_id: number | null; group_id: number | null }>({ user_id: null, group_id: null })

async function loadAll() {
  try {
    users.value = await api.users()
    groups.value = await api.groups()
  } catch (e) {
    toast.error(errMessage(e))
  }
}
onMounted(loadAll)

async function createUser() {
  if (!nu.value.email || nu.value.password.length < 6) {
    toast.error('Email and a password of at least 6 characters are required')
    return
  }
  try {
    await api.createUser({ ...nu.value })
    toast.success(`User ${nu.value.email} created`)
    nu.value = { email: '', password: '', full_name: '', is_superuser: false }
    await loadAll()
  } catch (e) {
    toast.error(errMessage(e))
  }
}

async function deleteUser(u: UserOut) {
  if (!confirm(`Delete user "${u.email}"? Their content stays, ownership references are cleared.`)) return
  try {
    await api.deleteUser(u.id)
    users.value = users.value.filter((x) => x.id !== u.id)
    toast.success(`User ${u.email} deleted`)
  } catch (e) {
    toast.error(errMessage(e))
  }
}

async function createGroup() {
  if (!ng.value.name) return
  try {
    await api.createGroup({ ...ng.value })
    toast.success(`Group ${ng.value.name} created`)
    ng.value = { name: '', description: '' }
    await loadAll()
  } catch (e) {
    toast.error(errMessage(e))
  }
}

async function addMember() {
  if (!member.value.user_id || !member.value.group_id) return
  try {
    await api.addToGroup(member.value.user_id, member.value.group_id)
    toast.success('User added to the group')
  } catch (e) {
    toast.error(errMessage(e))
  }
}
</script>

<template>
  <div class="admin">
    <div class="page-head">
      <h2><Shield :size="18" /> Administration</h2>
    </div>

    <div class="admin-grid">
      <!-- utenti -->
      <div class="card">
        <h4><UserIcon :size="14" /> Users <span class="muted">{{ users.length }}</span></h4>
        <table class="rows">
          <tbody>
            <tr v-for="u in users" :key="u.id">
              <td>
                {{ u.email }}
                <span v-if="u.is_superuser" class="tag">admin</span>
                <span v-if="!u.is_active" class="tag off">disabled</span>
                <div v-if="u.full_name" class="muted small">{{ u.full_name }}</div>
              </td>
              <td class="right">
                <button
                  class="mini danger"
                  :disabled="u.id === me?.id"
                  :title="u.id === me?.id ? 'You cannot delete your own account' : 'Delete user'"
                  @click="deleteUser(u)"
                ><Trash2 :size="13" /></button>
              </td>
            </tr>
          </tbody>
        </table>

        <h4 class="subhead"><Plus :size="13" /> New user</h4>
        <input v-model="nu.email" type="email" placeholder="email" />
        <input v-model="nu.password" type="password" placeholder="password (min 6)" autocomplete="new-password" />
        <input v-model="nu.full_name" type="text" placeholder="full name (optional)" />
        <label class="chk"><input v-model="nu.is_superuser" type="checkbox" /> superuser</label>
        <button class="primary" @click="createUser">Create user</button>
      </div>

      <!-- gruppi -->
      <div class="card">
        <h4><UsersIcon :size="14" /> Groups <span class="muted">{{ groups.length }}</span></h4>
        <table class="rows">
          <tbody>
            <tr v-for="g in groups" :key="g.id">
              <td>
                {{ g.name }}
                <div v-if="g.description" class="muted small">{{ g.description }}</div>
              </td>
            </tr>
            <tr v-if="!groups.length"><td class="muted">No groups yet.</td></tr>
          </tbody>
        </table>

        <h4 class="subhead"><Plus :size="13" /> New group</h4>
        <input v-model="ng.name" type="text" placeholder="group name" />
        <input v-model="ng.description" type="text" placeholder="description (optional)" />
        <button class="primary" @click="createGroup">Create group</button>

        <h4 class="subhead"><UsersIcon :size="13" /> Add to group</h4>
        <Select
          v-model="member.user_id"
          :options="users.map((u) => ({ value: u.id, label: u.email }))"
          placeholder="user…"
        />
        <Select
          v-model="member.group_id"
          :options="groups.map((g) => ({ value: g.id, label: g.name }))"
          placeholder="group…"
        />
        <button @click="addMember">Add</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.page-head { margin-bottom: 16px; }
.page-head h2 {
  margin: 0;
  font-size: 19px;
  font-weight: 700;
  display: inline-flex;
  align-items: center;
  gap: 9px;
}
.admin-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  align-items: start;
}
.card {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 16px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
}
.card h4 { margin: 0; display: inline-flex; align-items: center; gap: 7px; }
.subhead { margin-top: 14px !important; color: var(--muted); font-size: 12.5px; }
table.rows { width: 100%; border-collapse: collapse; font-size: 13px; }
table.rows td { padding: 6px 4px; border-bottom: 1px solid var(--border-soft); }
table.rows tr:last-child td { border-bottom: none; }
td.right { text-align: right; width: 40px; }
.small { font-size: 11.5px; }
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
.chk { display: flex; align-items: center; gap: 6px; }
.chk input { width: auto; }
button.mini { padding: 3px 8px; }
.mini.danger { border-color: var(--danger); color: var(--danger); }
.mini.danger:hover:not(:disabled) { background: var(--danger); color: #fff; }
.mini.danger:disabled { opacity: 0.4; cursor: not-allowed; }
</style>

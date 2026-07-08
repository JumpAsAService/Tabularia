<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { ChevronRight, ChevronDown } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useProjects, type UserOut, type GroupOut } from '~/composables/useProjects'

// Amministrazione minimale (solo superuser): crea utenti, gruppi, appartenenze.
const api = useProjects()

const users = ref<UserOut[]>([])
const groups = ref<GroupOut[]>([])
const error = ref('')

const nu = ref({ email: '', password: '', full_name: '', is_superuser: false })
const ng = ref({ name: '', description: '' })
const member = ref<{ user_id: number | null; group_id: number | null }>({ user_id: null, group_id: null })

async function loadAll() {
  try {
    users.value = await api.users()
    groups.value = await api.groups()
  } catch (e) {
    error.value = errMessage(e)
  }
}
onMounted(loadAll)

async function createUser() {
  if (!nu.value.email || nu.value.password.length < 6) {
    error.value = 'Email e password (min 6 caratteri) obbligatorie'
    return
  }
  try {
    await api.createUser({ ...nu.value })
    nu.value = { email: '', password: '', full_name: '', is_superuser: false }
    await loadAll()
  } catch (e) {
    error.value = errMessage(e)
  }
}

async function createGroup() {
  if (!ng.value.name) return
  try {
    await api.createGroup({ ...ng.value })
    ng.value = { name: '', description: '' }
    await loadAll()
  } catch (e) {
    error.value = errMessage(e)
  }
}

async function addMember() {
  if (!member.value.user_id || !member.value.group_id) return
  try {
    await api.addToGroup(member.value.user_id, member.value.group_id)
    error.value = ''
  } catch (e) {
    error.value = errMessage(e)
  }
}

const open = ref(false)
</script>

<template>
  <div class="admin">
    <button class="toggle" @click="open = !open">
      <ChevronDown v-if="open" :size="15" />
      <ChevronRight v-else :size="15" />
      Amministrazione ({{ users.length }} utenti, {{ groups.length }} gruppi)
    </button>

    <div v-if="open" class="admin-body">
      <div class="col">
        <h4>Nuovo utente</h4>
        <input v-model="nu.email" type="email" placeholder="email" />
        <input v-model="nu.password" type="password" placeholder="password (min 6)" />
        <input v-model="nu.full_name" type="text" placeholder="nome (opzionale)" />
        <label class="chk"><input v-model="nu.is_superuser" type="checkbox" /> superuser</label>
        <button class="primary" @click="createUser">Crea utente</button>
      </div>

      <div class="col">
        <h4>Nuovo gruppo</h4>
        <input v-model="ng.name" type="text" placeholder="nome gruppo" />
        <input v-model="ng.description" type="text" placeholder="descrizione (opzionale)" />
        <button class="primary" @click="createGroup">Crea gruppo</button>

        <h4 style="margin-top: 16px">Aggiungi a gruppo</h4>
        <Select
          v-model="member.user_id"
          :options="users.map((u) => ({ value: u.id, label: u.email }))"
          placeholder="utente…"
        />
        <Select
          v-model="member.group_id"
          :options="groups.map((g) => ({ value: g.id, label: g.name }))"
          placeholder="gruppo…"
        />
        <button @click="addMember">Aggiungi</button>
      </div>
    </div>

    <p v-if="error" class="err">{{ error }}</p>
  </div>
</template>

<style scoped>
.admin { margin-top: 16px; }
.toggle {
  width: 100%;
  text-align: left;
  justify-content: flex-start;
  background: var(--panel);
}
.admin-body {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  padding: 14px;
  border: 1px solid var(--border);
  border-top: none;
  border-radius: 0 0 var(--radius) var(--radius);
}
.col { display: flex; flex-direction: column; gap: 8px; }
.col h4 { margin: 0 0 2px; }
.chk { display: flex; align-items: center; gap: 6px; }
.chk input { width: auto; }
.err { color: var(--danger); margin-top: 10px; }
</style>

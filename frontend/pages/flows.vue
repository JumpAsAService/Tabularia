<script setup lang="ts">
// Tutti i flussi nelle cartelle leggibili, con ricerca. Da qui si apre l'editor.
import { computed, onMounted, ref } from 'vue'
import { Workflow, Search, Trash2, Folder, Plus } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { skeletonPad } from '~/composables/useSkeleton'
import { useFlows, type FlowSummary } from '~/composables/useFlows'
import { useProjects } from '~/composables/useProjects'

const flowsApi = useFlows()
const projectsApi = useProjects()
const toast = useToast()

const flows = ref<FlowSummary[]>([])
const folderName = ref<Record<number, string>>({})
const q = ref('')
const loading = ref(true)
const error = ref('')

onMounted(async () => {
  const t0 = performance.now()
  try {
    const [fl, projects] = await Promise.all([flowsApi.list(), projectsApi.list()])
    flows.value = fl
    folderName.value = Object.fromEntries(projects.map((p) => [p.id, p.name]))
  } catch (e) {
    error.value = errMessage(e)
  } finally {
    await skeletonPad(t0) // skeleton visibile almeno il minimo: niente flash
    loading.value = false
  }
})

const filtered = computed(() => {
  const needle = q.value.trim().toLowerCase()
  if (!needle) return flows.value
  return flows.value.filter(
    (f) =>
      f.name.toLowerCase().includes(needle) ||
      (folderName.value[f.project_id] ?? '').toLowerCase().includes(needle),
  )
})

function fmtDate(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  return d.toLocaleDateString('it-IT', { day: '2-digit', month: '2-digit', year: '2-digit' }) +
    ' ' + d.toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' })
}

async function deleteFlow(f: FlowSummary) {
  if (!confirm(`Delete flow "${f.name}"?`)) return
  try {
    await flowsApi.remove(f.id)
    flows.value = flows.value.filter((x) => x.id !== f.id)
    toast.success(`Flow "${f.name}" deleted`)
  } catch (e) {
    toast.error(errMessage(e))
  }
}
</script>

<template>
  <AppShell>
    <div class="page-head">
      <h2><Workflow :size="18" /> Flows <span class="muted count">{{ flows.length }}</span></h2>
      <div class="head-actions">
        <span class="searchbox"><Search :size="14" /><input v-model="q" type="text" placeholder="Search flows…" /></span>
        <NuxtLink to="/editor" class="btn-link"><Plus :size="14" /> New flow</NuxtLink>
      </div>
    </div>

    <p v-if="error" class="err">{{ error }}</p>
    <SkeletonRows v-else-if="loading" :rows="4" />
    <p v-else-if="!filtered.length" class="muted">
      {{ flows.length ? 'No flow matches the search.' : 'No flows yet: create one from a folder or with New flow.' }}
    </p>

    <table v-else class="list">
      <thead>
        <tr><th>Name</th><th>Folder</th><th>Last modified</th><th /></tr>
      </thead>
      <tbody>
        <tr v-for="f in filtered" :key="f.id">
          <td>
            <NuxtLink :to="`/editor?flow=${f.id}`" class="rowlink">
              <Workflow :size="14" /> {{ f.name }}
            </NuxtLink>
            <div v-if="f.description" class="muted desc">{{ f.description }}</div>
          </td>
          <td class="muted"><Folder :size="13" /> {{ folderName[f.project_id] ?? `#${f.project_id}` }}</td>
          <td class="muted nowrap">{{ fmtDate(f.updated_at) }}</td>
          <td class="right">
            <button class="mini danger" title="Delete flow" @click="deleteFlow(f)"><Trash2 :size="13" /></button>
          </td>
        </tr>
      </tbody>
    </table>
  </AppShell>
</template>

<style scoped src="~/assets/listpage.css" />

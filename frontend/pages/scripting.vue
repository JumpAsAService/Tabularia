<script setup lang="ts">
// The Scripting tab (docs/specs/saved-scripts.md): what scripting offers this person,
// read-only — the saved scripts they see, the environments they may write for (their
// packages and variables) and the container registries granted to them. Managing
// environments and registries stays with the administrators. The tab shows only to
// whoever may use an environment or a registry, never on Docker Compose.
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Boxes, CalendarClock, CircleCheck, CircleX, Clock, FileCode2, KeyRound, Plus, Terminal } from 'lucide-vue-next'
import { useFormat } from '~/composables/useFormat'
import { useFolderPaths } from '~/composables/useFolderPaths'
import { useScriptingEditor } from '~/composables/useScripting'
import { useRegistriesEditor } from '~/composables/useRegistries'
import { useSavedScripts, type SavedScript } from '~/composables/useSavedScripts'
import { useProjects, type Project } from '~/composables/useProjects'
import Select from '~/components/ui/Select.vue'

const { t } = useI18n()
const fmtx = useFormat()
const scripting = useScriptingEditor()
const registries = useRegistriesEditor()
const scriptsApi = useSavedScripts()
const projectsApi = useProjects()
const folderPaths = useFolderPaths()

const scripts = ref<SavedScript[]>([])
const projects = ref<Project[]>([])
const loading = ref(true)
const q = ref('')

onMounted(async () => {
  await Promise.allSettled([
    scripting.load(true), registries.load(true),
    scriptsApi.list().then((l) => { scripts.value = l }),
    projectsApi.list().then((l) => { projects.value = l }),
  ])
  loading.value = false
})

const environments = computed(() => scripting.state.value?.environments ?? [])
const grantedRegistries = computed(() => (registries.state.value?.disabled_reason ? [] : registries.state.value?.registries ?? []))
const shown = computed(() => {
  const text = q.value.trim().toLowerCase()
  if (!text) return scripts.value
  return scripts.value.filter((x) => x.name.toLowerCase().includes(text)
    || (folderPaths.pathOf(x.project_id) ?? '').toLowerCase().includes(text)
    || (x.environment_name ?? '').toLowerCase().includes(text))
})

// «New script»: in a folder chosen here (EDIT is checked when it is created)
const choosing = ref(false)
const folderOptions = computed(() => projects.value.map((p) => ({ value: p.id, label: p.name, hint: folderPaths.pathOf(p.parent_id) })))
function newIn(projectId: number | null) {
  if (projectId != null) navigateTo(`/script?project=${projectId}`)
}

const statusIcon = (s: string | null) => (s === 'SUCCESS' ? CircleCheck : s === 'FAILURE' ? CircleX : Clock)
</script>

<template>
  <AppShell>
    <div class="st">
      <div class="page-head">
        <h1><Terminal :size="18" /> {{ $t('scriptingTab.title') }}</h1>
        <p class="muted intro">{{ $t('scriptingTab.intro') }}</p>
      </div>
      <SkeletonRows v-if="loading" :rows="6" />

      <template v-else>
        <section class="card">
          <div class="sec-head">
            <h2><FileCode2 :size="15" /> {{ $t('scriptingTab.scripts') }} <span class="n">{{ scripts.length }}</span></h2>
            <input v-if="scripts.length > 5" v-model="q" type="search" class="filter" :placeholder="$t('scriptingTab.filter')" :aria-label="$t('scriptingTab.filter')" />
            <div v-if="environments.length" class="new">
              <button v-if="!choosing" type="button" class="primary" @click="choosing = true"><Plus :size="14" /> {{ $t('scriptingTab.newScript') }}</button>
              <Select
                v-else
                :model-value="null"
                :options="folderOptions"
                :placeholder="$t('scriptingTab.chooseFolder')"
                searchable
                @update:model-value="newIn"
              />
            </div>
          </div>
          <table v-if="shown.length" class="rows">
            <thead>
              <tr>
                <th>{{ $t('scriptingTab.name') }}</th>
                <th>{{ $t('scriptingTab.folder') }}</th>
                <th>{{ $t('scriptingTab.environment') }}</th>
                <th>{{ $t('scriptingTab.lastRun') }}</th>
                <th>{{ $t('scriptingTab.schedule') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="x in shown" :key="x.id">
                <td><NuxtLink :to="`/script?id=${x.id}`" class="sname">{{ x.name }}</NuxtLink> <span class="muted small">v{{ x.current_version }}</span></td>
                <td class="muted">{{ folderPaths.pathOf(x.project_id) ?? '—' }}</td>
                <td>{{ x.environment_name ?? '—' }}</td>
                <td>
                  <span v-if="x.last_run_at" class="last" :class="(x.last_run_status ?? '').toLowerCase()">
                    <component :is="statusIcon(x.last_run_status)" :size="13" /> {{ fmtx.dateTime(x.last_run_at) }}
                  </span>
                  <span v-else class="muted">{{ $t('scriptingTab.never') }}</span>
                </td>
                <td>
                  <span v-if="x.run_schedule" class="cron"><CalendarClock :size="12" /> {{ x.run_schedule }}</span>
                  <span v-else class="muted">—</span>
                </td>
              </tr>
            </tbody>
          </table>
          <p v-else class="muted small">{{ q ? $t('scriptingTab.noMatch', { q }) : $t('scriptingTab.noScripts') }}</p>
        </section>

        <section v-if="environments.length" class="card">
          <h2><Terminal :size="15" /> {{ $t('scriptingTab.environments') }} <span class="n">{{ environments.length }}</span></h2>
          <div class="grid">
            <article v-for="e in environments" :key="e.id" class="item">
              <div class="item-head">
                <strong>{{ e.name }}</strong>
                <span class="tag">{{ e.language === 'r' ? 'R' : 'Python' }} {{ e.language_version ?? '' }}</span>
                <span class="state" :class="{ ok: e.ready }">{{ e.ready ? $t('scriptingTab.ready') : $t('scriptingTab.notReady') }}</span>
              </div>
              <p v-if="e.description" class="muted small">{{ e.description }}</p>
              <details v-if="e.packages.length">
                <summary>{{ $t('scriptingTab.packages', { n: e.packages.length }) }}</summary>
                <ul class="mono">
                  <li v-for="p in e.packages" :key="p">{{ p }}</li>
                </ul>
              </details>
              <details v-if="e.variables.length">
                <summary>{{ $t('scriptingTab.variables', { n: e.variables.length }) }}</summary>
                <ul class="vars">
                  <li v-for="v in e.variables" :key="v.name">
                    <code>{{ v.name }}</code>
                    <span v-if="v.secret" class="muted"><KeyRound :size="11" /> {{ $t('scriptingTab.secret') }}</span>
                    <span v-else-if="v.from_tabularia" class="muted">{{ $t('scriptingTab.fromTabularia') }}</span>
                    <span v-else>{{ v.value }}</span>
                  </li>
                </ul>
              </details>
            </article>
          </div>
        </section>

        <section v-if="grantedRegistries.length" class="card">
          <h2><Boxes :size="15" /> {{ $t('scriptingTab.registries') }} <span class="n">{{ grantedRegistries.length }}</span></h2>
          <div class="grid">
            <article v-for="r in grantedRegistries" :key="r.id" class="item">
              <div class="item-head"><strong>{{ r.name }}</strong> <code class="muted small">{{ r.host }}</code></div>
              <p v-if="r.description" class="muted small">{{ r.description }}</p>
              <div class="small">{{ $t('scriptingTab.repositories') }}
                <code v-for="p in r.repositories" :key="p" class="repo">{{ p }}</code>
              </div>
              <details v-if="r.variables.length">
                <summary>{{ $t('scriptingTab.variables', { n: r.variables.length }) }}</summary>
                <ul class="vars">
                  <li v-for="v in r.variables" :key="v.name">
                    <code>{{ v.name }}</code>
                    <span v-if="v.secret" class="muted"><KeyRound :size="11" /> {{ $t('scriptingTab.secret') }}</span>
                    <span v-else>{{ v.value }}</span>
                  </li>
                </ul>
              </details>
            </article>
          </div>
        </section>
      </template>
    </div>
  </AppShell>
</template>

<style scoped>
.st { display: flex; flex-direction: column; gap: 14px; }
.page-head h1 { margin: 0; display: inline-flex; align-items: center; gap: 8px; }
.intro { margin: 4px 0 0; font-size: 12.5px; max-width: 760px; }
.card { border: 1px solid var(--border-soft); border-radius: 10px; padding: 12px 14px; background: var(--panel); display: flex; flex-direction: column; gap: 10px; }
.card h2 { margin: 0; font-size: 13.5px; display: inline-flex; align-items: center; gap: 7px; }
.n { font-size: 11px; color: var(--muted); font-weight: 500; }
.sec-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.sec-head h2 { margin-right: auto; }
.filter { max-width: 240px; }
.new { min-width: 260px; display: flex; justify-content: flex-end; }
.new button { display: inline-flex; align-items: center; gap: 6px; }
.rows { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.rows th { text-align: left; font-weight: 600; color: var(--muted); padding: 4px 8px; border-bottom: 1px solid var(--border-soft); }
.rows td { padding: 6px 8px; border-bottom: 1px solid var(--border-soft); vertical-align: middle; }
.sname { font-weight: 600; }
.small { font-size: 12px; }
.last { display: inline-flex; align-items: center; gap: 5px; }
.last.failure { color: var(--danger); }
.last.success svg { color: var(--accent-2); }
.cron { display: inline-flex; align-items: center; gap: 5px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 10px; }
.item { border: 1px solid var(--border-soft); border-radius: 8px; padding: 9px 11px; display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.item-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.item p { margin: 0; }
.tag { font-size: 10.5px; padding: 1px 7px; border-radius: 8px; background: var(--panel-2); border: 1px solid var(--border); color: var(--muted); }
.state { margin-left: auto; font-size: 11px; color: var(--warning); }
.state.ok { color: var(--accent-2); }
details summary { cursor: pointer; font-size: 12px; color: var(--muted); }
.mono { margin: 6px 0 0; padding-left: 16px; max-height: 220px; overflow: auto; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; }
.vars { list-style: none; margin: 6px 0 0; padding: 0; display: flex; flex-direction: column; gap: 3px; font-size: 12px; }
.vars li { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.repo { margin-left: 6px; font-size: 11.5px; }
@media (max-width: 760px) {
  .rows th:nth-child(2), .rows td:nth-child(2), .rows th:nth-child(5), .rows td:nth-child(5) { display: none; }
}
</style>

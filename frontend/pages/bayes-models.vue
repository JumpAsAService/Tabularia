<script setup lang="ts">
// The space of the Bayesian models (docs/specs/bayes-graph-editor.md, «The list»; the approved mockup
// «The list»): the models this person sees, with the family the gateway derives from the saved graph
// (and the template it was opened from under it), the version, the flows that pin it, the compile of
// its current version, the last save and who made it, and the row's actions (duplicate, versions,
// rename, move, delete — refused while a flow uses it). «New model» asks for a folder and opens the
// editor on the empty canvas with the templates. Shown to whoever may use the group `bayes`, never on
// Docker Compose (as the Scripting tab).
import { computed, nextTick, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  CircleCheck, CircleX, Clock, Copy, Dices, Ellipsis, FolderInput, History, Info, LoaderCircle, Pencil, Plus, Trash2, Workflow,
} from 'lucide-vue-next'
import Select from '~/components/ui/Select.vue'
import Modal from '~/components/ui/Modal.vue'
import RowMenu, { type RowMenuItem } from '~/components/ui/RowMenu.vue'
import BayesGlossary from '~/components/bayes/BayesGlossary.vue'
import { useFormat } from '~/composables/useFormat'
import { useFolderPaths } from '~/composables/useFolderPaths'
import { useProjects, type Project } from '~/composables/useProjects'
import { useModelGroups } from '~/composables/useModelGroups'
import { bayesUsable } from '~/composables/modelCatalog'
import { useToast } from '~/composables/useToast'
import { useBayesModels, bayesFamilyKey, type BayesModelListed, type BayesUses } from '~/composables/useBayesModels'
import { bayesCompileCell, bayesFilterModels } from '~/composables/bayesModelsList'
import { bayesFetchError } from '~/composables/bayesModelSave'
import { bayesTemplates } from '~/composables/bayesTemplates'

const { t, te } = useI18n()
const fmt = useFormat()
const toast = useToast()
const api = useBayesModels()
const projectsApi = useProjects()
const folderPaths = useFolderPaths()
const groups = useModelGroups()
const { user } = useAuth()

useHead({ title: computed(() => t('nav.bayesModels')) })

const models = ref<BayesModelListed[]>([])
const projects = ref<Project[]>([])
const loading = ref(true)
const failed = ref('')
const q = ref('')

async function load () {
  try {
    models.value = await api.list()
    failed.value = ''
  } catch (e) {
    failed.value = String((e as Error)?.message ?? '')
  }
}
onMounted(async () => {
  await Promise.allSettled([
    groups.load(true),
    folderPaths.load(),
    load(),
    projectsApi.list().then((l) => { projects.value = l }),
  ])
  loading.value = false
})

const usable = computed(() => bayesUsable(groups.state.value))
const onCompose = computed(() => groups.state.value?.disabled_reason === 'kubernetes')
const templateTitle = (id: string) => (te(`bayes.templates.${id}.title`) ? t(`bayes.templates.${id}.title`) : id)
const pathOf = (id: number) => folderPaths.pathOf(id)
const shown = computed(() => bayesFilterModels(models.value, q.value, pathOf, templateTitle))

// the four families of the empty state, with how many templates each has
const families = computed(() => {
  const all = bayesTemplates()
  return [
    { id: 'glm', n: all.filter(x => x.family === 'glm').length },
    { id: 'hierarchical', n: all.filter(x => x.family === 'hierarchical').length },
    { id: 'series', n: all.filter(x => x.family === 'series').length },
  ]
})
const whatOpen = ref(false)

// «New model»: in a folder chosen here (EDIT is checked when it is created)
const choosing = ref(false)
const folderOptions = computed(() => projects.value.map(p => ({ value: p.id, label: p.name, hint: folderPaths.pathOf(p.parent_id) })))
function newIn (projectId: number | null) {
  if (projectId != null) navigateTo(`/bayes-model?project=${projectId}`)
}
// the folder's Select stands in place of «New model»: closed without a choice, the button comes back
// with the focus (unless a click put it on another field)
const newButton = ref<HTMLButtonElement | null>(null)
function closeChoosing () {
  choosing.value = false
  nextTick(() => { if (document.activeElement === document.body) newButton.value?.focus() })
}

const familyWords = (m: BayesModelListed) => t(bayesFamilyKey(m.family))
const fromWords = (m: BayesModelListed) => (m.template_id ? t('bayesModels.list.openedFrom', { name: templateTitle(m.template_id) }) : t('bayesModels.list.openedEmpty'))
const whoWords = (m: BayesModelListed) => (!m.saved_by ? '' : m.saved_by === user.value?.email ? t('bayesModels.list.you') : m.saved_by)
const compileIcon = (tone: string) => (tone === 'ok' ? CircleCheck : tone === 'run' ? LoaderCircle : tone === 'ko' ? CircleX : Clock)

// ---------------------------------------------------------------- «used by N flows»

const usesOf = ref<{ model: BayesModelListed, uses: BayesUses | null } | null>(null)
async function showUses (m: BayesModelListed) {
  usesOf.value = { model: m, uses: null }
  try {
    const u = await api.uses(m.id)
    if (usesOf.value?.model.id === m.id) usesOf.value = { model: m, uses: u }
  } catch (e) {
    usesOf.value = null
    toast.error(t('bayesModels.list.failed', { error: String((e as Error)?.message ?? '') }))
  }
}

// ---------------------------------------------------------------- the row's actions

const busy = ref<number | null>(null)
async function duplicate (m: BayesModelListed) {
  busy.value = m.id
  try {
    const copy = await api.duplicate(m.id)
    toast.success(t('bayesModels.list.duplicated', { name: copy.name }))
    await load()
  } catch (e) {
    toast.error(t('bayesModels.list.failed', { error: errorWords(e) }))
  } finally {
    busy.value = null
  }
}

const menu = ref<{ model: BayesModelListed, x: number, y: number, byKey: boolean } | null>(null)
function openMenu (m: BayesModelListed, ev: MouseEvent) {
  const r = (ev.currentTarget as HTMLElement).getBoundingClientRect()
  menu.value = { model: m, x: r.right, y: r.bottom + 4, byKey: ev.detail === 0 }
}
const menuItems = computed<RowMenuItem[]>(() => {
  const m = menu.value?.model
  if (!m) return []
  return [
    { id: 'rename', label: t('bayesModels.list.rename'), icon: Pencil, disabled: !m.editable },
    { id: 'move', label: t('bayesModels.list.move'), icon: FolderInput, disabled: !m.editable },
    { id: 'delete', label: m.uses > 0 ? t('bayesModels.list.deleteInUse', { n: m.uses }) : t('bayesModels.list.delete'), icon: Trash2, danger: true, disabled: !m.editable || m.uses > 0, sep: true },
  ]
})
function onMenu (id: string) {
  const m = menu.value?.model
  menu.value = null
  if (!m) return
  if (id === 'rename') { renaming.value = { model: m, name: m.name } } else if (id === 'move') { moving.value = { model: m, to: m.project_id } } else if (id === 'delete') { deleting.value = m }
}

const errorWords = (e: unknown) => {
  const { data } = bayesFetchError(e)
  return typeof data.detail === 'string' ? data.detail : String((e as Error)?.message ?? '')
}

const renaming = ref<{ model: BayesModelListed, name: string } | null>(null)
async function rename () {
  const r = renaming.value
  if (!r || !r.name.trim()) return
  try {
    await api.update(r.model.id, { name: r.name.trim() })
    renaming.value = null
    await load()
  } catch (e) {
    const { status } = bayesFetchError(e)
    toast.error(status === 409 ? t('bayes.editor.store.nameTaken', { name: r.name.trim() }) : t('bayesModels.list.failed', { error: errorWords(e) }))
  }
}

const moving = ref<{ model: BayesModelListed, to: number } | null>(null)
async function move () {
  const mv = moving.value
  if (!mv || mv.to === mv.model.project_id) { moving.value = null; return }
  try {
    await api.update(mv.model.id, { project_id: mv.to })
    moving.value = null
    await load()
  } catch (e) {
    toast.error(t('bayesModels.list.failed', { error: errorWords(e) }))
  }
}

const deleting = ref<BayesModelListed | null>(null)
async function remove () {
  const m = deleting.value
  if (!m) return
  try {
    await api.remove(m.id)
    deleting.value = null
    toast.success(t('projectBrowser.bayesModelDeleted', { name: m.name }))
    await load()
  } catch (e) {
    const { status, data } = bayesFetchError(e)
    deleting.value = null
    if (status === 409 && Array.isArray(data.flows)) toast.error(t('bayesModels.list.inUse', { flows: (data.flows as string[]).join(', ') }))
    else toast.error(t('bayesModels.list.failed', { error: errorWords(e) }))
  }
}
</script>

<template>
  <AppShell>
    <div class="st">
      <div class="page-head">
        <h1><Dices :size="18" aria-hidden="true" /> {{ t('nav.bayesModels') }} <span v-if="!loading && models.length" class="count">{{ models.length }}</span></h1>
        <p class="intro">{{ t('bayesModels.list.intro') }}</p>
      </div>
      <SkeletonRows v-if="loading" :rows="6" />

      <section v-else-if="!usable" class="card empty">
        <h2 class="eh">{{ t('bayesModels.list.unavailableTitle') }}</h2>
        <p>{{ onCompose ? t('bayesModels.list.unavailableCompose') : t('bayesModels.list.unavailable') }}</p>
      </section>

      <section v-else-if="failed" class="card empty">
        <h2 class="eh">{{ t('bayesModels.list.loadFailedTitle') }}</h2>
        <p>{{ t('bayesModels.list.failed', { error: failed }) }}</p>
        <div class="acts2"><button type="button" @click="load">{{ t('bayesModels.list.retry') }}</button></div>
      </section>

      <section v-else-if="!models.length" class="card empty" :aria-label="t('bayesModels.list.emptyTitle')">
        <h2 class="eh">{{ t('bayesModels.list.emptyTitle') }}</h2>
        <p>{{ t('bayesModels.list.emptyWhat') }}</p>
        <div class="fams">
          <span v-for="f in families" :key="f.id"><b>{{ t(`bayesModels.list.familyShort_${f.id}`) }}</b> · {{ t('bayesModels.list.templatesCount', { n: f.n }) }}</span>
          <span><b>{{ t('bayesModels.family_free') }}</b> · {{ t('bayesModels.list.fromScratch') }}</span>
        </div>
        <div class="acts2">
          <button v-if="!choosing" ref="newButton" type="button" class="primary" @click="choosing = true"><Plus :size="14" aria-hidden="true" /> {{ t('bayesModels.list.new') }}</button>
          <div v-else class="newsel">
            <Select :model-value="null" :options="folderOptions" :placeholder="t('bayesModels.list.chooseFolder')" searchable auto-open @update:model-value="newIn" @close="closeChoosing" />
          </div>
          <button type="button" :aria-expanded="whatOpen" aria-controls="bm-what" @click="whatOpen = !whatOpen"><Info :size="14" aria-hidden="true" /> {{ t('bayesModels.list.what') }}</button>
        </div>
        <ExpandBody><div v-if="whatOpen" id="bm-what" class="what"><BayesGlossary /></div></ExpandBody>
      </section>

      <section v-else class="card">
        <div class="sec-head">
          <h2><Dices :size="15" aria-hidden="true" /> {{ t('bayesModels.list.seen') }} <span class="n">{{ models.length }}</span></h2>
          <input v-if="models.length > 5" v-model="q" type="search" class="filter" :placeholder="t('bayesModels.list.filter')" :aria-label="t('bayesModels.list.filter')">
          <div class="new">
            <button v-if="!choosing" ref="newButton" type="button" class="primary" @click="choosing = true"><Plus :size="14" aria-hidden="true" /> {{ t('bayesModels.list.new') }}</button>
            <Select v-else :model-value="null" :options="folderOptions" :placeholder="t('bayesModels.list.chooseFolder')" searchable auto-open @update:model-value="newIn" @close="closeChoosing" />
          </div>
        </div>
        <div v-if="shown.length" class="tw">
          <table class="rows">
            <thead>
              <tr>
                <th>{{ t('bayesModels.list.colName') }}</th>
                <th>{{ t('bayesModels.list.colFolder') }}</th>
                <th :title="t('bayesModels.list.colFamilyHint')">{{ t('bayesModels.list.colFamily') }}</th>
                <th>{{ t('bayesModels.list.colVersion') }}</th>
                <th>{{ t('bayesModels.list.colUses') }}</th>
                <th>{{ t('bayesModels.list.colCompile') }}</th>
                <th>{{ t('bayesModels.list.colSaved') }}</th>
                <th class="r"><span class="sr-only">{{ t('bayesModels.list.colActions') }}</span></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="m in shown" :key="m.id">
                <td class="nmc">
                  <div class="nm">
                    <NuxtLink :to="`/bayes-model?id=${m.id}`" class="sname"><Dices :size="14" aria-hidden="true" /><span>{{ m.name }}</span></NuxtLink>
                    <span v-if="m.description" class="desc">{{ m.description }}</span>
                  </div>
                </td>
                <td class="muted fold">{{ folderPaths.pathOf(m.project_id) ?? '—' }}</td>
                <td>
                  <span class="fam"><b>{{ familyWords(m) }}</b><span :title="fromWords(m)">{{ fromWords(m) }}</span></span>
                </td>
                <td class="ver">v{{ m.current_version }}</td>
                <td>
                  <button v-if="m.uses > 0" type="button" class="uses" :aria-label="t('bayesModels.list.usesOpen', { n: m.uses, name: m.name })" @click="showUses(m)">
                    <Workflow :size="13" aria-hidden="true" /><span>{{ t(m.uses === 1 ? 'bayesModels.list.usesOne' : 'bayesModels.list.usesMany', { n: m.uses }) }}</span>
                  </button>
                  <span v-else class="muted nowrap">{{ t('bayesModels.list.usesNone') }}</span>
                </td>
                <td>
                  <span class="cs" :class="bayesCompileCell(m.compile).tone">
                    <component :is="compileIcon(bayesCompileCell(m.compile).tone)" :size="14" :class="{ spin: m.compile.state === 'running' }" aria-hidden="true" />
                    <span>
                      {{ t(bayesCompileCell(m.compile).key) }}
                      <small v-if="bayesCompileCell(m.compile).sub === 'at'">{{ t('bayesModels.list.since', { when: fmt.ago(m.compile.at) }) }}</small>
                      <small v-else-if="bayesCompileCell(m.compile).sub === 'detail'">{{ m.compile.detail }}</small>
                      <small v-else-if="bayesCompileCell(m.compile).sub === 'firstRun'">{{ t('bayesModels.list.firstRun') }}</small>
                    </span>
                  </span>
                </td>
                <td class="when">
                  <time :datetime="m.updated_at" :title="fmt.dateTime(m.updated_at)">{{ fmt.ago(m.updated_at) }}</time>
                  <span v-if="whoWords(m)">{{ whoWords(m) }}</span>
                </td>
                <td class="r">
                  <span class="acts">
                    <button
                      type="button"
                      class="mini"
                      :disabled="!m.editable || busy === m.id"
                      :aria-label="t('bayesModels.list.duplicate', { name: m.name })"
                      :title="t('bayesModels.list.duplicateShort')"
                      @click="duplicate(m)"
                    >
                      <LoaderCircle v-if="busy === m.id" :size="13" class="spin" aria-hidden="true" /><Copy v-else :size="13" aria-hidden="true" />
                    </button>
                    <NuxtLink :to="`/bayes-model?id=${m.id}#versions`" class="mini ilink" :aria-label="t('bayesModels.list.versions', { name: m.name })" :title="t('bayesModels.list.versionsShort')">
                      <History :size="13" aria-hidden="true" />
                    </NuxtLink>
                    <button
                      type="button"
                      class="mini"
                      aria-haspopup="menu"
                      :aria-expanded="menu?.model.id === m.id"
                      :aria-label="t('bayesModels.list.more', { name: m.name })"
                      :title="t('bayesModels.list.moreShort')"
                      @click="openMenu(m, $event)"
                    >
                      <Ellipsis :size="13" aria-hidden="true" />
                    </button>
                  </span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-else class="muted small">{{ t('bayesModels.list.noMatch', { q }) }}</p>
        <p class="foot"><Info :size="13" aria-hidden="true" />{{ t('bayesModels.list.foot') }}</p>
      </section>
    </div>

    <RowMenu
      :open="!!menu"
      :x="menu?.x ?? 0"
      :y="menu?.y ?? 0"
      align="end"
      :items="menuItems"
      :by-key="menu?.byKey"
      :label="menu ? t('bayesModels.list.more', { name: menu.model.name }) : ''"
      @select="onMenu"
      @close="menu = null"
    />

    <Modal :open="!!usesOf" :title="usesOf ? t('bayesModels.list.usesTitle', { name: usesOf.model.name }) : ''" :width="480" @close="usesOf = null">
      <p v-if="!usesOf?.uses" class="mbody muted"><LoaderCircle :size="13" class="spin" aria-hidden="true" /> {{ t('bayesModels.list.loading') }}</p>
      <template v-else>
        <ul class="ulist">
          <li v-for="f in usesOf.uses.flows" :key="`${f.flow_id}-${f.node_id}`">
            <NuxtLink :to="`/editor?flow=${f.flow_id}`"><Workflow :size="13" aria-hidden="true" />{{ f.flow_name }}</NuxtLink>
            <span class="muted">{{ t('bayesModels.list.pins', { n: f.version }) }}</span>
          </li>
        </ul>
        <p v-if="usesOf.uses.hidden" class="mbody muted">{{ t('bayesModels.list.hiddenFlows', { n: usesOf.uses.hidden }) }}</p>
      </template>
    </Modal>
    <Modal :open="!!renaming" :title="t('bayesModels.list.renameTitle')" :width="420" @close="renaming = null">
      <label v-if="renaming" class="fld">
        <span>{{ t('bayes.editor.store.name') }}</span>
        <input v-model="renaming.name" type="text" maxlength="200" @keyup.enter="rename">
      </label>
      <template #footer>
        <button type="button" @click="renaming = null">{{ t('bayes.editor.store.cancel') }}</button>
        <button type="button" class="primary" :disabled="!renaming?.name.trim()" @click="rename">{{ t('bayesModels.list.rename') }}</button>
      </template>
    </Modal>
    <Modal :open="!!moving" :title="t('bayesModels.list.moveTitle')" :width="420" @close="moving = null">
      <div v-if="moving" class="fld">
        <span>{{ t('bayes.editor.store.folder') }}</span>
        <Select :model-value="moving.to" :options="folderOptions" searchable @update:model-value="(v: number) => { if (moving) moving.to = v }" />
      </div>
      <template #footer>
        <button type="button" @click="moving = null">{{ t('bayes.editor.store.cancel') }}</button>
        <button type="button" class="primary" @click="move">{{ t('bayesModels.list.move') }}</button>
      </template>
    </Modal>
    <Modal :open="!!deleting" :title="t('bayesModels.list.deleteTitle')" :width="440" @close="deleting = null">
      <p class="mbody">{{ deleting ? t('projectBrowser.confirmDeleteBayesModel', { name: deleting.name }) : '' }}</p>
      <template #footer>
        <button type="button" @click="deleting = null">{{ t('bayes.editor.store.cancel') }}</button>
        <button type="button" class="danger" @click="remove"><Trash2 :size="14" aria-hidden="true" />{{ t('bayesModels.list.delete') }}</button>
      </template>
    </Modal>
  </AppShell>
</template>

<style scoped>
.st { display: flex; flex-direction: column; gap: 14px; }
.count { font-size: 13px; font-weight: 500; color: var(--muted); }
.card { border: 1px solid var(--border-soft); border-radius: var(--radius); padding: 12px 14px; background: var(--panel); display: flex; flex-direction: column; gap: 10px; }
.card h2 { margin: 0; font-size: 13.5px; display: inline-flex; align-items: center; gap: 7px; }
.n { font-size: 11px; color: var(--muted); font-weight: 500; }
.sec-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.sec-head h2 { margin-right: auto; }
.filter { max-width: 260px; }
.new { min-width: 200px; display: flex; justify-content: flex-end; }
.new button, .acts2 button { display: inline-flex; align-items: center; gap: 6px; }
.tw { overflow-x: auto; margin: 0 -6px; }
.rows { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.rows th { text-align: left; font-weight: 600; color: var(--muted); padding: 5px 8px; border-bottom: 1px solid var(--border-soft); white-space: nowrap; }
.rows td { padding: 7px 8px; border-bottom: 1px solid var(--border-soft); vertical-align: middle; }
.rows tbody tr { transition: background var(--dur-tint); }
@media (hover: hover) and (pointer: fine) { .rows tbody tr:hover { background: var(--row-hover); } }
.rows th.r, .rows td.r { text-align: right; }
.nmc { min-width: 200px; max-width: 340px; }
.nm { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.sname { display: inline-flex; align-items: center; gap: 6px; font-weight: 600; color: var(--text); text-decoration: none; min-width: 0; }
.sname :deep(svg) { flex: none; color: var(--muted); }
.sname span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
@media (hover: hover) and (pointer: fine) { .sname:hover span { color: var(--accent-hi); text-decoration: underline; text-underline-offset: 3px; } }
.sname:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }
.desc { font-size: 11.5px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; padding-left: 20px; }
.muted { color: var(--muted); }
.fold { white-space: nowrap; max-width: 220px; overflow: hidden; text-overflow: ellipsis; }
.nowrap { white-space: nowrap; }
.small { font-size: 12px; }
.fam { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
.fam b { font-weight: 600; white-space: nowrap; }
/* «opened from …» on one line: the title is in the cell's tooltip when it is cut */
.fam span { font-size: 11px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 230px; }
.ver { font-variant-numeric: tabular-nums; }
/* «3 flows»: a link that lists them */
.uses {
  display: inline-flex; align-items: center; gap: 5px; padding: 0; min-height: 24px; font: inherit; color: var(--text);
  background: none; border: 0; box-shadow: none; white-space: nowrap;
}
.uses::before { display: none; }
.uses :deep(svg) { color: var(--muted); }
.uses span { text-decoration: underline; text-decoration-color: color-mix(in srgb, var(--text) 40%, transparent); text-underline-offset: 3px; }
@media (hover: hover) and (pointer: fine) { .uses:hover span { color: var(--accent-hi); text-decoration-color: currentColor; } }
.uses:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }
.cs { display: inline-flex; align-items: flex-start; gap: 6px; }
.cs :deep(svg) { flex: none; margin-top: 1px; }
.cs > span { display: flex; flex-direction: column; gap: 1px; }
.cs small { font-size: 11px; color: var(--muted); max-width: 200px; }
.cs.ok :deep(svg) { color: var(--accent-2); }
.cs.run :deep(svg) { color: var(--accent); }
.cs.none :deep(svg), .cs.none { color: var(--muted); }
.cs.ko > span { color: var(--danger-text); }
.cs.ko :deep(svg) { color: var(--danger); }
.when { white-space: nowrap; font-variant-numeric: tabular-nums; }
.when time { display: block; }
.when span { display: block; font-size: 11px; color: var(--muted); max-width: 180px; overflow: hidden; text-overflow: ellipsis; }
.acts { display: inline-flex; gap: 4px; }
.acts .mini { display: inline-grid; place-items: center; width: 26px; min-height: 26px; padding: 0; }
.ilink { color: var(--text); text-decoration: none; border: 1px solid var(--border); border-radius: var(--radius-control); background: var(--panel-2); }
@media (hover: hover) and (pointer: fine) { .ilink:hover { border-color: var(--accent); } }
.ilink:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.foot { margin: 2px 0 0; display: flex; gap: 7px; align-items: flex-start; font-size: 11.5px; line-height: 1.45; color: var(--muted); }
.foot :deep(svg) { flex: none; margin-top: 1px; }
.empty { max-width: 760px; padding: 18px 22px 20px; gap: 12px; }
.empty .eh { font-size: 14px; font-weight: 700; }
.empty p { margin: 0; font-size: 12.5px; line-height: 1.55; color: var(--muted); max-width: 68ch; }
.fams { display: flex; flex-wrap: wrap; gap: 6px 18px; font-size: 12px; color: var(--muted); }
.fams b { color: var(--text); font-weight: 600; }
.acts2 { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 4px; }
.newsel { min-width: 260px; }
.what { border-top: 1px solid var(--border-soft); padding-top: 12px; }
.mbody { margin: 0 0 8px; line-height: 1.5; display: flex; align-items: center; gap: 6px; }
.ulist { list-style: none; margin: 0 0 8px; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.ulist li { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
.ulist a { display: inline-flex; align-items: center; gap: 6px; color: var(--text); }
.ulist a :deep(svg) { color: var(--muted); }
.fld { display: flex; flex-direction: column; gap: 5px; font-size: 12px; color: var(--muted); }
@media (max-width: 1399px) {
  .nmc { max-width: 280px; }
  .fold { max-width: 160px; }
  .fam span { max-width: 170px; }
}
@media (max-width: 1100px) {
  .rows th:nth-child(2), .rows td:nth-child(2) { display: none; }
}
</style>

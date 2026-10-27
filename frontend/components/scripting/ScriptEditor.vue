<script setup lang="ts">
// The code of a Script node, in Monaco (the editor of VS Code). On the left the node's
// files: modules next to the entrypoint, in folders if wanted; the one marked ▶ is
// run. On the right what the script finds in its environment — Tabularia's
// variables, the environment's own, the installed packages — inserted at the cursor
// with a click and offered while typing (scriptCompletions.ts).
//
// Every change goes to the node at once (debounced), like the other fields of the
// node panel: the flow's undo and save cover it.
//
// Python is checked by Ruff in the gateway when typing pauses (undefined names, syntax
// errors, unused imports: underlined); a failed last run marks the line it stopped at,
// until that file changes, and its log is at the bottom.
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  Check, CheckCircle2, FilePlus2, FileCode2, KeyRound, LoaderCircle, Package, Pencil, Play, Sparkles, Trash2, Variable, X, XCircle,
} from 'lucide-vue-next'
import type * as Monaco from 'monaco-editor'
import Modal from '~/components/ui/Modal.vue'
import { useScriptingEditor, type EditorEnvironment, type LintDiagnostic, type ScriptNodeRun } from '~/composables/useScripting'
import { useFormat } from '~/composables/useFormat'
import { runError } from '~/composables/scriptDiagnostics'
import { useTheme } from '~/composables/useTheme'
import { languageOf, loadMonaco, type MonacoApi } from '~/composables/useMonaco'
import { suggest, variableReference, type CompletionContext } from '~/composables/scriptCompletions'
import { useAi, type AiStatus, type ScriptProposal } from '~/composables/useAi'
import ScriptAssistant, { type ScriptInputColumns } from '~/components/scripting/ScriptAssistant.vue'

interface ScriptFile { path: string; content: string }

const props = defineProps<{
  open: boolean
  title: string
  environment: EditorEnvironment
  files: ScriptFile[]
  entrypoint: string
  tabulariaVariables: string[]
  readOnly?: boolean
  lastRun?: ScriptNodeRun | null
  // a saved script's page, not a Script node: the texts about the node and the flow differ
  saved?: boolean
  // for the assistant: the inputs' columns (names and types, no rows) and whether the
  // script must, may or does not write TABULARIA_OUTPUT
  inputs?: ScriptInputColumns[]
  output?: 'required' | 'optional' | 'none'
  // one file and nothing else: a notebook's marimo file. No list of files, no last run,
  // no assistant (it writes scripts, not notebooks)
  single?: boolean
}>()
const emit = defineEmits<{
  (e: 'update', patch: { files: ScriptFile[]; entrypoint: string }): void
  (e: 'close'): void
}>()
const { t } = useI18n()
const fmt = useFormat()
const { theme } = useTheme()
const scripting = useScriptingEditor()

const language = computed<'python' | 'r'>(() => props.environment.language)
const local = ref<ScriptFile[]>(props.files.map((f) => ({ ...f })))
const entry = ref(props.entrypoint || local.value[0]?.path || '')
const current = ref(entry.value || local.value[0]?.path || '')
const loading = ref(true)
const loadError = ref('')
const completions = ref<Record<string, string[]>>({})

const host = ref<HTMLElement | null>(null)
const monaco = shallowRef<MonacoApi | null>(null)
const editor = shallowRef<Monaco.editor.IStandaloneCodeEditor | null>(null)
const models = new Map<string, Monaco.editor.ITextModel>()
const disposables: Monaco.IDisposable[] = []

// ── changes go to the node ───────────────────────────────────────────────────
let timer: ReturnType<typeof setTimeout> | undefined
function push(now = false) {
  clearTimeout(timer)
  if (props.readOnly) return // a version looked at, or a reader: nothing goes back to the page
  const send = () => emit('update', { files: local.value.map((f) => ({ ...f })), entrypoint: entry.value })
  if (now) send()
  else timer = setTimeout(send, 400)
}

function modelFor(file: ScriptFile): Monaco.editor.ITextModel {
  const m = monaco.value!
  let model = models.get(file.path)
  if (!model) {
    model = m.editor.createModel(file.content, languageOf(file.path), m.Uri.parse(`inmemory://script/${file.path}`))
    disposables.push(model.onDidChangeContent(() => {
      const f = local.value.find((x) => x.path === file.path)
      if (f) {
        f.content = model!.getValue()
        push()
        clearRunMarker(f.path)
        scheduleLint(f.path)
      }
    }))
    models.set(file.path, model)
    markRunError()
    scheduleLint(file.path, 0)
  }
  return model
}

// ── Ruff, while typing ───────────────────────────────────────────────────────
const LINT_PAUSE = 800
const lintTimers = new Map<string, ReturnType<typeof setTimeout>>()
// errors and warnings of each file, Ruff's and the last run's, for the list of files
const problems = ref<Record<string, { errors: number; warnings: number }>>({})

function countProblems(path: string) {
  const m = monaco.value
  const model = models.get(path)
  if (!m || !model) return
  const markers = m.editor.getModelMarkers({ resource: model.uri })
  problems.value = {
    ...problems.value,
    [path]: {
      errors: markers.filter((x) => x.severity === m.MarkerSeverity.Error).length,
      warnings: markers.filter((x) => x.severity === m.MarkerSeverity.Warning).length,
    },
  }
}

function scheduleLint(path: string, wait = LINT_PAUSE) {
  if (language.value !== 'python' || !path.endsWith('.py') || !scripting.state.value?.lint) return
  clearTimeout(lintTimers.get(path))
  lintTimers.set(path, setTimeout(() => lint(path), wait))
}

async function lint(path: string) {
  const m = monaco.value
  const model = models.get(path)
  if (!m || !model || model.isDisposed()) return
  const version = model.getVersionId()
  let answer: { diagnostics: LintDiagnostic[]; skipped: boolean }
  try {
    answer = await scripting.lint(props.environment.id, path, model.getValue(), local.value.map((f) => f.path))
  } catch {
    return // no check: nothing underlined, as before
  }
  if (model.isDisposed() || model.getVersionId() !== version) return // typed since: a later check answers
  if (answer.skipped) {
    scheduleLint(path, 1000)
    return
  }
  m.editor.setModelMarkers(model, 'ruff', answer.diagnostics.map((d) => ({
    startLineNumber: d.line,
    startColumn: d.column,
    endLineNumber: d.end_line,
    endColumn: d.end_column,
    message: d.message,
    code: d.code,
    source: 'Ruff',
    severity: d.severity === 'error' ? m.MarkerSeverity.Error : m.MarkerSeverity.Warning,
  })))
  countProblems(path)
}

// ── the last run ─────────────────────────────────────────────────────────────
const failed = computed(() => props.lastRun?.status === 'FAILURE' ? props.lastRun : null)
const stopped = computed(() => failed.value && !failed.value.stale
  ? runError(language.value, failed.value.log, local.value.map((f) => f.path))
  : null)
// the files changed since the editor opened: the run's line no longer says where it stopped
const editedSinceRun = new Set<string>()

function markRunError() {
  const m = monaco.value
  const where = stopped.value
  if (!m) return
  for (const [path, model] of models) {
    const mark = where && where.path === path && !editedSinceRun.has(path)
      && where.line <= model.getLineCount() ? where : null
    m.editor.setModelMarkers(model, 'run', mark ? [{
      startLineNumber: mark.line,
      startColumn: model.getLineFirstNonWhitespaceColumn(mark.line) || 1,
      endLineNumber: mark.line,
      endColumn: model.getLineMaxColumn(mark.line),
      message: mark.message || t('scripting.editor.runStoppedHere'),
      source: t('scripting.editor.lastRunSource'),
      severity: m.MarkerSeverity.Error,
    }] : [])
    countProblems(path)
  }
}

function clearRunMarker(path: string) {
  if (editedSinceRun.has(path)) return
  editedSinceRun.add(path)
  if (stopped.value?.path === path) markRunError()
}

watch(stopped, () => markRunError())

function goToError() {
  const where = stopped.value
  if (!where || !editor.value) return
  show(where.path)
  editor.value.revealLineInCenter(where.line)
  editor.value.setPosition({ lineNumber: where.line, column: 1 })
}

const logOpen = ref(!!failed.value)

function show(path: string) {
  const file = local.value.find((f) => f.path === path)
  if (!file || !editor.value) return
  current.value = path
  editor.value.setModel(modelFor(file))
  editor.value.updateOptions({ tabSize: language.value === 'r' ? 2 : 4 })
  editor.value.focus()
}

// ── the files ────────────────────────────────────────────────────────────────
const PATH = /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}(\/[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}){0,7}$/
const newPath = ref('')
const renaming = ref<string | null>(null)
const renameTo = ref('')
const fileError = ref('')
const sorted = computed(() => [...local.value].sort((a, b) => a.path.localeCompare(b.path)))

function validPath(path: string, except?: string): string {
  if (!PATH.test(path) || path.split('/').includes('..')) return t('scripting.editor.badPath')
  if (local.value.some((f) => f.path.toLowerCase() === path.toLowerCase() && f.path !== except)) return t('scripting.editor.pathTaken')
  return ''
}

function addFile() {
  const path = newPath.value.trim()
  fileError.value = validPath(path)
  if (fileError.value) return
  local.value.push({ path, content: '' })
  if (!entry.value) entry.value = path
  newPath.value = ''
  push(true)
  lintAll()
  nextTick(() => show(path))
}

function startRename(path: string) {
  renaming.value = path
  renameTo.value = path
  fileError.value = ''
}

function rename() {
  const from = renaming.value
  const to = renameTo.value.trim()
  if (!from) return
  if (to === from) { renaming.value = null; return }
  fileError.value = validPath(to, from)
  if (fileError.value) return
  const file = local.value.find((f) => f.path === from)!
  models.get(from)?.dispose()
  models.delete(from)
  file.path = to
  if (entry.value === from) entry.value = to
  renaming.value = null
  push(true)
  lintAll()
  nextTick(() => show(to))
}

function remove(path: string) {
  if (!confirm(t('scripting.editor.confirmDelete', { path }))) return
  local.value = local.value.filter((f) => f.path !== path)
  models.get(path)?.dispose()
  models.delete(path)
  if (entry.value === path) entry.value = local.value[0]?.path ?? ''
  push(true)
  lintAll()
  if (current.value === path) {
    const next = entry.value || local.value[0]?.path
    if (next) show(next)
    else editor.value?.setModel(null)
  }
}

// the files changed: an import of a module just added (or removed) reads differently
function lintAll() {
  for (const f of local.value) if (models.has(f.path)) scheduleLint(f.path)
}

function makeEntry(path: string) {
  entry.value = path
  push(true)
}

// ── the assistant, beside the code (when the installation has one) ───────────
const aiApi = useAi()
const aiStatus = ref<AiStatus | null>(null)
const side = ref<'env' | 'assistant'>('env')
// mounted the first time it is opened (its AI notice asks then, not when the editor opens),
// and kept while the variables are shown: the conversation stays
const assistantOpened = ref(false)
watch(side, (s) => { if (s === 'assistant') assistantOpened.value = true })
const aiAvailable = computed(() => !!aiStatus.value?.enabled && !!aiStatus.value.models.length && !props.readOnly && !props.single)
onMounted(async () => {
  try { aiStatus.value = await aiApi.status() } catch { /* no assistant: the panel stays as it was */ }
})

// a proposal of the assistant, as ONE change: its files replaced in their models with
// an edit (Ctrl+Z takes it back), the new ones added, the removed ones dropped
function applyProposal(p: ScriptProposal) {
  const m = monaco.value
  for (const path of p.removed) {
    local.value = local.value.filter((f) => f.path !== path)
    models.get(path)?.dispose()
    models.delete(path)
  }
  for (const f of p.files) {
    if (!p.changed.includes(f.path)) continue
    const mine = local.value.find((x) => x.path === f.path)
    const model = models.get(f.path)
    if (mine && model && m) {
      // a stop before and after: Ctrl+Z takes back the proposal, and only the proposal
      model.pushStackElement()
      model.pushEditOperations([], [{ range: model.getFullModelRange(), text: f.content }], () => null)
      model.pushStackElement()
    } else if (mine) {
      mine.content = f.content
    } else {
      local.value.push({ path: f.path, content: f.content })
    }
  }
  entry.value = local.value.some((f) => f.path === p.entrypoint) ? p.entrypoint : entry.value
  push(true)
  lintAll()
  const first = p.changed[0]
  if (first) nextTick(() => show(first))
  else if (!local.value.some((f) => f.path === current.value)) nextTick(() => show(entry.value))
}

// ── what the script finds in its environment ─────────────────────────────────
const variables = computed(() => [
  ...props.tabulariaVariables.map((name) => ({ name, source: 'tabularia' as const, value: null as string | null })),
  ...props.environment.variables.map((v) => ({
    name: v.name,
    source: v.from_tabularia ? 'exception' as const : v.secret ? 'secret' as const : 'plain' as const,
    value: v.value,
  })),
])

const modules = computed(() => {
  if (language.value === 'python') return Object.keys(completions.value).sort()
  return props.environment.packages.map((p) => p.split('@')[0]!).sort()
})

function insert(text: string) {
  const ed = editor.value
  if (!ed || props.readOnly) return
  const sel = ed.getSelection()
  if (!sel) return
  ed.executeEdits('tabularia', [{ range: sel, text, forceMoveMarkers: true }])
  ed.focus()
}

function insertVariable(name: string) {
  insert(variableReference(language.value, name))
}

function insertModule(name: string) {
  insert(language.value === 'python' ? `import ${name}\n` : `library(${name})\n`)
}

// ── Monaco ───────────────────────────────────────────────────────────────────
function context(): CompletionContext {
  return {
    language: language.value,
    variables: variables.value.map((v) => ({
      name: v.name,
      detail: v.source === 'tabularia' ? t('scripting.editor.setByTabularia')
        : v.source === 'secret' ? t('scripting.editor.secret') : (v.value ?? ''),
    })),
    completions: completions.value,
    files: local.value,
  }
}

function registerCompletions(m: MonacoApi) {
  const kinds = {
    variable: m.languages.CompletionItemKind.Variable,
    module: m.languages.CompletionItemKind.Module,
    member: m.languages.CompletionItemKind.Field,
    function: m.languages.CompletionItemKind.Function,
  }
  const provider: Monaco.languages.CompletionItemProvider = {
    triggerCharacters: language.value === 'python' ? ['.', '"', "'", ' '] : [':', '"', "'", '('],
    provideCompletionItems(model, position) {
      const before = model.getLineContent(position.lineNumber).slice(0, position.column - 1)
      const word = model.getWordUntilPosition(position)
      const range = new m.Range(position.lineNumber, word.startColumn, position.lineNumber, word.endColumn)
      const items = suggest(context(), before, model.getValue()).slice(0, 2000)
      return {
        suggestions: items.map((s) => ({
          label: s.label, kind: kinds[s.kind], detail: s.detail, insertText: s.label, range,
        })),
      }
    },
  }
  disposables.push(m.languages.registerCompletionItemProvider(language.value, provider))
}

onMounted(async () => {
  try {
    const [m, comp] = await Promise.all([loadMonaco(), scripting.completions(props.environment.id, props.environment.version)])
    monaco.value = m
    completions.value = comp
    loading.value = false
    await nextTick()
    if (!host.value) return
    editor.value = m.editor.create(host.value, {
      automaticLayout: true,
      fontSize: 13,
      minimap: { enabled: true },
      scrollBeyondLastLine: false,
      readOnly: !!props.readOnly,
      theme: theme.value === 'light' ? 'vs' : 'vs-dark',
      tabSize: language.value === 'r' ? 2 : 4,
      insertSpaces: true,
      renderWhitespace: 'selection',
      quickSuggestions: { other: true, comments: false, strings: true },
    })
    registerCompletions(m)
    // every file at once: the list marks the problems of the modules not opened yet
    for (const f of local.value) modelFor(f)
    if (current.value) show(current.value)
  } catch (e) {
    loading.value = false
    loadError.value = t('scripting.editor.loadFailed')
  }
})

watch(theme, (th) => monaco.value?.editor.setTheme(th === 'light' ? 'vs' : 'vs-dark'))

// Ctrl/Cmd+S saves the flow (or the script's page) while the editor is open: what was
// typed in the last 400 ms goes to the node first. In capture, before their handlers.
function flushOnSave(e: KeyboardEvent) {
  if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === 's') push(true)
}
onMounted(() => window.addEventListener('keydown', flushOnSave, true))

onBeforeUnmount(() => {
  window.removeEventListener('keydown', flushOnSave, true)
  push(true) // what was typed in the last moment
  clearTimeout(timer)
  for (const x of lintTimers.values()) clearTimeout(x)
  for (const d of disposables) d.dispose()
  editor.value?.dispose()
  for (const model of models.values()) model.dispose()
  models.clear()
})
</script>

<template>
  <Modal :open="open" :title="title" :width="1480" @close="emit('close')">
    <template #head>
      <span class="environment">
        <FileCode2 :size="13" /> {{ environment.name }} · {{ environment.language === 'r' ? 'R' : 'Python' }}
        {{ environment.language_version ?? '' }}
      </span>
      <button v-if="aiAvailable" type="button" class="assist-toggle" :class="{ on: side === 'assistant' }"
              :aria-pressed="side === 'assistant'" @click="side = side === 'assistant' ? 'env' : 'assistant'">
        <Sparkles :size="13" /> {{ $t('scriptAssistant.toggle') }}
      </button>
    </template>
    <!-- nokey: the flow editor's keys (Space pans, Backspace deletes the selected node) stay
         out of the code. Monaco types through EditContext on Chrome and Edge: an element
         Vue Flow does not recognise as a text field -->
    <div class="layout nokey" :class="{ wide: aiAvailable && side === 'assistant', single }">
      <!-- the node's files -->
      <aside v-if="!single" class="files" :aria-label="$t('scripting.editor.files')">
        <div class="side-title">{{ $t('scripting.editor.files') }}</div>
        <ul>
          <li v-for="f in sorted" :key="f.path" :class="{ 'is-on': f.path === current }">
            <template v-if="renaming === f.path">
              <input v-model="renameTo" class="rename" spellcheck="false" :aria-label="$t('scripting.editor.rename')"
                     @keydown.enter.prevent="rename" @keydown.esc.stop.prevent="renaming = null" />
              <button type="button" class="icona" :aria-label="$t('scripting.editor.rename')" @click="rename"><Check :size="13" /></button>
            </template>
            <template v-else>
              <button type="button" class="file" :title="f.path" @click="show(f.path)">
                <Play v-if="f.path === entry" :size="11" class="entry" :aria-label="$t('scripting.editor.runsThis')" />
                <span>{{ f.path }}</span>
                <span v-if="problems[f.path]?.errors" class="problems errors"
                      :title="$t('scripting.editor.problems', { errors: problems[f.path]!.errors, warnings: problems[f.path]!.warnings })">
                  {{ problems[f.path]!.errors }}</span>
                <span v-else-if="problems[f.path]?.warnings" class="problems"
                      :title="$t('scripting.editor.problems', { errors: 0, warnings: problems[f.path]!.warnings })">
                  {{ problems[f.path]!.warnings }}</span>
              </button>
              <span v-if="!readOnly" class="file-actions">
                <button v-if="f.path !== entry" type="button" class="icona" :title="$t('scripting.editor.makeEntry')"
                        :aria-label="$t('scripting.editor.makeEntry')" @click="makeEntry(f.path)"><Play :size="12" /></button>
                <button type="button" class="icona" :title="$t('scripting.editor.rename')" :aria-label="$t('scripting.editor.rename')"
                        @click="startRename(f.path)"><Pencil :size="12" /></button>
                <button type="button" class="icona" :title="$t('scripting.editor.delete')" :aria-label="$t('scripting.editor.delete')"
                        @click="remove(f.path)"><Trash2 :size="12" /></button>
              </span>
            </template>
          </li>
        </ul>
        <form v-if="!readOnly" class="new" @submit.prevent="addFile">
          <input v-model="newPath" spellcheck="false" :placeholder="language === 'r' ? 'R/helpers.R' : 'lib/helpers.py'"
                 :aria-label="$t('scripting.editor.newFile')" />
          <button type="submit" class="icona" :title="$t('scripting.editor.newFile')" :aria-label="$t('scripting.editor.newFile')"
                  :disabled="!newPath.trim()"><FilePlus2 :size="14" /></button>
        </form>
        <p v-if="fileError" class="error" role="alert">{{ fileError }}</p>
        <p class="note">{{ language === 'r' ? $t('scripting.editor.modulesHintR') : $t('scripting.editor.modulesHintPython') }}</p>
      </aside>

      <!-- the code, and the last run's log under it -->
      <div class="middle">
        <div class="code" data-own-escape>
          <div v-if="loading" class="state"><LoaderCircle :size="16" class="spin" /> {{ $t('scripting.editor.loading') }}</div>
          <div v-else-if="loadError" class="state error">{{ loadError }}</div>
          <div v-else-if="!local.length" class="state muted">{{ $t('scripting.editor.noFiles') }}</div>
          <div ref="host" class="monaco" :class="{ hidden: loading || !!loadError || !local.length }" />
        </div>
        <details v-if="!single" class="last" :open="logOpen" @toggle="logOpen = ($event.target as HTMLDetailsElement).open">
          <summary>
            <template v-if="lastRun">
              <component :is="lastRun.status === 'SUCCESS' ? CheckCircle2 : XCircle" :size="13"
                         :class="lastRun.status === 'SUCCESS' ? 'ok' : 'ko'" />
              {{ $t(lastRun.status === 'SUCCESS' ? 'scripting.editor.lastRunOk' : lastRun.status === 'FAILURE'
                ? 'scripting.editor.lastRunFailed' : 'scripting.editor.lastRunOther',
                    { when: fmt.ago(lastRun.finished_at ?? lastRun.started_at), status: lastRun.status }) }}
              <span v-if="lastRun.stale" class="muted">· {{ $t('scripting.editor.runStale') }}</span>
            </template>
            <span v-else class="muted">{{ $t(saved ? 'scripting.editor.noRunSaved' : 'scripting.editor.noRun') }}</span>
            <button v-if="stopped" type="button" class="go" @click.prevent="goToError">
              {{ $t('scripting.editor.goTo', { place: `${stopped.path}:${stopped.line}` }) }}
            </button>
          </summary>
          <pre v-if="lastRun?.log" class="log">{{ lastRun.log }}</pre>
          <p v-else-if="lastRun?.error" class="log">{{ lastRun.error }}</p>
        </details>
      </div>

      <!-- the assistant, in place of the environment's panel -->
      <aside v-if="aiAvailable && aiStatus && assistantOpened" v-show="side === 'assistant'" class="env assist">
        <ScriptAssistant
          :status="aiStatus"
          :environment-id="environment.id"
          :files="local"
          :entrypoint="entry"
          :variables="variables.map((v) => v.name)"
          :inputs="inputs ?? []"
          :output="output ?? 'optional'"
          :saved="!!saved"
          :last-run="lastRun ?? null"
          :read-only="readOnly"
          @apply="applyProposal"
        />
      </aside>
      <!-- what the script finds -->
      <aside v-show="!aiAvailable || side !== 'assistant'" class="env" :aria-label="$t('scripting.editor.environment')">
        <div class="side-title"><Variable :size="13" /> {{ $t('scripting.editor.variables') }}</div>
        <p class="note">{{ $t('scripting.editor.variablesHint') }}</p>
        <ul class="vars">
          <li v-for="v in variables" :key="v.name">
            <button type="button" class="var" :disabled="readOnly" @click="insertVariable(v.name)">
              <code>{{ v.name }}</code>
              <span v-if="v.source === 'tabularia'" class="tag">{{ $t('scripting.editor.setByTabularia') }}</span>
              <span v-else-if="v.source === 'secret'" class="tag"><KeyRound :size="10" /> {{ $t('scripting.editor.secret') }}</span>
              <span v-else-if="v.source === 'exception'" class="tag exception">{{ $t('scripting.editor.fromTabularia', { name: v.value }) }}</span>
              <span v-else class="value">{{ v.value }}</span>
            </button>
          </li>
        </ul>
        <div class="side-title"><Package :size="13" /> {{ $t('scripting.editor.packages', { n: modules.length }) }}</div>
        <ul class="mods">
          <li v-for="m in modules" :key="m">
            <button type="button" class="mod" :disabled="readOnly" :title="$t('scripting.editor.insertImport')" @click="insertModule(m)">{{ m }}</button>
          </li>
        </ul>
      </aside>
    </div>
    <template #footer>
      <span class="muted foot">{{ $t(saved ? 'scripting.editor.footerSaved' : 'scripting.editor.footer') }}</span>
      <button type="button" class="primary" @click="emit('close')"><X :size="13" /> {{ $t('scripting.editor.close') }}</button>
    </template>
  </Modal>
</template>

<style scoped>
.environment { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted); margin-right: 8px; }
.assist-toggle { display: inline-flex; align-items: center; gap: 5px; padding: 3px 10px; font-size: 12px; margin-right: 6px; }
.assist-toggle.on { border-color: var(--accent); color: var(--accent-hi); background: color-mix(in srgb, var(--accent) 10%, transparent); }
.layout { display: grid; grid-template-columns: 230px minmax(0, 1fr) 260px; gap: 12px; height: min(72vh, 900px); min-height: 420px; }
.layout.wide { grid-template-columns: 210px minmax(0, 1fr) 360px; }
.layout.single { grid-template-columns: minmax(0, 1fr) 260px; }
.env.assist { overflow: hidden; }
.files, .env { display: flex; flex-direction: column; gap: 6px; min-height: 0; overflow: auto; }
.side-title { display: flex; align-items: center; gap: 6px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); margin-top: 4px; }
ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
.files li { display: flex; align-items: center; gap: 2px; border-radius: 6px; }
.files li.is-on { background: var(--bg-soft); }
.file { flex: 1; min-width: 0; display: flex; align-items: center; justify-content: flex-start; gap: 5px; border: 0; background: none; padding: 5px 6px; text-align: left;
  font-family: var(--font-mono, ui-monospace, monospace); font-size: 12px; color: var(--text); cursor: pointer; min-height: 0; }
.file span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.entry { color: var(--accent-2); flex: none; }
.file-actions { display: none; }
.files li:hover .file-actions, .files li:focus-within .file-actions { display: inline-flex; }
.icona { border: 0; background: none; padding: 3px; min-height: 0; color: var(--muted); cursor: pointer; border-radius: 5px; display: inline-grid; place-items: center; }
.icona:hover:not(:disabled) { color: var(--text); background: var(--panel-2); }
.rename, .new input { flex: 1; min-width: 0; font-family: var(--font-mono, ui-monospace, monospace); font-size: 12px; padding: 4px 6px; }
.new { display: flex; gap: 4px; align-items: center; margin-top: 4px; }
.note { margin: 0; font-size: 11.5px; line-height: 1.45; color: var(--muted); }
.error { margin: 0; font-size: 12px; color: var(--danger); }
.middle { display: flex; flex-direction: column; gap: 8px; min-width: 0; min-height: 0; }
.code { position: relative; flex: 1; min-width: 0; min-height: 0; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
.last { flex: none; font-size: 12px; }
.last summary { display: flex; align-items: center; gap: 6px; cursor: pointer; flex-wrap: wrap; }
.last .ok { color: var(--accent-2); }
.last .ko { color: var(--danger); }
.go { margin-left: auto; border: 0; background: none; min-height: 0; padding: 2px 6px; font-size: 12px; color: var(--danger);
  font-family: var(--font-mono, ui-monospace, monospace); cursor: pointer; border-radius: 5px; text-decoration: underline; }
.go:hover { background: var(--bg-soft); }
.last .log { margin: 6px 0 0; max-height: 22vh; overflow: auto; font-size: 11.5px; padding: 8px; border-radius: 6px;
  background: var(--bg-soft); white-space: pre-wrap; overflow-wrap: anywhere; }
.problems { margin-left: auto; flex: none; font-size: 10.5px; min-width: 16px; padding: 0 5px; border-radius: 999px; text-align: center;
  color: var(--warning); border: 1px solid color-mix(in srgb, var(--warning) 50%, var(--border)); }
.problems.errors { color: var(--danger); border-color: color-mix(in srgb, var(--danger) 50%, var(--border)); }
.monaco { position: absolute; inset: 0; }
.monaco.hidden { visibility: hidden; }
.state { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; gap: 8px; font-size: 13px; z-index: 1; }
.vars .var, .mods .mod { width: 100%; display: flex; align-items: center; justify-content: flex-start; gap: 6px; flex-wrap: wrap; border: 0; background: none; padding: 4px 6px;
  border-radius: 6px; text-align: left; cursor: pointer; min-height: 0; font-size: 12px; color: var(--text); }
.vars .var:hover:not(:disabled), .mods .mod:hover:not(:disabled) { background: var(--bg-soft); }
.var code { font-size: 11.5px; }
.tag { font-size: 10.5px; padding: 0 6px; border-radius: 999px; border: 1px solid var(--border); color: var(--muted); display: inline-flex; align-items: center; gap: 3px; }
.tag.exception { color: var(--warning); border-color: color-mix(in srgb, var(--warning) 50%, var(--border)); }
.value { font-size: 11px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 100%; }
.mods { max-height: 40%; overflow: auto; }
.mod { font-family: var(--font-mono, ui-monospace, monospace); }
.foot { flex: 1; font-size: 12px; }
@media (max-width: 1000px) {
  .layout, .layout.single { grid-template-columns: 1fr; height: auto; }
  .code { height: 60vh; flex: none; }
}
</style>

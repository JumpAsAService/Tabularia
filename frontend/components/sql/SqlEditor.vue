<script setup lang="ts">
// The SQL editor of a SQL script: Monaco with SQL highlighting, the completion of the
// datasources the person may read (their names as the SQL writes them) and of their
// columns, and Ctrl/Cmd+Enter to run — the selection when there is one, else everything.
// Monaco is loaded the first time an editor opens, never with the page.
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type * as Monaco from 'monaco-editor'
import { loadMonaco, type MonacoApi } from '~/composables/useMonaco'
import { useTheme } from '~/composables/useTheme'
import { sqlName, type SqlCatalogItem } from '~/composables/useSqlScripts'

const props = defineProps<{ modelValue: string; catalog: SqlCatalogItem[]; readOnly?: boolean }>()
const emit = defineEmits<{ (e: 'update:modelValue', v: string): void; (e: 'run', sql: string): void }>()

const { t } = useI18n()
const { theme } = useTheme()
const host = ref<HTMLElement | null>(null)
const loading = ref(true)
const loadError = ref('')
let monaco: MonacoApi | null = null
let editor: Monaco.editor.IStandaloneCodeEditor | null = null
const disposables: Monaco.IDisposable[] = []

/** What «Run» runs: the selected text, or the whole SQL. */
function runText(): string {
  const sel = editor?.getSelection()
  const model = editor?.getModel()
  if (sel && model && !sel.isEmpty()) return model.getValueInRange(sel)
  return editor?.getValue() ?? props.modelValue
}

/** Writes `text` where the cursor is (the catalog's clicks). */
function insert(text: string) {
  if (!editor || props.readOnly) return
  const sel = editor.getSelection()
  if (!sel) return
  editor.executeEdits('catalog', [{ range: sel, text, forceMoveMarkers: true }])
  editor.focus()
}

defineExpose({ runText, insert, focus: () => editor?.focus() })

function registerCompletions(m: MonacoApi) {
  disposables.push(m.languages.registerCompletionItemProvider('sql', {
    triggerCharacters: ['"', '.', ' '],
    provideCompletionItems(model, position) {
      const word = model.getWordUntilPosition(position)
      const line = model.getLineContent(position.lineNumber)
      // an opening quote typed already is replaced too: the name brings its own quotes
      const startCol = line[word.startColumn - 2] === '"' ? word.startColumn - 1 : word.startColumn
      const range = new m.Range(position.lineNumber, startCol, position.lineNumber, word.endColumn)
      const all = props.catalog
      const tables = all.filter((d) => d.has_data).map((d) => ({
        label: sqlName(d, all), kind: m.languages.CompletionItemKind.Struct,
        detail: d.path, insertText: sqlName(d, all), range, sortText: `0${d.name.toLowerCase()}`,
      }))
      // the columns of the datasources the SQL already names, then of all the others
      const text = model.getValue().toLowerCase()
      const named = new Set(all.filter((d) => text.includes(d.name.toLowerCase())).map((d) => d.id))
      const seen = new Map<string, { names: string[]; near: boolean }>()
      for (const d of all) {
        for (const c of d.columns) {
          const item = seen.get(c.name) ?? { names: [], near: false }
          item.names.push(d.name)
          item.near ||= named.has(d.id)
          seen.set(c.name, item)
        }
      }
      const columns = [...seen.entries()].map(([name, v]) => {
        const quoted = /^[A-Za-z_][A-Za-z0-9_]*$/.test(name) ? name : `"${name.replace(/"/g, '""')}"`
        return {
          label: quoted, kind: m.languages.CompletionItemKind.Field, detail: v.names.slice(0, 3).join(', '),
          insertText: quoted, range, sortText: `${v.near ? 1 : 2}${name.toLowerCase()}`,
        }
      })
      return { suggestions: [...tables, ...columns].slice(0, 3000) }
    },
  }))
}

onMounted(async () => {
  try {
    const m = await loadMonaco()
    monaco = m
    loading.value = false
    if (!host.value) return
    editor = m.editor.create(host.value, {
      value: props.modelValue,
      language: 'sql',
      automaticLayout: true,
      fontSize: 13,
      minimap: { enabled: false },
      scrollBeyondLastLine: false,
      readOnly: !!props.readOnly,
      theme: theme.value === 'light' ? 'vs' : 'vs-dark',
      tabSize: 2,
      insertSpaces: true,
      wordWrap: 'on',
      quickSuggestions: { other: true, comments: false, strings: true },
      ariaLabel: t('sqlScript.editorLabel'),
    })
    disposables.push(editor.onDidChangeModelContent(() => {
      const v = editor!.getValue()
      if (v !== props.modelValue) emit('update:modelValue', v)
    }))
    editor.addCommand(m.KeyMod.CtrlCmd | m.KeyCode.Enter, () => emit('run', runText()))
    registerCompletions(m)
  } catch {
    loading.value = false
    loadError.value = t('sqlScript.editorFailed')
  }
})

// a version looked at, a restore, a new script: the text comes from outside
watch(() => props.modelValue, (v) => {
  if (editor && v !== editor.getValue()) editor.setValue(v)
})
watch(() => props.readOnly, (ro) => editor?.updateOptions({ readOnly: !!ro }))
watch(theme, (th) => monaco?.editor.setTheme(th === 'light' ? 'vs' : 'vs-dark'))

onBeforeUnmount(() => {
  for (const d of disposables) d.dispose()
  editor?.dispose()
})
</script>

<template>
  <div class="sqled">
    <p v-if="loadError" class="err" role="alert">{{ loadError }}</p>
    <p v-else-if="loading" class="muted small">{{ $t('sqlScript.editorLoading') }}</p>
    <div ref="host" class="host" :class="{ hidden: loading || loadError }" />
  </div>
</template>

<style scoped>
.sqled { position: relative; height: 100%; min-height: 0; display: flex; flex-direction: column; }
.host { flex: 1; min-height: 0; border: 1px solid var(--border-soft); border-radius: 8px; overflow: hidden; }
.host.hidden { visibility: hidden; }
.err { color: var(--danger); margin: 0; }
.small { font-size: 12px; }
</style>

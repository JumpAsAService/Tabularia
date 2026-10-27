<script setup lang="ts">
// The SQL editor of a SQL script: Monaco with SQL highlighting, the completion of the
// tables the SQL can read (the datasources of the catalog, or a connection's tables: their
// names as the SQL writes them) and of their columns, and Ctrl/Cmd+Enter to run — the
// selection when there is one, else everything.
// Monaco is loaded the first time an editor opens, never with the page.
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type * as Monaco from 'monaco-editor'
import { loadMonaco, type MonacoApi } from '~/composables/useMonaco'
import { useTheme } from '~/composables/useTheme'
import type { SqlTable } from '~/composables/useSqlScripts'

const props = defineProps<{ modelValue: string; tables: SqlTable[]; readOnly?: boolean }>()
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

// the qualified name being typed at the cursor: parts bare or quoted, joined by dots, the
// last one possibly open
const QUALIFIED = /(?:(?:[\w$#]+|"[^"]*"|`[^`]*`|\[[^\]]*\])\.)*(?:[\w$#]*|"[^"]*|`[^`]*|\[[^\]]*)$/

/** Whether the end of `before` is inside a `--` comment or a '…' text of its line. */
function inCommentOrText(before: string): boolean {
  let quote = ''
  for (let i = 0; i < before.length; i++) {
    const c = before[i]
    if (quote) {
      if (c === quote) quote = ''
    } else if (c === "'" || c === '"' || c === '`') {
      quote = c
    } else if (c === '-' && before[i + 1] === '-') {
      return true
    }
  }
  return quote === "'"
}

function registerCompletions(m: MonacoApi) {
  disposables.push(m.languages.registerCompletionItemProvider('sql', {
    triggerCharacters: ['"', '`', '[', '.', ' '],
    provideCompletionItems(model, position) {
      const word = model.getWordUntilPosition(position)
      const line = model.getLineContent(position.lineNumber)
      // in a comment or a text nothing is proposed: Enter would put a name in place of a word
      if (inCommentOrText(line.slice(0, position.column - 1))) return { suggestions: [] }
      // an opening quote typed already is replaced too: the name brings its own quotes
      const startCol = '"`['.includes(line[word.startColumn - 2] ?? 'x') ? word.startColumn - 1 : word.startColumn
      const range = new m.Range(position.lineNumber, startCol, position.lineNumber, word.endColumn)
      // a table's name replaces the whole qualified name typed so far (`catalogo.list`,
      // `"Vendite"."Cl`), not only its last word
      const typed = QUALIFIED.exec(line.slice(0, position.column - 1))?.[0] ?? ''
      const tableRange = new m.Range(position.lineNumber, Math.min(startCol, position.column - typed.length),
                                     position.lineNumber, word.endColumn)
      const all = props.tables
      const tables = all.map((d) => ({
        label: d.insert, kind: m.languages.CompletionItemKind.Struct,
        detail: d.detail, insertText: d.insert, range: tableRange, sortText: `0${d.name.toLowerCase()}`,
      }))
      // the columns of the tables the SQL already names, then of all the others
      const text = model.getValue().toLowerCase()
      const seen = new Map<string, { insert: string; names: string[]; near: boolean }>()
      for (const d of all) {
        const near = text.includes(d.name.toLowerCase())
        for (const c of d.columns) {
          const item = seen.get(c.name) ?? { insert: c.insert, names: [], near: false }
          item.names.push(d.name)
          item.near ||= near
          seen.set(c.name, item)
        }
      }
      const columns = [...seen.entries()].map(([name, v]) => ({
        label: v.insert, kind: m.languages.CompletionItemKind.Field, detail: v.names.slice(0, 3).join(', '),
        insertText: v.insert, range, sortText: `${v.near ? 1 : 2}${name.toLowerCase()}`,
      }))
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

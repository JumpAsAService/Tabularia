// Monaco, the editor of VS Code (MIT), for the Script nodes' code. Loaded the first
// time a code editor opens, never with the page: it weighs a few MB. Everything is
// served by the frontend itself (no CDN), so it works on a network without internet.
//
// Only what the scripts need: the editor's features (suggestions, find, multiple
// cursors…) and the highlighting of Python, R and the files that sit next to them.
// The language services of TypeScript, CSS, HTML and JSON are left out.
import type * as Monaco from 'monaco-editor'

export type MonacoApi = typeof Monaco

let loading: Promise<MonacoApi> | null = null

export function loadMonaco(): Promise<MonacoApi> {
  if (!loading) {
    loading = (async () => {
      // Vite's worker import: a file of its own under /_nuxt
      const { default: EditorWorker } = await import('monaco-editor/esm/vs/editor/editor.worker?worker')
      ;(self as any).MonacoEnvironment = { getWorker: () => new EditorWorker() }
      // the editor's API without the languages it ships with (the types are the package's)
      // @ts-expect-error — the subpath has no types of its own
      const monaco = (await import('monaco-editor/esm/vs/editor/editor.api')) as unknown as MonacoApi
      await Promise.all([
        // @ts-expect-error — no types for the side-effect modules
        import('monaco-editor/esm/vs/editor/editor.all.js'),
        // @ts-expect-error
        import('monaco-editor/esm/vs/basic-languages/python/python.contribution'),
        // @ts-expect-error
        import('monaco-editor/esm/vs/basic-languages/r/r.contribution'),
        // @ts-expect-error
        import('monaco-editor/esm/vs/basic-languages/sql/sql.contribution'),
        // @ts-expect-error
        import('monaco-editor/esm/vs/basic-languages/yaml/yaml.contribution'),
        // @ts-expect-error
        import('monaco-editor/esm/vs/basic-languages/markdown/markdown.contribution'),
        // @ts-expect-error
        import('monaco-editor/esm/vs/basic-languages/shell/shell.contribution'),
      ])
      return monaco
    })()
    loading.catch(() => { loading = null }) // a failed load is tried again next time
  }
  return loading
}

export function languageOf(path: string): string {
  const ext = path.split('.').pop()?.toLowerCase() ?? ''
  return ({ py: 'python', r: 'r', sql: 'sql', yaml: 'yaml', yml: 'yaml', md: 'markdown', sh: 'shell' } as Record<string, string>)[ext]
    ?? 'plaintext'
}

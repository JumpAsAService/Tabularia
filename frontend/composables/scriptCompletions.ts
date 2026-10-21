// What the Script node's code editor offers while typing. No language server: what
// the environment holds was listed once, when its image was checked (the public
// names of every module, `completions`), and the rest is read from the code itself.
//
// Python
//   os.environ["…  os.environ.get("…  os.getenv("…   the variables the script will have
//   import …  from …                                  the installed modules, and the node's own
//   alias.…                                           the names of the module behind `alias`
//                                                     (import x as alias / import x / a file of the node)
//   from x import …                                   the names of x
// R
//   Sys.getenv("…                                     the variables
//   library(…  require(…                              the installed packages
//   pkg::…                                            what pkg exports
//   a name                                            what the packages loaded with library() export

export interface Suggestion {
  label: string
  kind: 'variable' | 'module' | 'member' | 'function'
  detail?: string
}

export interface CompletionContext {
  language: 'python' | 'r'
  variables: { name: string; detail: string }[]
  completions: Record<string, string[]>
  // the node's files: their modules and their top-level names
  files: { path: string; content: string }[]
}

const PY_ENV = /(?:os\.environ\[|os\.environ\.get\(|os\.getenv\()\s*["']([A-Za-z0-9_]*)$/
const PY_IMPORT = /^\s*(?:import|from)\s+([\w.]*)$/
const PY_FROM_IMPORT = /^\s*from\s+([\w.]+)\s+import\s+(?:[\w\s,]*,\s*)?(\w*)$/
const PY_MEMBER = /([A-Za-z_]\w*)\.(\w*)$/
const R_ENV = /Sys\.getenv\(\s*["']([A-Za-z0-9_]*)$/
const R_LIBRARY = /(?:library|require|requireNamespace)\(\s*["']?([\w.]*)$/
const R_NAMESPACE = /([A-Za-z][\w.]*):::?([\w.]*)$/

/** A file of the node as a Python module name: lib/scoring.py → lib.scoring. */
export function moduleOf(path: string): string | null {
  if (!path.endsWith('.py')) return null
  const parts = path.slice(0, -3).split('/')
  if (parts[parts.length - 1] === '__init__') parts.pop()
  return parts.every((p) => /^[A-Za-z_]\w*$/.test(p)) && parts.length ? parts.join('.') : null
}

/** The top-level names a Python file defines: def, class and assignments at column 0. */
export function topLevelNames(content: string): string[] {
  const names = new Set<string>()
  for (const m of content.matchAll(/^(?:async\s+def|def|class)\s+([A-Za-z_]\w*)|^([A-Za-z_]\w*)\s*(?::[^=\n]*)?=(?!=)/gm)) {
    const n = m[1] ?? m[2]
    if (n && !n.startsWith('_')) names.add(n)
  }
  return [...names].sort()
}

/** What each name stands for in the code: `import polars as pl` → pl → polars. */
export function pythonAliases(code: string): Map<string, string> {
  const out = new Map<string, string>()
  for (const m of code.matchAll(/^\s*import\s+(.+)$/gm)) {
    for (const part of m[1]!.split(',')) {
      const am = /^\s*([\w.]+)(?:\s+as\s+(\w+))?\s*$/.exec(part)
      if (!am) continue
      const mod = am[1]!
      out.set(am[2] ?? mod.split('.')[0]!, am[2] ? mod : mod.split('.')[0]!)
    }
  }
  for (const m of code.matchAll(/^\s*from\s+([\w.]+)\s+import\s+([\w\s,]+?)\s*$/gm)) {
    for (const part of m[2]!.split(',')) {
      const am = /^\s*(\w+)(?:\s+as\s+(\w+))?\s*$/.exec(part)
      if (am) out.set(am[2] ?? am[1]!, `${m[1]}.${am[1]}`)
    }
  }
  return out
}

function localModules(ctx: CompletionContext): Map<string, string[]> {
  const out = new Map<string, string[]>()
  for (const f of ctx.files) {
    const mod = moduleOf(f.path)
    if (mod) out.set(mod, topLevelNames(f.content))
  }
  return out
}

function namesOf(module: string, ctx: CompletionContext, local: Map<string, string[]>): string[] {
  // only what is known for that very module: another's names would mislead
  return local.get(module) ?? ctx.completions[module] ?? []
}

function variables(ctx: CompletionContext, prefix: string): Suggestion[] {
  return ctx.variables.filter((v) => v.name.startsWith(prefix.toUpperCase()) || !prefix)
    .map((v) => ({ label: v.name, kind: 'variable', detail: v.detail }))
}

/** The suggestions for the text of the line before the cursor; `code` is the whole file. */
export function suggest(ctx: CompletionContext, before: string, code: string): Suggestion[] {
  if (ctx.language === 'python') {
    let m = PY_ENV.exec(before)
    if (m) return variables(ctx, m[1]!)
    const local = localModules(ctx)
    m = PY_FROM_IMPORT.exec(before)
    if (m) return namesOf(m[1]!, ctx, local).map((n) => ({ label: n, kind: 'member', detail: m![1] }))
    m = PY_IMPORT.exec(before)
    if (m) {
      const modules = new Set([...Object.keys(ctx.completions), ...local.keys()])
      return [...modules].sort().map((n) => ({ label: n, kind: 'module', detail: local.has(n) ? 'this script' : undefined }))
    }
    m = PY_MEMBER.exec(before)
    if (m) {
      const target = pythonAliases(code).get(m[1]!)
      if (!target) return []
      const names = namesOf(target, ctx, local)
      return names.map((n) => ({ label: n, kind: 'member', detail: target }))
    }
    return []
  }
  let m = R_ENV.exec(before)
  if (m) return variables(ctx, m[1]!)
  m = R_NAMESPACE.exec(before)
  if (m) return (ctx.completions[m[1]!] ?? []).map((n) => ({ label: n, kind: 'function', detail: m![1] }))
  m = R_LIBRARY.exec(before)
  if (m) return Object.keys(ctx.completions).sort().map((n) => ({ label: n, kind: 'module' }))
  // a plain name: what the packages loaded in this file export
  if (/[A-Za-z.][\w.]*$/.test(before)) {
    const loaded = [...code.matchAll(/(?:library|require)\(\s*["']?([\w.]+)/g)].map((x) => x[1]!)
    const out: Suggestion[] = []
    for (const pkg of loaded) for (const n of ctx.completions[pkg] ?? []) out.push({ label: n, kind: 'function', detail: pkg })
    return out
  }
  return []
}

/** How a variable is read in the script's language. */
export function variableReference(language: 'python' | 'r', name: string): string {
  return language === 'python' ? `os.environ["${name}"]` : `Sys.getenv("${name}")`
}

// Where a failed run of a Script node stopped, read from its log, so the code editor
// can mark that line of that file.
//
// Python: the last frame of the traceback that is one of the node's files
//   File "/tabularia/code/lib/utils.py", line 4, in read_input
// (runs before the `python -P` of the Job show Kubernetes' dated folder in the path:
// /tabularia/code/..2026_10_21_08_36_26.3942651677/utils.py), and the exception's
// line at the end. R: the parse error of a file read with source()
//   Error in source("R/utils.R") : R/utils.R:4:5: unexpected symbol
// (Rscript gives no line for the other errors).

export interface RunError {
  path: string
  line: number
  message: string
}

const CODE_DIR = /^\/tabularia\/code\/(?:\.\.[^/]+\/)?(.+)$/
const PY_FRAME = /^\s*File "([^"]+)", line (\d+)/
const R_PARSE = /([A-Za-z0-9_./-]+\.[Rr]):(\d+):\d+: (.+)$/

function lastLine(lines: string[]): string {
  for (let i = lines.length - 1; i >= 0; i--) {
    const l = lines[i]!.trim()
    if (l) return l
  }
  return ''
}

export function runError(language: 'python' | 'r', log: string, paths: string[]): RunError | null {
  if (!log) return null
  const lines = log.split('\n')
  const known = new Set(paths)
  if (language === 'python') {
    let found: RunError | null = null
    for (const l of lines) {
      const frame = PY_FRAME.exec(l)
      if (!frame) continue
      const path = CODE_DIR.exec(frame[1]!)?.[1] ?? frame[1]!
      if (known.has(path)) found = { path, line: Number(frame[2]), message: '' }
    }
    if (found) found.message = lastLine(lines)
    return found
  }
  for (let i = lines.length - 1; i >= 0; i--) {
    const m = R_PARSE.exec(lines[i]!)
    if (m && known.has(m[1]!)) return { path: m[1]!, line: Number(m[2]), message: m[3]!.trim() }
  }
  return null
}

// The differences between two texts, line by line: what a proposal of the script assistant would
// change in a file, shown before it is applied (components/scripting/ScriptAssistant.vue). A true
// difference, in order: a line moved is a line removed and a line added, and a line repeated counts
// each time. Kept apart from the page so a test can run it.

export interface DiffLine {
  kind: 'same' | 'added' | 'removed' | 'gap'
  text: string
  /** the line's number in the text before (removed, same) and after (added, same); a gap has neither */
  before?: number
  after?: number
}

export interface LineDiff {
  added: number
  removed: number
  /** every line of both texts, in reading order */
  lines: DiffLine[]
}

// Above this many cells the table of the longest common run of lines is not built: what lies between
// the unchanged start and end of the two texts is then given as removed and added whole. A file of
// the editor is 256 KB at most, some thousands of lines: only a rewrite from top to bottom gets here.
const MAX_CELLS = 4_000_000

function split(text: string): string[] {
  return text === '' ? [] : text.replace(/\r\n/g, '\n').split('\n')
}

export function lineDiff(before: string, after: string): LineDiff {
  const a = split(before)
  const b = split(after)
  let start = 0
  while (start < a.length && start < b.length && a[start] === b[start]) start++
  let endA = a.length
  let endB = b.length
  while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) { endA--; endB-- }

  const lines: DiffLine[] = []
  for (let i = 0; i < start; i++) lines.push({ kind: 'same', text: a[i]!, before: i + 1, after: i + 1 })

  const n = endA - start
  const m = endB - start
  if (n * m > MAX_CELLS) {
    for (let i = start; i < endA; i++) lines.push({ kind: 'removed', text: a[i]!, before: i + 1 })
    for (let j = start; j < endB; j++) lines.push({ kind: 'added', text: b[j]!, after: j + 1 })
  } else if (n || m) {
    // the longest common run of lines of the two middles, from the end backwards
    const width = m + 1
    const table = new Uint32Array((n + 1) * width)
    for (let i = n - 1; i >= 0; i--) {
      for (let j = m - 1; j >= 0; j--) {
        table[i * width + j] = a[start + i] === b[start + j]
          ? table[(i + 1) * width + j + 1]! + 1
          : Math.max(table[(i + 1) * width + j]!, table[i * width + j + 1]!)
      }
    }
    let i = 0
    let j = 0
    while (i < n || j < m) {
      if (i < n && j < m && a[start + i] === b[start + j]) {
        lines.push({ kind: 'same', text: a[start + i]!, before: start + i + 1, after: start + j + 1 })
        i++; j++
      } else if (i < n && (j === m || table[(i + 1) * width + j]! >= table[i * width + j + 1]!)) {
        lines.push({ kind: 'removed', text: a[start + i]!, before: start + i + 1 })
        i++
      } else {
        lines.push({ kind: 'added', text: b[start + j]!, after: start + j + 1 })
        j++
      }
    }
  }
  for (let k = 0; endA + k < a.length; k++) {
    lines.push({ kind: 'same', text: a[endA + k]!, before: endA + k + 1, after: endB + k + 1 })
  }
  return {
    added: lines.filter((l) => l.kind === 'added').length,
    removed: lines.filter((l) => l.kind === 'removed').length,
    lines,
  }
}

/**
 * The changed lines of a difference with `context` unchanged lines around each run of them, and a
 * `gap` where unchanged lines were left out (its text is how many).
 */
export function diffHunks(diff: LineDiff, context = 2): DiffLine[] {
  const { lines } = diff
  const keep = new Array<boolean>(lines.length).fill(false)
  lines.forEach((l, i) => {
    if (l.kind === 'same') return
    for (let k = Math.max(0, i - context); k <= Math.min(lines.length - 1, i + context); k++) keep[k] = true
  })
  const out: DiffLine[] = []
  let skipped = 0
  lines.forEach((l, i) => {
    if (!keep[i]) { skipped++; return }
    if (skipped) { out.push({ kind: 'gap', text: String(skipped) }); skipped = 0 }
    out.push(l)
  })
  if (skipped && out.length) out.push({ kind: 'gap', text: String(skipped) })
  return out
}

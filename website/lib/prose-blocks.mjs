// Build-time touches on the docs' parsed Markdown (Nuxt Content's minimark tree), for the
// site only: the Markdown in docs/ stays plain, as GitHub shows it.
//
// - pageTocAfterTitle: an <page-toc> right after the page's title, the "On this page" list
//   for narrow screens (components/content/PageToc.vue).
// - stackTables: every cell learns its column's name (data-label), so on a phone each row is
//   shown as a block of "name: value" lines; a table too wide for the text column (five
//   columns or more, or long unbreakable values) is shown that way at every width: its
//   first column as the row's title, the short columns on one line, a long last column
//   (a description) below, full width. Table roles are written out, so the CSS layout keeps
//   the table's meaning for screen readers.
// - markChecks: a paragraph that starts with **Check:** (how to see that a step worked)
//   gets class "check", drawn as a box of its own.
// - markZoomableImages: the screenshots (images under media/ that are not a link already)
//   can be enlarged (components/content/ProseImg.vue).
// - markCallouts: GitHub's alerts ("> [!NOTE]", TIP, IMPORTANT, WARNING, CAUTION, which
//   GitHub draws itself) become callouts of three kinds, note (NOTE, TIP), important and
//   warning (WARNING, CAUTION), with their title; a plain quote stays a quote.

const isEl = (n, tag) => Array.isArray(n) && (tag ? n[0] === tag : typeof n[0] === 'string')

/** Puts <page-toc> after the first h1 of the page (nothing when there is no h1). */
export function pageTocAfterTitle (nodes) {
  const i = nodes.findIndex(n => isEl(n, 'h1'))
  if (i < 0 || nodes.some(n => isEl(n, 'page-toc'))) return nodes
  return [...nodes.slice(0, i + 1), ['page-toc', {}], ...nodes.slice(i + 1)]
}

const textOf = n => (typeof n === 'string' ? n : Array.isArray(n) ? n.slice(2).map(textOf).join('') : '')
const kids = n => n.slice(2).filter(c => Array.isArray(c))
/** The text column holds about 76 characters of prose (max-width: 76ch, 17px). */
const COLUMN_PX = 760
const longestWord = t => Math.max(0, ...t.split(/\s+/).map(w => w.length))

/** Marks one table (a minimark node), in place. Exported for the tests. */
export function stackTable (table) {
  const thead = kids(table).find(n => n[0] === 'thead')
  const tbody = kids(table).find(n => n[0] === 'tbody')
  const headRow = thead && kids(thead).find(n => n[0] === 'tr')
  const labels = headRow ? kids(headRow).map(c => textOf(c).trim()) : []
  const rows = tbody ? kids(tbody).filter(n => n[0] === 'tr') : []
  const cols = Math.max(labels.length, ...rows.map(r => kids(r).length), 0)
  // the width the table needs at least: per column its longest word (code does not wrap
  // inside a word), about 8.5px a character, plus the cells' padding
  const need = Array.from({ length: cols }, (_, i) =>
    Math.max(longestWord(labels[i] || ''), ...rows.map(r => longestWord(textOf(kids(r)[i] || '')))))
  const wide = cols >= 5 || need.reduce((a, w) => a + w * 8.5 + 24, 0) > COLUMN_PX
  // a long last column (a description) goes below the row in the stacked layout
  const avg = i => rows.reduce((a, r) => a + textOf(kids(r)[i] || '').length, 0) / Math.max(rows.length, 1)
  const middle = Array.from({ length: Math.max(cols - 2, 0) }, (_, k) => avg(k + 1))
  const bodyLast = cols >= 3 && avg(cols - 1) > 40 && avg(cols - 1) > 2 * Math.max(0, ...middle)

  table[1] = { ...table[1], ...(wide ? { class: 'is-stacked' } : {}) }
  for (const group of [thead, tbody].filter(Boolean)) {
    group[1] = { ...group[1], role: 'rowgroup' }
    for (const tr of kids(group).filter(n => n[0] === 'tr')) {
      tr[1] = { ...tr[1], role: 'row' }
      kids(tr).forEach((cell, i) => {
        const role = cell[0] === 'th' ? 'columnheader' : 'cell'
        const place = i === 0 ? 'title' : bodyLast && i === cols - 1 ? 'body' : 'meta'
        cell[1] = { ...cell[1], role, ...(cell[0] === 'td' ? { 'data-label': labels[i] || '', 'data-place': place } : {}) }
      })
    }
  }
  return table
}

/** Marks every table of a list of nodes, in place. */
export function stackTables (nodes) {
  for (const n of nodes) {
    if (!Array.isArray(n)) continue
    if (n[0] === 'table') stackTable(n)
    else stackTables(n.slice(2))
  }
  return nodes
}

/** Gives class "check" to the paragraphs that start with **Check:**, at any depth. */
export function markChecks (nodes) {
  for (const n of nodes) {
    if (!Array.isArray(n)) continue
    const first = n[0] === 'p' ? n.slice(2).find(c => !(typeof c === 'string' && !c.trim())) : null
    if (first && isEl(first, 'strong') && textOf(first).trim() === 'Check:') {
      n[1] = { ...n[1], class: [n[1]?.class, 'check'].filter(Boolean).join(' ') }
    } else markChecks(n.slice(2))
  }
  return nodes
}

/** Marks the screenshots that can be enlarged: images under media/, not inside a link. */
export function markZoomableImages (nodes, inLink = false) {
  for (const n of nodes) {
    if (!Array.isArray(n)) continue
    if (n[0] === 'img' && !inLink && /(^|\/)media\//.test(String(n[1]?.src || ''))) n[1] = { ...n[1], zoom: 'true' }
    else markZoomableImages(n.slice(2), inLink || n[0] === 'a')
  }
  return nodes
}

const ALERTS = { NOTE: ['note', 'Note'], TIP: ['note', 'Tip'], IMPORTANT: ['important', 'Important'], WARNING: ['warning', 'Warning'], CAUTION: ['warning', 'Caution'] }
const ALERT = /^\s*\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\][ \t]*\n?/

/** Turns the blockquotes that start with a GitHub alert marker into callouts, in place. */
export function markCallouts (nodes) {
  for (const n of nodes) {
    if (!Array.isArray(n)) continue
    if (n[0] === 'blockquote') {
      const pi = n.findIndex((c, i) => i >= 2 && isEl(c, 'p'))
      const para = pi >= 0 ? n[pi] : null
      // the first thing of the paragraph: "[!NOTE]" as text, or as the span MDC makes of it
      const ti = para ? para.findIndex((c, i) => i >= 2 && !(typeof c === 'string' && !c.trim())) : -1
      const first = ti >= 0 ? para[ti] : null
      const m = typeof first === 'string'
        ? ALERT.exec(first)
        : isEl(first, 'span') && /^!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)$/.exec(textOf(first).trim())
          ? [textOf(first), textOf(first).trim().slice(1)] : null
      if (m) {
        const [kind, title] = ALERTS[m[1]]
        const rest = typeof first === 'string' ? first.slice(m[0].length) : ''
        if (rest.trim()) para[ti] = rest
        else para.splice(ti, 1)
        // what follows the marker starts on the next line
        if (typeof para[ti] === 'string') {
          para[ti] = para[ti].replace(/^\s*\n/, '')
          if (!para[ti]) para.splice(ti, 1)
        }
        // the marker alone on its line leaves an empty paragraph behind: drop it
        if (para.length <= 2) n.splice(pi, 1)
        n[1] = { ...n[1], class: `callout callout--${kind}` }
        n.splice(2, 0, ['p', { class: 'callout__title' }, title])
      }
    }
    markCallouts(n.slice(2))
  }
  return nodes
}

/** Nuxt Content hook helper: every touch above, in place. */
export function enhanceProse (content) {
  const body = content?.body
  if (!body || body.type !== 'minimark' || !Array.isArray(body.value)) return
  body.value = pageTocAfterTitle(body.value)
  stackTables(body.value)
  markChecks(body.value)
  markZoomableImages(body.value)
  markCallouts(body.value)
}

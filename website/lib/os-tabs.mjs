// Operating-system tabs for the docs.
//
// In docs/*.md a step that differs between systems is written as consecutive blocks, each
// under a fixed level-4 heading, always in this order:
//
//   #### Windows          #### Windows
//   …                     …
//   #### Linux            #### Linux and macOS
//   …                     …
//   #### macOS
//   …
//
// On GitHub they stay plain headings. For the site, groupOsSections() wraps each run of
// two or more of them in <os-tabs><os-panel>…</os-panel>…</os-tabs> (components/content),
// which shows them as tabs once JavaScript runs. A block ends at the next heading of level
// 1 to 4, so text common to every system after a group goes under a heading of its own:
// "#### All systems". Neither kind of heading is listed in the page's table of contents.

/** Heading text → panel key. */
export const OS_HEADINGS = {
  'Windows': 'windows',
  'Linux': 'linux',
  'macOS': 'macos',
  'Linux and macOS': 'linux-macos',
}

/** Panel key → the systems it covers (the visitor's choice is one system). */
export const OS_SYSTEMS = {
  'windows': ['windows'],
  'linux': ['linux'],
  'macos': ['macos'],
  'linux-macos': ['linux', 'macos'],
}

const textOf = n => (typeof n === 'string' ? n : Array.isArray(n) ? n.slice(2).map(textOf).join('') : '')
const headingLevel = n => (Array.isArray(n) && /^h[1-6]$/.test(n[0]) ? Number(n[0][1]) : 0)
const osKey = n => (Array.isArray(n) && n[0] === 'h4' ? OS_HEADINGS[textOf(n).trim()] || null : null)

/** Groups the system headings of a list of minimark nodes; returns a new list. */
export function groupOsNodes (nodes) {
  const out = []
  let i = 0
  while (i < nodes.length) {
    if (!osKey(nodes[i])) {
      const n = nodes[i]
      // the same inside containers (a blockquote, a list item): rare, but cheap
      out.push(Array.isArray(n) && n.length > 2 ? [n[0], n[1], ...groupOsNodes(n.slice(2))] : n)
      i++
      continue
    }
    const panels = []
    const seen = new Set()
    let j = i
    while (j < nodes.length) {
      const key = osKey(nodes[j])
      if (!key || seen.has(key)) break
      seen.add(key)
      let k = j + 1
      while (k < nodes.length) {
        const lv = headingLevel(nodes[k])
        if (lv && lv <= 4) break
        k++
      }
      panels.push({ key, nodes: nodes.slice(j, k) })
      j = k
    }
    if (panels.length < 2) {
      out.push(...nodes.slice(i, j))
    } else {
      out.push(['os-tabs', { systems: panels.map(p => p.key).join(' ') },
        ...panels.map(p => ['os-panel', { os: p.key, label: textOf(p.nodes[0]).trim() }, ...p.nodes])])
    }
    i = j
  }
  return out
}

/** The heading that brings the text back to every system after a group. */
export const ALL_SYSTEMS = 'All systems'

/** Drops the system headings, and "All systems", from a page's table of contents. */
export function pruneOsToc (links) {
  const drop = new Set([...Object.keys(OS_HEADINGS), ALL_SYSTEMS])
  return (links || []).filter(l => !drop.has(String(l.text || '').trim()))
    .map(l => (l.children ? { ...l, children: pruneOsToc(l.children) } : l))
}

/** Nuxt Content hook helper: groups the system headings of a parsed page in place. */
export function groupOsSections (content) {
  const body = content?.body
  if (!body || body.type !== 'minimark' || !Array.isArray(body.value)) return
  body.value = groupOsNodes(body.value)
  if (body.toc?.links) body.toc.links = pruneOsToc(body.toc.links)
}

// The keys that work on every page of the app (components/ui/AppKeys.vue listens, the top
// bar and the list of keys read the same tables): the places of the top bar with the
// letter that leads to each, what a key means outside a text field, how a key is written
// on a Mac and elsewhere, and the walk along a row of tabs. Pure functions, no Nuxt: the
// tests import them as they are (tests/app-keys.test.mjs).

// ── The places of the top bar ────────────────────────────────────────────────

export interface NavSection {
  id: string
  to: string
  /** i18n key of its name */
  label: string
  /** the letter after «g»: taken from the ENGLISH name, the same in every language */
  key: string
  /** the item of the bar that holds it, when it sits in a menu */
  menu?: 'analysis' | 'admin'
  /** what its person must have to see it */
  needs?: 'scripting' | 'observe'
}

/** In the bar's order. A menu stands where its first place stands. */
export const NAV_SECTIONS: readonly NavSection[] = [
  { id: 'explore', to: '/', label: 'nav.explore', key: 'e' },
  { id: 'flows', to: '/flows', label: 'nav.flows', key: 'f' },
  { id: 'datasources', to: '/datasources', label: 'nav.datasources', key: 'd' },
  { id: 'lineage', to: '/lineage', label: 'nav.lineage', key: 'l' },
  { id: 'viewer', to: '/viewer', label: 'nav.viewer', key: 'v', menu: 'analysis' },
  { id: 'reports', to: '/reports', label: 'nav.reports', key: 'p', menu: 'analysis' },
  { id: 'dashboards', to: '/dashboards', label: 'nav.dashboards', key: 'b', menu: 'analysis' },
  { id: 'assistant', to: '/chat', label: 'nav.assistant', key: 'a' },
  { id: 'connections', to: '/connections', label: 'nav.connections', key: 'c' },
  // only to whoever may use a scripting environment or a container registry
  { id: 'scripting', to: '/scripting', label: 'nav.scripting', key: 's', needs: 'scripting' },
  { id: 'runs', to: '/runs', label: 'nav.runs', key: 'r' },
  // Administration: for an administrator or an observer
  { id: 'admin', to: '/admin', label: 'nav.admin', key: 'm', menu: 'admin', needs: 'observe' },
  { id: 'queue', to: '/queue', label: 'nav.queue', key: 'q', menu: 'admin', needs: 'observe' },
  { id: 'monitoring', to: '/monitoring', label: 'nav.monitoring', key: 'o', menu: 'admin', needs: 'observe' },
  { id: 'performance', to: '/performance', label: 'nav.performance', key: 'n', menu: 'admin', needs: 'observe' },
  { id: 'cluster', to: '/cluster', label: 'nav.cluster', key: 't', menu: 'admin', needs: 'observe' },
  { id: 'audit', to: '/audit', label: 'nav.audit', key: 'u', menu: 'admin', needs: 'observe' },
]

/** The places this person sees in the bar: the only ones «g» leads to. */
export function visibleSections(can: { scripting: boolean; observe: boolean }): NavSection[] {
  return NAV_SECTIONS.filter((s) => !s.needs || can[s.needs])
}

// ── A key outside the text fields ────────────────────────────────────────────

/** How long «g» waits for its letter (ms). */
export const SEQUENCE_MS = 1500

export interface KeyFacts {
  key: string
  ctrlKey?: boolean
  metaKey?: boolean
  altKey?: boolean
  repeat?: boolean
}

export type AppKeyAct =
  | { act: 'pending' }                 // «g»: the letter is awaited
  | { act: 'goto'; to: string }        // «g» then a place's letter
  | { act: 'swallow' }                 // «g» then a character that is no place: nobody takes it
  | { act: 'tab'; dir: 1 | -1 }        // «,» «.»: the page's row of sub-tabs
  | { act: 'help' }                    // «?»
  | { act: 'search' }                  // «/»
  | { act: null }

const ALONE = new Set(['Shift', 'Control', 'Alt', 'Meta', 'CapsLock', 'AltGraph', 'Dead'])

/**
 * What a key does, away from a text field and with nothing open. `pendingSince` is when «g»
 * was pressed (null: it was not); the answer says whether «g» still waits (`pending`).
 * Symbols are read from `key`, never from the physical key: «?» is Shift+' on an Italian
 * keyboard, «.» is shifted on a French one. Nothing with Ctrl, ⌘ or Alt: those are the
 * browser's and the system's.
 */
export function appKey(
  k: KeyFacts, pendingSince: number | null, now: number, sections: readonly NavSection[],
): AppKeyAct & { pending: boolean } {
  const waiting = pendingSince !== null && now - pendingSince <= SEQUENCE_MS
  // a modifier going down by itself is not the letter
  if (ALONE.has(k.key)) return { act: null, pending: waiting }
  if (k.ctrlKey || k.metaKey || k.altKey) return { act: null, pending: false }
  if (waiting) {
    const place = sections.find((s) => s.key === k.key)
    if (place) return { act: 'goto', to: place.to, pending: false }
    // a character that leads nowhere is dropped, so that «g n» never reaches the page as
    // «n»; a key that is not a character (an arrow, Esc) is the page's again
    return { act: k.key.length === 1 ? 'swallow' : null, pending: false }
  }
  if (k.repeat) return { act: null, pending: false }
  switch (k.key) {
    case 'g': return { act: 'pending', pending: true }
    case ',': return { act: 'tab', dir: -1, pending: false }
    case '.': return { act: 'tab', dir: 1, pending: false }
    case '?': return { act: 'help', pending: false }
    case '/': return { act: 'search', pending: false }
  }
  return { act: null, pending: false }
}

/** Where the focus is, as far as typing goes. */
export interface FocusFacts {
  /** the element's tag, upper case */
  tag: string
  /** an <input>'s type */
  type?: string
  editable?: boolean
  /** inside a code editor (Monaco, CodeMirror, `.nokey`) or an ARIA text box */
  inEditor?: boolean
}

const NOT_TEXT = new Set(['checkbox', 'radio', 'button', 'submit', 'reset', 'range', 'color', 'file', 'image'])

/** True when a character typed now would go into a field: no key of the app acts there. */
export function keysAreTyping(f: FocusFacts): boolean {
  if (f.editable || f.inEditor) return true
  if (f.tag === 'TEXTAREA' || f.tag === 'SELECT') return true
  return f.tag === 'INPUT' && !NOT_TEXT.has((f.type ?? 'text').toLowerCase())
}

// ── A row of tabs ────────────────────────────────────────────────────────────

/** The tab after (`dir` 1) or before the chosen one, going round, past the ones that are
 *  off; -1 when there is no other to go to. */
export function stepTab(tabs: readonly { selected: boolean; disabled: boolean }[], dir: 1 | -1): number {
  const n = tabs.length
  if (!n) return -1
  const at = tabs.findIndex((x) => x.selected)
  // none chosen yet: the first going forwards, the last going back
  let i = at < 0 ? (dir > 0 ? -1 : n) : at
  for (let turns = 0; turns < n; turns++) {
    i = (i + dir + n) % n
    if (i === at) return -1
    if (!tabs[i].disabled) return i
  }
  return -1
}

/** A key on a row of tabs with the focus on tab `at`: the tab the focus goes to, or -1.
 *  ← → on a row, ↑ ↓ too on a column; Home and End where the keyboard has them. */
export function rovingKey(key: string, at: number, count: number, vertical = false): number {
  if (count <= 0) return -1
  const back = key === 'ArrowLeft' || (vertical && key === 'ArrowUp')
  const on = key === 'ArrowRight' || (vertical && key === 'ArrowDown')
  if (back) return at <= 0 ? count - 1 : at - 1
  if (on) return at < 0 || at >= count - 1 ? 0 : at + 1
  if (key === 'Home') return 0
  if (key === 'End') return count - 1
  return -1
}

// ── How a key is written ─────────────────────────────────────────────────────

/** A Mac (or an iPad with a keyboard), from what the browser says of itself. */
export function platformIsMac(nav?: { platform?: string; userAgentData?: { platform?: string } } | null): boolean {
  const n = nav ?? (typeof navigator !== 'undefined' ? (navigator as any) : null)
  return /Mac|iPhone|iPad/i.test(n?.userAgentData?.platform || n?.platform || '')
}

export interface KeyNames {
  mac: boolean
  mod: string
  alt: string
  shift: string
  enter: string
  backspace: string
  /** a chord: «Ctrl+Enter», «⌘↩» */
  chord: (...parts: string[]) => string
  /** the ways to one action: `pc` on a keyboard that has the key, `any` on every one. On
   *  a Mac only `any` (and `mac`) are shown: it has no Home, End, Del nor Menu key, and its
   *  function keys ask for Fn. */
  ways: (o: { pc?: string[]; mac?: string[]; any?: string[] }) => string
}

/** The names of the keys on this keyboard: ⌘ ⌥ ⇧ ↩ ⌫ on a Mac, words elsewhere. */
export function keyNames(mac: boolean): KeyNames {
  const sep = ' · '
  return {
    mac,
    mod: mac ? '⌘' : 'Ctrl',
    alt: mac ? '⌥' : 'Alt',
    shift: mac ? '⇧' : 'Shift',
    enter: mac ? '↩' : 'Enter',
    backspace: mac ? '⌫' : 'Backspace',
    chord: (...parts) => parts.join(mac ? '' : '+'),
    ways: (o) => [...(mac ? (o.mac ?? []) : (o.pc ?? [])), ...(o.any ?? [])].join(sep),
  }
}

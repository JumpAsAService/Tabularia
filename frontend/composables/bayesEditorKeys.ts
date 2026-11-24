// The keys of the editor of Bayesian models (docs/specs/bayes-graph-editor.md, «Keyboard»), as one
// pure function: a key event, as the browser gives it, and the keyboard in use (a Mac's or not) in,
// the action it asks for out (or null). The canvas and the page ask it and then do what the action
// says; whether the focus is in a field, a dialog is open or a node is selected is theirs to judge,
// because it is the page's state and not the key's.
//
// The same module holds the table of the keys the spec lists, with a way for each keyboard, so a test
// can run every row on both: no action may need an F key, Home, End or the Menu key, nor Alt with a
// letter, nor three modifiers (DESIGN.md «Keyboard»). Nothing of Vue, Nuxt or the DOM; every export
// starts with `bayes` or `BAYES_`, as the other composables' (they are auto-imported everywhere).
import type { BayesDirection } from '~/composables/bayesCanvasModel'

/** What a key asks of the editor. */
export type BayesKeyAction =
  | { kind: 'walk', dir: BayesDirection }
  | { kind: 'move', dir: BayesDirection }
  | { kind: 'add' }
  | { kind: 'menu' }
  | { kind: 'remove' }
  | { kind: 'duplicate' }
  | { kind: 'undo' }
  | { kind: 'redo' }
  | { kind: 'save' }
  | { kind: 'zoomIn' }
  | { kind: 'zoomOut' }
  | { kind: 'fit' }

export type BayesKeyKind = BayesKeyAction['kind']

/** The part of a `KeyboardEvent` the rules read. */
export interface BayesKeyEvent {
  key: string
  ctrlKey?: boolean
  metaKey?: boolean
  altKey?: boolean
  shiftKey?: boolean
}

const DIRS: Record<string, BayesDirection> = { ArrowLeft: 'left', ArrowRight: 'right', ArrowUp: 'up', ArrowDown: 'down' }

/** The direction an arrow key points to, or null for any other key. */
export function bayesArrowDirection (key: string): BayesDirection | null {
  return DIRS[key] ?? null
}

/** How far Shift with an arrow moves the selected node or box, in pixels, as (dx, dy). */
export function bayesMoveStep (dir: BayesDirection): [number, number] {
  return dir === 'left' ? [-10, 0] : dir === 'right' ? [10, 0] : dir === 'up' ? [0, -10] : [0, 10]
}

/**
 * The action of a key, or null when the key is none of the editor's. Control or Command alone make
 * the page's keys (undo, redo, duplicate); without them, the canvas' keys. ⌘Y is the browser's history
 * on a Mac, so redo is ⌘⇧Z there and Ctrl+Y only elsewhere. Ctrl or ⌘ with S saves (the browser's
 * «save the page» never wanted in an editor). Alt takes an arrow to walk and takes nothing else.
 */
export function bayesKeyAction (e: BayesKeyEvent, mac: boolean): BayesKeyAction | null {
  const shift = !!e.shiftKey
  if (e.ctrlKey || e.metaKey) {
    if (e.altKey) return null
    const key = e.key.toLowerCase()
    if (key === 'z') return { kind: shift ? 'redo' : 'undo' }
    if (key === 'y' && !shift && !mac) return { kind: 'redo' }
    if (key === 'd' && !shift) return { kind: 'duplicate' }
    if (key === 's' && !shift) return { kind: 'save' }
    return null
  }
  const dir = DIRS[e.key]
  if (dir) {
    if (e.altKey) return shift ? null : { kind: 'walk', dir }
    return shift ? { kind: 'move', dir } : null
  }
  if (e.altKey) return null
  if (e.key === 'Delete' || e.key === 'Backspace') return { kind: 'remove' }
  if ((e.key === 'm' && !shift) || e.key === 'ContextMenu' || (e.key === 'F10' && shift)) return { kind: 'menu' }
  if (e.key === 'n' && !shift) return { kind: 'add' }
  if (e.key === '+' || e.key === '=') return { kind: 'zoomIn' }
  if (e.key === '-') return { kind: 'zoomOut' }
  if (e.key === '0') return { kind: 'fit' }
  return null
}

/** What a key asks of the Stan code tab (lot G4): the mapping table's rows are walked, the code is left. */
export type BayesCodeKey = 'prev' | 'next' | 'leave'

/**
 * The keys of the Stan code tab, with no modifier: ↑ and ↓ walk the mapping table's rows (one tab stop for
 * the whole table, a laptop has the arrows), Esc leaves the code (Monaco keeps the focus otherwise) for the
 * tab's own button. Tab and Shift+Tab are the browser's: they go on from the code to the table.
 */
export function bayesCodeKey (e: BayesKeyEvent): BayesCodeKey | null {
  if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return null
  if (e.key === 'ArrowUp') return 'prev'
  if (e.key === 'ArrowDown') return 'next'
  if (e.key === 'Escape') return 'leave'
  return null
}

/** Whether an action is the page's (it works from anywhere but a field) or the canvas'. */
export function bayesKeyScope (kind: BayesKeyKind): 'page' | 'canvas' {
  return kind === 'undo' || kind === 'redo' || kind === 'duplicate' || kind === 'save' ? 'page' : 'canvas'
}

/** One way to press an action: the key and its modifiers (Control and Command are both «mod»). */
export interface BayesKeyWay {
  key: string
  mod?: boolean
  alt?: boolean
  shift?: boolean
}

/** A row of the spec's table: the ways on a PC and on a Mac, and the action each asks for. */
export interface BayesKeyRow {
  id: string
  /** Absent for the rows other code carries (Vue Flow's Tab, the app's `,` `.`, `?`, Esc). */
  action?: BayesKeyKind | 'walk' | 'move'
  pc: BayesKeyWay[]
  mac: BayesKeyWay[]
  /** Where an action that is not this module's lives. */
  elsewhere?: string
}

const arrows = (extra: Omit<BayesKeyWay, 'key'>): BayesKeyWay[] => Object.keys(DIRS).map(key => ({ key, ...extra }))
const same = (...w: BayesKeyWay[]) => ({ pc: w, mac: w })

export const BAYES_KEY_TABLE: readonly BayesKeyRow[] = [
  { id: 'into', ...same({ key: 'Tab' }, { key: 'Enter' }), elsewhere: 'Vue Flow focuses and selects nodes' },
  { id: 'walk', action: 'walk', ...same(...arrows({ alt: true })) },
  { id: 'move', action: 'move', ...same(...arrows({ shift: true })) },
  { id: 'add', action: 'add', ...same({ key: 'n' }) },
  { id: 'menu', action: 'menu', pc: [{ key: 'F10', shift: true }, { key: 'm' }], mac: [{ key: 'm' }] },
  { id: 'remove', action: 'remove', pc: [{ key: 'Delete' }, { key: 'Backspace' }], mac: [{ key: 'Backspace' }] },
  { id: 'duplicate', action: 'duplicate', ...same({ key: 'd', mod: true }) },
  { id: 'undo', action: 'undo', ...same({ key: 'z', mod: true }) },
  { id: 'redo', action: 'redo', pc: [{ key: 'y', mod: true }, { key: 'z', mod: true, shift: true }], mac: [{ key: 'z', mod: true, shift: true }] },
  { id: 'save', action: 'save', ...same({ key: 's', mod: true }) },
  { id: 'zoomIn', action: 'zoomIn', ...same({ key: '+' }) },
  { id: 'zoomOut', action: 'zoomOut', ...same({ key: '-' }) },
  { id: 'fit', action: 'fit', ...same({ key: '0' }) },
  { id: 'codeRows', ...same({ key: 'ArrowUp' }, { key: 'ArrowDown' }), elsewhere: 'the Stan code tab’s mapping table (bayesCodeKey)' },
  { id: 'tabs', ...same({ key: ',' }, { key: '.' }), elsewhere: 'the app’s rule for the main row of tabs' },
  { id: 'escape', ...same({ key: 'Escape' }), elsewhere: 'the fields and the menus, back to the node' },
  { id: 'help', ...same({ key: '?' }), elsewhere: 'the app’s list of keys' },
]

/** The event a way makes on a keyboard: on a Mac «mod» is Command, elsewhere Control. */
export function bayesKeyEventOf (w: BayesKeyWay, mac: boolean): Required<BayesKeyEvent> {
  return { key: w.key, ctrlKey: !!w.mod && !mac, metaKey: !!w.mod && mac, altKey: !!w.alt, shiftKey: !!w.shift }
}

/** Keys that some keyboards do not have (F keys, Home, End, the Menu key): never the only way. */
export function bayesKeyMissingOnLaptops (w: BayesKeyWay): boolean {
  return /^F\d{1,2}$/.test(w.key) || w.key === 'Home' || w.key === 'End' || w.key === 'ContextMenu'
}

/** The forbidden shapes: Alt with a letter (other than an arrow's walk), three modifiers. */
export function bayesKeyForbidden (w: BayesKeyWay): boolean {
  const mods = (w.mod ? 1 : 0) + (w.alt ? 1 : 0) + (w.shift ? 1 : 0)
  return mods >= 3 || (!!w.alt && /^[a-z]$/i.test(w.key))
}

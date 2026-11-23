// What the canvas of a Bayesian model hands to its nodes and boxes (BayesNode.vue, BayesPlate.vue):
// each Vue Flow node holds only an id, and reads what it shows from here, so that typing in the
// inspector redraws the node's text without the canvas rebuilding its nodes.
// Every export starts with `bayes`, `BAYES_` or `Bayes`: a composable's exports are auto-imported everywhere.
import type { Component, InjectionKey } from 'vue'

/** A node as drawn: its kind's shape and icon, its words, its problems. */
export interface BayesNodeView {
  id: string
  kind: string
  icon: Component
  label: string
  name: string
  /** A short fact beside the name (the type of a data node, «random walk»…). */
  what: string
  /** Lines in mono under it: the prior, the expression, the components. */
  lines: string[]
  /** The tag on the top border («hyperprior»), '' for none. */
  role: string
  errors: number
  warnings: number
  selected: boolean
}

/** A box as drawn. */
export interface BayesPlateView {
  id: string
  kind: 'rows' | 'group'
  label: string
  /** The name of the group index that tells its units apart ('' for the rows). */
  keyName: string
  nonCentred: boolean
  series: boolean
  errors: number
  warnings: number
  selected: boolean
  nested: boolean
  /** A dragged node or box would land in this box if dropped now. */
  target: boolean
}

/** A box's place and size while it is resized, as Vue Flow's resizer gives them (relative to its parent box). */
export interface BayesResizeParams { x: number, y: number, width: number, height: number }

export interface BayesCanvasView {
  /** A version looked at: the boxes' switches and handles do nothing. */
  readonly: () => boolean
  node: (id: string) => BayesNodeView | undefined
  plate: (id: string) => BayesPlateView | undefined
  /** The box's switch: the non-centred form on or off (one change of the graph). */
  setForm: (plate: string, nonCentred: boolean) => void
  /** The rows box's switch: these rows are a time series, or not. */
  setSeries: (on: boolean) => void
  /** A box being resized: what it holds stays where it is on the canvas. */
  resizing: (plate: string, p: BayesResizeParams) => void
  /** A resize that ended: the box's new place and size go into the graph. */
  resized: (plate: string, p: BayesResizeParams) => void
}

export const BAYES_CANVAS_VIEW: InjectionKey<BayesCanvasView> = Symbol('bayes-canvas-view')

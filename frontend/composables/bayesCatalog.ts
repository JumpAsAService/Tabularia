// The catalogue of the Bayesian editor as the browser reads it: distributions with their Stan
// names, the vocabulary of the expression language, Stan's reserved words, the limits of the
// language and of the graph. Everything comes from assets/bayes-catalog.json, a byte-for-byte
// copy of the gateway's snapshot (gateway-go/internal/bayescatalog, written from its
// declarations; the copy by .github/scripts/bayes_catalog.py). Nothing of a distribution's name
// or parametrization is written anywhere else: a new distribution appears here by being added there.
//
// Pure functions, nothing of Nuxt or the DOM: the tests import them as they are. Each one takes
// the catalogue as its last argument, the real one unless a test hands another. Every export
// starts with `bayes`, `BAYES_` or `Bayes`: a composable's exports are auto-imported everywhere.

import raw from '~/assets/bayes-catalog.json'

export type BayesUse = 'prior' | 'likelihood' | 'internal'

/** What the browser can draw of a distribution: all of it, one marginal of it, or nothing. */
export type BayesBrowser = 'exact' | 'marginal' | 'none'

export interface BayesInterval {
  lower?: number
  lower_open?: boolean
  upper?: number
  upper_open?: boolean
}

export interface BayesParam {
  name: string
  constraint: string
}

export interface BayesStanNames {
  statement: string
  density: string
  rng?: string
  lcdf?: string
  lccdf?: string
}

export interface BayesDistribution {
  id: string
  uses: BayesUse[]
  params: BayesParam[]
  support: string
  outcome?: string
  stan: BayesStanNames
  location_scale?: { location: string, scale: string }
  link?: string
  browser: BayesBrowser
}

export interface BayesFunction {
  name: string
  stan?: string
  arguments: number
  domain?: string
  range?: string
  apart?: boolean
}

export interface BayesOperator {
  symbol: string
  kind: 'binary' | 'unary'
  stan: string
  elementwise: string
  precedence: number
  assoc: 'left' | 'right'
}

export interface BayesHint { token: string, message: string }
export interface BayesConfusable { codepoint: string, char: string, ascii: string }

export interface BayesCatalog {
  format_version: number
  stan: Record<string, unknown> & { version: string, reserved_words: string[] }
  intervals: Record<string, BayesInterval>
  distributions: BayesDistribution[]
  language: {
    functions: BayesFunction[]
    operators: BayesOperator[]
    hints: BayesHint[]
    confusables: BayesConfusable[]
  }
  internal_functions: string[]
  limits: Record<string, number>
}

/** The real catalogue (the copy of the gateway's snapshot). */
export const BAYES_CATALOG = raw as unknown as BayesCatalog

/** The distributions, all of them or those that may play one role. */
export function bayesDistributions (use?: BayesUse, catalog: BayesCatalog = BAYES_CATALOG): BayesDistribution[] {
  return use ? catalog.distributions.filter(d => d.uses.includes(use)) : catalog.distributions
}

export function bayesDistribution (id: string, catalog: BayesCatalog = BAYES_CATALOG): BayesDistribution | undefined {
  return catalog.distributions.find(d => d.id === id)
}

export function bayesFunction (name: string, catalog: BayesCatalog = BAYES_CATALOG): BayesFunction | undefined {
  return catalog.language.functions.find(f => f.name === name)
}

/** One of the language's and the graph's limits (a positive number). */
export function bayesLimit (key: string, catalog: BayesCatalog = BAYES_CATALOG): number {
  const value = catalog.limits[key]
  if (typeof value !== 'number') throw new Error(`bayes catalogue: no limit named ${key}`)
  return value
}

export function bayesInterval (name: string, catalog: BayesCatalog = BAYES_CATALOG): BayesInterval | undefined {
  return catalog.intervals[name]
}

/** Is `x` inside the interval (the catalogue's open and closed ends)? */
export function bayesIntervalContains (interval: BayesInterval, x: number): boolean {
  if (Number.isNaN(x)) return false
  if (interval.lower !== undefined && (interval.lower_open ? x <= interval.lower : x < interval.lower)) return false
  if (interval.upper !== undefined && (interval.upper_open ? x >= interval.upper : x > interval.upper)) return false
  return true
}

/** Does the distribution have the CDFs a truncation needs (Stan's `lcdf` and `lccdf`)? */
export function bayesCanTruncate (d: BayesDistribution): boolean {
  return Boolean(d.stan.lcdf && d.stan.lccdf)
}

/** Does the browser draw this distribution (all of it or one marginal)? */
export function bayesDrawsInBrowser (d: BayesDistribution): boolean {
  return d.browser !== 'none'
}

/** The distinct supports the distributions name. */
export function bayesSupports (catalog: BayesCatalog = BAYES_CATALOG): string[] {
  return [...new Set(catalog.distributions.map(d => d.support))]
}

/**
 * Every message key the catalogue asks of the five languages' catalogs (the words are in
 * i18n/locales/*.gen.ts, the catalogue holds ids only): a distribution's name and sentence, each
 * of its parameters, each function, each support, and the message of each hint.
 */
export function bayesMessageKeys (catalog: BayesCatalog = BAYES_CATALOG): string[] {
  const keys: string[] = []
  for (const d of catalog.distributions) {
    keys.push(`bayes.dist.${d.id}.name`, `bayes.dist.${d.id}.what`)
    for (const p of d.params) keys.push(`bayes.dist.${d.id}.param.${p.name}`)
  }
  for (const f of catalog.language.functions) keys.push(`bayes.fn.${f.name}`)
  for (const s of bayesSupports(catalog)) keys.push(`bayes.support.${s}`)
  for (const h of catalog.language.hints) keys.push(h.message)
  return keys
}

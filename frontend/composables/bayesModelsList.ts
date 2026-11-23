// The list of the Bayesian models (pages/bayes-models.vue): what a row says, as pure functions the
// tests read without a browser. Every export starts with `bayes` (a composable's exports are
// auto-imported everywhere).
import type { BayesCompile, BayesModelListed } from '~/composables/useBayesModels'

/** The models whose name, description, folder path or opening template match the filter. */
export function bayesFilterModels<T extends Pick<BayesModelListed, 'name' | 'description' | 'project_id' | 'template_id'>> (
  list: readonly T[], query: string, pathOf: (id: number) => string | undefined, templateTitle: (id: string) => string = () => '',
): T[] {
  const fold = (s: string) => s.normalize('NFKD').replace(/\p{M}/gu, '').toLowerCase()
  const q = fold(query.trim())
  if (!q) return [...list]
  return list.filter(m => [m.name, m.description, pathOf(m.project_id) ?? '', m.template_id ? templateTitle(m.template_id) : '']
    .some(x => fold(x).includes(q)))
}

/** The compile cell: its words' key, the second line (when, why), and its tone. */
export function bayesCompileCell (c: BayesCompile | null | undefined): { key: string, sub: 'at' | 'detail' | 'firstRun' | '', tone: 'ok' | 'run' | 'none' | 'ko' } {
  switch (c?.state) {
    case 'ready': return { key: 'bayesModels.compile_ready', sub: '', tone: 'ok' }
    case 'running': return { key: 'bayesModels.compile_running', sub: c.at ? 'at' : '', tone: 'run' }
    case 'queued': return { key: 'bayesModels.compile_queued', sub: c.at ? 'at' : '', tone: 'run' }
    case 'failed': return { key: 'bayesModels.compile_failed', sub: c.detail ? 'detail' : '', tone: 'ko' }
    default: return { key: 'bayesModels.list.toCompile', sub: 'firstRun', tone: 'none' }
  }
}

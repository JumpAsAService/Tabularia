// One row per EXECUTION of a flow. A launch writes the orchestration run and, under it, one
// child run per step (output, script, container) that carries the same flow_id: the API
// lists them all, so a chart drawing every row showed two or more bars for one run. A child
// whose parent is not in the list (a flow group's cell, the flow's only row for that
// execution) stays.
export interface RunLike {
  id: number
  parent_run_id?: number | null
}

export function executionRuns<T extends RunLike>(runs: readonly T[]): T[] {
  const ids = new Set(runs.map((r) => r.id))
  return runs.filter((r) => r.parent_run_id == null || !ids.has(r.parent_run_id))
}

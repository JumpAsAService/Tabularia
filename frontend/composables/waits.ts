// The pause before the next look at something running (a run, a trial), as the
// gateway waits for its steps (services/waits.py): soon at first, then every second
// for half a minute — where most runs end — then `cap`.
export const FIRST_PAUSE_MS = 300

export function nextPause(previous: number, since: number, cap: number): number {
  if (Date.now() - since > 30_000) return cap
  return Math.min(1000, cap, previous * 2)
}

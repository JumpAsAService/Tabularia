// Minimum visibility time of the skeletons: a 40ms load would make the shimmer flash like
// a glitch. It is not a fixed sleep: it waits only for the DIFFERENCE, so slow loads are
// not slowed down.
export const SKELETON_MIN_MS = 300

/**
 * Waits for what is missing so that the skeleton stays visible at least `min` ms from
 * the instant `t0` (taken with performance.now() at the start of the load).
 */
export async function skeletonPad(t0: number, min = SKELETON_MIN_MS): Promise<void> {
  const wait = min - (performance.now() - t0)
  if (wait > 0) await new Promise((r) => setTimeout(r, wait))
}

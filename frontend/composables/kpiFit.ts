// A KPI tile's figure is never cut (KpiCard, `fit`): it takes the size its card gives it, smaller
// down to a floor where it is still read at a glance, and only when not even the floor holds it
// whole, the same figure in compact notation (€ 7.54B), the whole one in its title. Pure functions,
// so that the choice is tested without a browser; the measuring is the component's.

/** The smallest a figure is drawn on screen before it turns compact, in px (the card's own size
 *  when the author chose a smaller one). On paper there is no floor: a figure there is never
 *  compacted after the fact. */
export const FIGURE_FLOOR_PX = 16

// the number in a figure as the server wrote it: digits with their grouping and decimal marks
// (`,` `.` `'`, a space, a narrow or a no-break one), not the sign or the unit around them
const DIGITS = /\d(?:[\d.,'   ]*\d)?/

/**
 * The figure `text` (written in its format: `€ 7,535,239,509.12`, `1.234 pz`, `26.6%`) with its
 * number in compact notation, the prefix, the suffix and the sign kept: `€ 7.54B`. `brief` writes a
 * number in compact notation in the dashboard's language. Null when there is nothing shorter to say:
 * no value, no digits, or a compact form no shorter than the whole one.
 */
export function compactFigure(text: string | null | undefined, value: number | null | undefined,
  brief: (n: number) => string): string | null {
  if (!text || typeof value !== 'number' || !Number.isFinite(value)) return null
  const m = DIGITS.exec(text)
  if (!m) return null
  const percent = text.slice(m.index + m[0].length).trimStart().startsWith('%')
  const short = brief(Math.abs(percent ? value * 100 : value))
  const out = text.slice(0, m.index) + short + text.slice(m.index + m[0].length)
  return out.length < text.length ? out : null
}

/**
 * Whether the whole figure fits, given how wide it is for each px of its size (`em`: its width at
 * 1 px), the size the card's style gives it there (`size`, the floor or more), and the room of the
 * card's line (`room`, px). Half a pixel of rounding is not a cut.
 */
export function fitsWhole(em: number, size: number, room: number): boolean {
  return !(em > 0) || em * size <= room + 0.5
}

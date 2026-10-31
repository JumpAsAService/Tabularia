// A report's style (app/reports/definition.py, Style) on a dashboard's screen, where the page
// behind it has five themes, not white paper. A colour that cannot be read on this theme is lifted
// until it can, as a rule's red still has to say «red»; a black or a grey, which says nothing, is
// left to the theme. A fill without a colour gets the ink that reads on it. Colours come checked
// from the definition and the engine; here they are checked again, as they reach a page's style.

const HEX = /^#[0-9a-f]{6}$/i
// not tokens: the ink on a fill the author chose, which is the same on every theme
const INK_DARK = '#0b0e14'
const INK_LIGHT = '#ffffff'
const LEAST_CONTRAST = 3 // WCAG's for what is large or bold; a table's colours mark, they do not carry prose

function luminance(hex: string): number {
  const channel = (at: number) => {
    const c = parseInt(hex.slice(at, at + 2), 16) / 255
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
  }
  return 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5)
}
function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}
const colour = (v: unknown): string | null => (typeof v === 'string' && HEX.test(v) ? v : null)
const channels = (hex: string) => [1, 3, 5].map((at) => parseInt(hex.slice(at, at + 2), 16))
const hexOf = (rgb: number[]) => `#${rgb.map((c) => Math.round(c).toString(16).padStart(2, '0')).join('')}`
const GREY_BELOW = 40 // the distance between a colour's channels under which it carries no hue

/** `ink`, as readable on `panel`: itself, or mixed towards the panel's opposite; null for a neutral. */
function readable(ink: string, panel: string): string | null {
  if (!HEX.test(panel) || contrast(ink, panel) >= LEAST_CONTRAST) return ink
  const rgb = channels(ink)
  if (Math.max(...rgb) - Math.min(...rgb) < GREY_BELOW) return null
  const towards = luminance(panel) < 0.5 ? 255 : 0
  for (let step = 1; step <= 10; step++) {
    const mixed = hexOf(rgb.map((c) => c + ((towards - c) * step) / 10))
    if (contrast(mixed, panel) >= LEAST_CONTRAST) return mixed
  }
  return null
}

/** `color` and `background` for a colour and a fill on a panel of this theme. */
function colours(color: unknown, fill: unknown, panel: string): Record<string, string> {
  const [ink, background] = [colour(color), colour(fill)]
  if (background) {
    return { background, color: ink ?? (contrast(INK_DARK, background) >= contrast(INK_LIGHT, background) ? INK_DARK : INK_LIGHT) }
  }
  const shown = ink && readable(ink, panel)
  return shown ? { color: shown } : {}
}

const WEIGHTS: Record<string, number> = { normal: 400, semibold: 600, bold: 700 }

function textStyle(st: Record<string, any>): Record<string, string | number> {
  const out: Record<string, string | number> = {}
  if (typeof st.size === 'number') out.fontSize = `${st.size}pt`
  const weight = typeof st.weight === 'number' ? st.weight : WEIGHTS[st.weight]
  if (weight) out.fontWeight = weight
  if (typeof st.italic === 'boolean') out.fontStyle = st.italic ? 'italic' : 'normal'
  if (st.align === 'left' || st.align === 'center' || st.align === 'right') out.textAlign = st.align
  return out
}

/** A table's cell, in the words the engine writes them (model._style: `fill`). */
export function cellStyleOf(st: Record<string, any> | null | undefined, panel: string): Record<string, string | number> {
  if (!st) return {}
  return { ...colours(st.color, st.fill, panel), ...textStyle(st) }
}

const VALIGN: Record<string, string> = { top: 'flex-start', middle: 'center', bottom: 'flex-end' }

/** A KPI's or a text's `style`, in the words of its definition (`background`). Lengths are a
 *  report's, in millimetres. A chart, a table and an image have none here, as in a report. */
export function tileStyleOf(st: Record<string, any> | null | undefined, panel: string): Record<string, string | number> {
  if (!st) return {}
  const out = { ...colours(st.color, st.background, panel), ...textStyle(st) }
  if (VALIGN[st.valign]) out.justifyContent = VALIGN[st.valign]
  if (typeof st.padding === 'number') out.padding = `${st.padding}mm`
  if (typeof st.radius === 'number') out.borderRadius = `${st.radius}mm`
  const edge = st.border && colour(st.border.color)
  if (edge && typeof st.border.width === 'number') out.border = `${st.border.width}pt solid ${edge}`
  return out
}

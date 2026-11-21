// The box of a list of numbers in a model's form (the times to report survival at, the quantiles, the age a unit has
// already lasted): the node's form and Predict's read and show it the same way. What is typed is read the way the
// language writes numbers (the comma is the decimal mark in it, de, fr and es), what was read is said under the box
// («3 values read: 12,5 · 24 · 36»), and a part that reads two ways or no way is not guessed: the box says so, keeps
// what was typed and saves nothing.
import { computed, reactive } from 'vue'
import { useI18n } from 'vue-i18n'
import { modelDecimalComma, modelNumberText, modelNumbersProblem, modelNumbersRead, modelNumbersText, type ModelField } from '~/composables/modelCatalog'

type Bad = { text: string; problem: 'ambiguous' | 'notNumber'; part: string; saved: string }

export function useModelNumbersBox(read: (key: string) => unknown, save: (key: string, values: number[] | undefined) => void) {
  const { t, locale } = useI18n()
  const decimalComma = computed(() => modelDecimalComma(locale.value))
  const bad = reactive<Record<string, Bad>>({})
  const stamp = (key: string) => JSON.stringify(read(key) ?? null)
  // the typed text that was not saved, while the saved value is still the one it was typed over
  const kept = (key: string) => (bad[key] && bad[key].saved === stamp(key) ? bad[key] : null)

  const text = (f: Pick<ModelField, 'key'>): string => kept(f.key)?.text ?? modelNumbersText(read(f.key), decimalComma.value)

  function change(f: Pick<ModelField, 'key'>, raw: string) {
    delete bad[f.key]
    const got = modelNumbersRead(raw, decimalComma.value)
    if (got.problem) {
      bad[f.key] = { text: raw, problem: got.problem, part: got.part ?? '', saved: stamp(f.key) }
      return
    }
    save(f.key, got.values.length ? got.values : undefined)
  }

  /** What is wrong with the box, in words: '' when nothing. */
  function problem(f: ModelField): string {
    const b = kept(f.key)
    if (b) return t(`models.form.numbers_${b.problem}`, { part: b.part })
    const p = modelNumbersProblem(f, read(f.key))
    return p ? t(`models.form.${p}`, { above: f.above ?? '', below: f.below ?? '', min: f.min ?? '', max: f.max ?? '', n: f.max_count ?? '' }) : ''
  }

  /** What was read, said back: «3 values read: 12,5 · 24 · 36». */
  function said(f: Pick<ModelField, 'key'>): string {
    const v = read(f.key)
    if (kept(f.key) || !Array.isArray(v) || !v.length) return ''
    return t('models.form.numbersRead', { n: v.length, list: v.map((n) => modelNumberText(Number(n), decimalComma.value)).join(' · ') }, v.length)
  }

  const reset = () => { for (const k of Object.keys(bad)) delete bad[k] }
  return { text, change, problem, said, reset, decimalComma }
}

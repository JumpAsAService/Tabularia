// The icon of a model's node: the palette's item, the canvas node, the panel's title. One place, so
// that a model added to the catalogue is given its icon once. Exports start with `model`: a
// composable's exports are auto-imported everywhere.
import { ChartScatter, ChartSpline, CircleDollarSign, HeartPulse, TrendingDown, UsersRound } from 'lucide-vue-next'
import { modelOf } from '~/composables/modelCatalog'

// a model with an icon of its own; the others have their family's
const OWN = { parametric_survival: TrendingDown, clv: CircleDollarSign, bg_bb: UsersRound }
const FAMILY = { survival: HeartPulse, timeseries: ChartSpline }

/** The lucide icon of a model of the catalogue (a scatter of points for the ones with nothing of their own). */
export function modelIconOf(id: unknown) {
  const model = modelOf(id)
  if (!model) return ChartScatter
  return (OWN as Record<string, typeof ChartScatter>)[model.id] ?? (FAMILY as Record<string, typeof ChartScatter>)[model.family] ?? ChartScatter
}

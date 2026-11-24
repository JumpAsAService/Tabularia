// Where a report's viewer stands when new pages replace the old ones (components/report/
// ReportPages.vue). The same document drawn again (a sort, a group closed) keeps the place
// it was at; another document (the posterior report, then the prior predictive check)
// starts at its top — a place kept from a longer document would land past the end of a
// shorter one. Pages that come with no name (an owner that names no documents, or none shown
// yet) keep the place as it is.
export type ScrollOnSwap = 'top' | 'keep'

export function scrollOnSwap(before: string | null | undefined, after: string | null | undefined): ScrollOnSwap {
  if (after == null) return 'keep'
  return before === after ? 'keep' : 'top'
}

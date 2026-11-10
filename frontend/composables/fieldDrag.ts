// A field being dragged in the Viewer: from the list of fields onto the table, the filters
// or the chart, or a column's name dragged along the table (or out of it). The same way as
// the report designer's panels (reportDesignStore `dragged`): an HTML5 drag whose payload is
// kept here, because a drag's data cannot be read until the drop, and the targets must know
// what is coming to say whether they take it.
import { ref } from 'vue'

export const FIELD_DRAG_TYPE = 'application/x-tabularia-field'

export interface FieldDragPayload {
  name: string
  dtype: string
  // the list of fields, a column's name in the table, or a chip on one of the three areas
  from: 'list' | 'table' | 'rows' | 'cols' | 'measures'
  // its place on that area, and a measure's function
  index?: number
  func?: string
  computed?: boolean
}

/** What is being dragged now (reactive: the targets show themselves while it is set). */
export const draggedField = ref<FieldDragPayload | null>(null)
/** A column dragged out of its table: releasing it there removes it. */
export const fieldDragOutside = ref(false)

export function startFieldDrag(e: DragEvent, payload: FieldDragPayload) {
  draggedField.value = payload
  fieldDragOutside.value = false
  e.dataTransfer?.setData(FIELD_DRAG_TYPE, payload.name)
  e.dataTransfer?.setData('text/plain', payload.name)
  if (e.dataTransfer) e.dataTransfer.effectAllowed = payload.from === 'list' ? 'copy' : 'copyMove'
}

export function endFieldDrag() {
  draggedField.value = null
  fieldDragOutside.value = false
}

/** The field a drag event carries, when it is one of ours. */
export function fieldDragOf(e: DragEvent): FieldDragPayload | null {
  return e.dataTransfer?.types.includes(FIELD_DRAG_TYPE) ? draggedField.value : null
}

// Icona e colore per ogni tipo di operazione (sidebar, nodi del canvas).
// Un posto solo: aggiungendo un'operazione all'engine basta estendere qui.
import type { FunctionalComponent } from 'vue'
import {
  Columns3,
  EyeOff,
  PenLine,
  Type,
  Filter,
  ArrowUpDown,
  Scissors,
  Fingerprint,
  PaintBucket,
  Eraser,
  Sigma,
  Link2,
  Settings,
  FileText,
  Repeat,
  UnfoldHorizontal,
  FoldVertical,
  Calculator,
  Merge,
  Terminal,
} from 'lucide-vue-next'

export interface OpMeta {
  icon: FunctionalComponent
  label: string
  color: string // accento del nodo/voce sidebar
}

const OP_META: Record<string, OpMeta> = {
  select:     { icon: Columns3,    label: 'ops.select', color: '#4f8cff' },
  drop:       { icon: EyeOff,      label: 'ops.drop',    color: '#4f8cff' },
  rename:     { icon: PenLine,     label: 'ops.rename',          color: '#4f8cff' },
  cast:       { icon: Type,        label: 'ops.cast',       color: '#4f8cff' },
  compute:    { icon: Calculator,  label: 'ops.compute', color: '#4f8cff' },
  sql:        { icon: Terminal,     label: 'ops.sql',       color: '#4f8cff' },
  filter:     { icon: Filter,      label: 'ops.filter',            color: '#f59e0b' },
  sort:       { icon: ArrowUpDown, label: 'ops.sort',            color: '#f59e0b' },
  limit:      { icon: Scissors,    label: 'ops.limit',      color: '#f59e0b' },
  unique:     { icon: Fingerprint, label: 'ops.unique',      color: '#f59e0b' },
  fill_null:  { icon: PaintBucket, label: 'ops.fill_null',       color: '#a78bfa' },
  drop_nulls: { icon: Eraser,      label: 'ops.drop_nulls',       color: '#a78bfa' },
  group_by:   { icon: Sigma,       label: 'ops.group_by',         color: '#6ee7b7' },
  pivot:      { icon: UnfoldHorizontal, label: 'ops.pivot',   color: '#6ee7b7' },
  unpivot:    { icon: FoldVertical,     label: 'ops.unpivot', color: '#6ee7b7' },
  join:       { icon: Link2,       label: 'ops.join',              color: '#6ee7b7' },
  union:      { icon: Merge,       label: 'ops.union', color: '#6ee7b7' },
  foreach:    { icon: Repeat,      label: 'ops.foreach',     color: '#f472b6' },
}

const DEFAULT_META: OpMeta = { icon: Settings, label: '', color: '#8b93a7' }

export const SOURCE_META: OpMeta = { icon: FileText, label: 'ops.source', color: '#6ee7b7' }

export function opMeta(type: string): OpMeta {
  return OP_META[type] ?? { ...DEFAULT_META, label: type }
}

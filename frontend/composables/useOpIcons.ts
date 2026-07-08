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
} from 'lucide-vue-next'

export interface OpMeta {
  icon: FunctionalComponent
  label: string
  color: string // accento del nodo/voce sidebar
}

const OP_META: Record<string, OpMeta> = {
  select:     { icon: Columns3,    label: 'Seleziona colonne', color: '#4f8cff' },
  drop:       { icon: EyeOff,      label: 'Scarta colonne',    color: '#4f8cff' },
  rename:     { icon: PenLine,     label: 'Rinomina',          color: '#4f8cff' },
  cast:       { icon: Type,        label: 'Cambia tipo',       color: '#4f8cff' },
  filter:     { icon: Filter,      label: 'Filtra',            color: '#f59e0b' },
  sort:       { icon: ArrowUpDown, label: 'Ordina',            color: '#f59e0b' },
  limit:      { icon: Scissors,    label: 'Limita righe',      color: '#f59e0b' },
  unique:     { icon: Fingerprint, label: 'Righe uniche',      color: '#f59e0b' },
  fill_null:  { icon: PaintBucket, label: 'Riempi null',       color: '#a78bfa' },
  drop_nulls: { icon: Eraser,      label: 'Scarta null',       color: '#a78bfa' },
  group_by:   { icon: Sigma,       label: 'Raggruppa',         color: '#6ee7b7' },
  join:       { icon: Link2,       label: 'Join',              color: '#6ee7b7' },
  foreach:    { icon: Repeat,      label: 'Ciclo foreach',     color: '#f472b6' },
}

const DEFAULT_META: OpMeta = { icon: Settings, label: '', color: '#8b93a7' }

export const SOURCE_META: OpMeta = { icon: FileText, label: 'Sorgente file', color: '#6ee7b7' }

export function opMeta(type: string): OpMeta {
  return OP_META[type] ?? { ...DEFAULT_META, label: type }
}

// A che punto è un import in corso: fase, conteggi e tempo trascorso, come li
// manda il gateway in RunInfo.progress («1.250.000 righe ricevute · 4m 05s»).
// I secondi li conta il server (l'orologio del browser può essere sbagliato);
// l'orologio che li fa avanzare fra un poll e l'altro sta in RunProgressText,
// così a ogni secondo si ridisegna quel testo e non la pagina intorno.
import type { RunProgress } from '~/composables/useRuns'

type Translate = (key: string, params?: Record<string, unknown>) => string

export function fmtSeconds(s: number): string {
  if (s < 60) return `${s}s`
  const two = (n: number) => String(n).padStart(2, '0')
  if (s < 3600) return `${Math.floor(s / 60)}m ${two(s % 60)}s`
  return `${Math.floor(s / 3600)}h ${two(Math.floor(s / 60) % 60)}m`
}

/** Il testo della fase; `extraSeconds` = quanti ne sono passati da quando è arrivata. */
export function progressText(t: Translate, locale: string, p: RunProgress | null | undefined, extraSeconds = 0): string | null {
  if (!p) return null
  const time = fmtSeconds(p.seconds + Math.max(0, extraSeconds))
  const n = (v?: number | null) => (v ?? 0).toLocaleString(locale)
  switch (p.phase) {
    case 'queued': return t('runProgress.queued', { time })
    case 'waiting': return t('runProgress.waiting', { time })
    case 'reading': return t('runProgress.reading', { n: n(p.rows), time })
    case 'files': return t('runProgress.files', { file: p.file ?? 0, files: p.files ?? 0, time })
    case 'calls': return t('runProgress.calls', { call: p.call ?? 0, calls: p.calls ?? 0, pages: n(p.pages), n: n(p.rows), time })
    case 'saving': return t('runProgress.saving', { time })
    case 'checking': return t('runProgress.checking', { time })
    default: return t('runProgress.running', { time })
  }
}

// Where a running import stands: phase, counts and time elapsed, as the gateway sends
// them in RunInfo.progress («1,250,000 rows received · 4m 05s»). The server counts the
// seconds (the browser's clock may be wrong); the clock moving them on between one poll
// and the next sits in RunProgressText, so at every second that text is redrawn and not
// the page around it.
import type { RunProgress } from '~/composables/useRuns'

type Translate = (key: string, params?: Record<string, unknown>) => string

export function fmtSeconds(s: number): string {
  if (s < 60) return `${s}s`
  const two = (n: number) => String(n).padStart(2, '0')
  if (s < 3600) return `${Math.floor(s / 60)}m ${two(s % 60)}s`
  return `${Math.floor(s / 3600)}h ${two(Math.floor(s / 60) % 60)}m`
}

/** The phase's text; `extraSeconds` = how many have passed since it arrived. */
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

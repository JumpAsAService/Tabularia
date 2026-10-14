// What an edit of a connection really changed: the body of its PATCH. Sending the
// whole form wrote back whatever the form showed, so a field the form had shown
// wrong (a database or schema blanked when the dialog opened) replaced the stored
// value. An empty password means "keep it", as before; empty and missing are the
// same for text, and `extra` is compared as the JSON it holds.
import type { ConnectionDraft, ConnectionInfo } from '~/composables/useConnections'

function sameJson(a: string | null | undefined, b: string | null | undefined): boolean {
  const read = (s: string | null | undefined) => {
    try {
      return JSON.stringify(JSON.parse(s || '{}'))
    } catch {
      return s ?? ''
    }
  }
  return read(a) === read(b)
}

export function connectionChanges(existing: ConnectionInfo, draft: ConnectionDraft): Partial<ConnectionDraft> {
  const out: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(draft)) {
    if (key === 'password') {
      if (value) out.password = value
    } else if (key === 'extra') {
      if (!sameJson(existing.extra, value as string)) out.extra = value
    } else if (key === 'port') {
      if ((existing.port ?? null) !== (value ?? null)) out.port = value
    } else if (((existing as unknown as Record<string, unknown>)[key] ?? '') !== (value ?? '')) {
      out[key] = value
    }
  }
  return out as Partial<ConnectionDraft>
}

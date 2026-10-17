// Engine messages the interface explains in the user's language. The engine
// writes its errors in English, and a run keeps them as they are; a few deserve
// more than a technical sentence because they ask the user to DO something. The
// SFTP host-key refusals are those: the connection is refused until someone runs
// «Test connection» again. They begin with a fixed English text (see
// backend/app/ingest/sftp.py, _handshake), which is what is matched here; any
// other message is shown unchanged.
type Translate = (key: string, values?: Record<string, unknown>) => string

const HOST_KEY_CHANGED = /^SFTP: the host key of (\S+) has changed/
const HOST_KEY_UNKNOWN = /^SFTP: the host key of (\S+) has not been accepted yet/

export function knownError(message: string | null | undefined, t: Translate): string {
  const text = message ?? ''
  let m = HOST_KEY_CHANGED.exec(text)
  if (m) return t('sftp.hostKeyChangedError', { host: m[1] })
  m = HOST_KEY_UNKNOWN.exec(text)
  if (m) return t('sftp.hostKeyUnknownError', { host: m[1] })
  return text
}

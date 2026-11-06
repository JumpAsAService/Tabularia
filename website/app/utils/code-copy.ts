// What the copy button of a docs code block puts on the clipboard: the block as written,
// or, when some lines carry a shell prompt ("$ ", "PS> ", "# " is not one: it is a comment),
// only those lines without the prompt, so the output shown under a command is left out.
const PROMPT = /^\s*(?:\$|PS [^>]*>|PS>)\s/

export function commandText (code: string): string {
  const text = code.replace(/\n+$/, '')
  const lines = text.split('\n')
  if (!lines.some(l => PROMPT.test(l))) return text
  return lines.filter(l => PROMPT.test(l)).map(l => l.replace(PROMPT, '')).join('\n')
}

const SHELLS = new Set(['bash', 'sh', 'shell', 'shellscript', 'zsh', 'console'])
const POWERSHELL = new Set(['powershell', 'ps1', 'pwsh', 'ps'])

/**
 * The terminal a code block runs in, when the page says it: a block that declares it
 * (```bash [Ubuntu terminal]), a PowerShell block anywhere, or a shell block inside a
 * system's block of the docs (Windows: the Ubuntu terminal of WSL; Linux, macOS: Terminal).
 * Otherwise nothing: the same command often runs in several terminals.
 */
export function terminalLabel (language?: string | null, os?: string | null, filename?: string | null): string {
  if (filename) return filename
  const lang = (language || '').toLowerCase()
  if (POWERSHELL.has(lang)) return 'PowerShell'
  if (!os || !SHELLS.has(lang)) return ''
  return os === 'windows' ? 'Ubuntu terminal (WSL)' : 'Terminal'
}

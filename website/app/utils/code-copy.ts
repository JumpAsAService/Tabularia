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

// Da quale browser e sistema arriva una sessione, letto dallo user-agent che il
// gateway ha tenuto all'accesso: «Firefox · Linux», «Chrome · Windows». Serve a
// riconoscere a colpo d'occhio «questo sono io sul portatile», non a identificare
// niente: lo user-agent lo dichiara il client.
const BROWSERS: [RegExp, string][] = [
  [/Edg\//, 'Edge'],
  [/OPR\/|Opera/, 'Opera'],
  [/Firefox\//, 'Firefox'],
  [/Chrome\/|CriOS\//, 'Chrome'],
  [/Safari\//, 'Safari'],
  [/^curl\//, 'curl'],
  [/python|aiohttp/i, 'Python'],
]
const SYSTEMS: [RegExp, string][] = [
  [/Windows/, 'Windows'],
  [/iPhone|iPad/, 'iOS'],
  [/Android/, 'Android'],
  [/Mac OS X|Macintosh/, 'macOS'],
  [/CrOS/, 'ChromeOS'],
  [/Linux/, 'Linux'],
]

/** «Browser · sistema», o null se lo user-agent manca (un osservatore non lo lascia). */
export function deviceLabel(userAgent: string | null | undefined): string | null {
  if (!userAgent) return null
  const browser = BROWSERS.find(([re]) => re.test(userAgent))?.[1] ?? userAgent.split(/[\s/]/)[0]
  const system = SYSTEMS.find(([re]) => re.test(userAgent))?.[1]
  return system ? `${browser} · ${system}` : browser
}

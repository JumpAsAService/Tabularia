// Which browser and system a session comes from, read from the user agent the gateway
// kept at sign-in: «Firefox · Linux», «Chrome · Windows». It serves to recognize at a
// glance «this is me on the laptop», not to identify anything: the client declares the
// user agent.
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

/** «Browser · system», or null when the user agent is missing (an observer does not leave one). */
export function deviceLabel(userAgent: string | null | undefined): string | null {
  if (!userAgent) return null
  const browser = BROWSERS.find(([re]) => re.test(userAgent))?.[1] ?? userAgent.split(/[\s/]/)[0]
  const system = SYSTEMS.find(([re]) => re.test(userAgent))?.[1]
  return system ? `${browser} · ${system}` : browser
}

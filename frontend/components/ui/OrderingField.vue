<script setup lang="ts">
// Il campo dietro il login: valori che si mettono in ordine.
//
// Esiste UNA tabella perfetta — colonne a passo fisso, righe a passo fisso —
// che scorre piano verso destra. Ogni valore se ne allontana tanto più quanto è
// a sinistra: sparso, inclinato, di misura diversa, e scritto SPORCO (separatori
// misti, spazi, n/a). Attraversando il centro rientra nella sua cella e diventa
// il valore pulito, allineato a destra. È quello che fa il prodotto, mostrato
// invece che detto; e non è la pioggia di Matrix: nulla cade, tutto si sistema.
//
// Un canvas solo, decorativo (aria-hidden). Colori letti dai token del tema,
// quindi segue dark / light / dracula / monokai senza saperne niente.
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = withDefaults(
  defineProps<{
    // zona centrale lasciata vuota (la card del login): lì non si disegna nulla,
    // così niente si muove dietro a chi sta digitando
    quietWidth?: number
    quietHeight?: number
  }>(),
  { quietWidth: 460, quietHeight: 620 },
)

const canvas = ref<HTMLCanvasElement | null>(null)

// sporco → pulito. Solo numeri e i loro modi di essere sbagliati.
const VALUES: [string, string][] = [
  [' 1.204,50', '1204.50'], ['12,0', '12.00'], ['n/a', '—'], ['3,1E2', '310.00'],
  ['0042', '42'], ['7.5 %', '0.075'], ['1 980', '1980'], ['-,5', '-0.50'],
  ['NULL', '—'], ['€ 89,9', '89.90'], ['2e3', '2000'], [' 17 ', '17'],
  ['003.10', '3.10'], ['1,000,000', '1000000'], ['?', '—'], ['44.0 ', '44.00'],
  ['9,99', '9.99'], ['.8', '0.80'], ['15%', '0.15'], ['1.5k', '1500'],
  ['-0', '0'], ['6,02e1', '60.20'], ['#N/D', '—'], ['2 .50', '2.50'],
  ['0,07', '0.07'], ['318,', '318'], ['+41', '41'], ['1/2', '0.50'],
]
const MONO = 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace'
const SPEED = 13 // px/s: la tabella scorre, non corre
const FPS = 30 // il movimento è lento: 30 fotogrammi bastano e dimezzano il costo

// hash intero deterministico di (colonna assoluta, riga) → il contenuto di una
// cella non cambia mentre attraversa lo schermo
function hash(a: number, b: number): number {
  let h = (a * 374761393 + b * 668265263) | 0
  h = (h ^ (h >>> 13)) * 1274126177
  h = h ^ (h >>> 16)
  return h >>> 0
}
const unit = (h: number, salt: number) => ((hash(h, salt) % 10000) / 10000)
const smooth = (a: number, b: number, x: number) => {
  const t = Math.min(1, Math.max(0, (x - a) / (b - a)))
  return t * t * (3 - 2 * t)
}
const lerp = (a: number, b: number, t: number) => a + (b - a) * t

let raf = 0
let last = 0
let clock = 0 // secondi di animazione (si ferma con la scheda nascosta)
let W = 0
let H = 0
let dpr = 1
let colors = { text: '#e8ebf2', accent: '#4f8cff', accent2: '#6ee7b7' }
// su fondo CHIARO lo stesso alpha rende molto meno (testo scuro al 5% su quasi
// bianco sparisce): i valori e i filetti si rinforzano in proporzione
let boost = 1
let reduced = false
let ro: ResizeObserver | null = null
let mo: MutationObserver | null = null
let mq: MediaQueryList | null = null
// stringhe dei font precostruite (misure a mezzo pixel): niente template per cella
const FONTS: string[] = []
for (let i = 0; i <= 60; i++) FONTS[i] = `${i / 2}px ${MONO}`

function luminance(css: string): number {
  const c = document.createElement('canvas').getContext('2d')
  if (!c) return 0
  c.fillStyle = css
  const m = /^#([0-9a-f]{6})$/i.exec(c.fillStyle as string)
  if (!m) return 0
  const n = parseInt(m[1], 16)
  return (0.2126 * ((n >> 16) & 255) + 0.7152 * ((n >> 8) & 255) + 0.0722 * (n & 255)) / 255
}

function readTheme() {
  const cs = getComputedStyle(document.documentElement)
  const v = (n: string, d: string) => cs.getPropertyValue(n).trim() || d
  colors = { text: v('--text', colors.text), accent: v('--accent', colors.accent), accent2: v('--accent-2', colors.accent2) }
  boost = luminance(v('--bg', '#0b0e14')) > 0.6 ? 1.75 : 1
}

function resize() {
  const el = canvas.value
  if (!el) return
  dpr = Math.min(window.devicePixelRatio || 1, 2)
  W = el.clientWidth
  H = el.clientHeight
  el.width = Math.round(W * dpr)
  el.height = Math.round(H * dpr)
  draw()
}

function draw() {
  const el = canvas.value
  const ctx = el?.getContext('2d')
  if (!el || !ctx || !W || !H) return
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  ctx.clearRect(0, 0, W, H)

  const narrow = W < 720
  const colW = narrow ? 76 : 98
  const rowH = narrow ? 26 : 30
  const cleanSize = narrow ? 11 : 12.5
  const quietX = Math.min(props.quietWidth, W - 24) / 2
  const quietY = Math.min(props.quietHeight, H - 24) / 2

  // DOVE i valori si sistemano. La fascia è legata alla card, non a frazioni
  // fisse dello schermo: a 1440 px cadeva quasi tutta SOTTO il form, e il
  // momento che dà senso alla pagina non lo vedeva nessuno. Largo: si chiude
  // appena a sinistra della card, così a destra la tabella esce già intera.
  // Stretto: la card occupa tutta la larghezza, l'asse diventa verticale —
  // caos sopra, tabella sotto.
  const to = narrow ? H / 2 + quietY + 10 : Math.max(120, W / 2 - quietX - 28)
  const from = narrow ? Math.max(24, H / 2 - quietY - 30) : Math.max(40, to - Math.max(280, W * 0.24))

  // filetti della tabella: compaiono solo dove c'è ordine
  const rule = narrow ? ctx.createLinearGradient(0, from, 0, to) : ctx.createLinearGradient(from, 0, to + 120, 0)
  rule.addColorStop(0, 'transparent')
  rule.addColorStop(1, colors.text)
  ctx.globalAlpha = Math.min(1, 0.055 * boost)
  ctx.fillStyle = rule
  if (narrow) { for (let y = Math.ceil(from / rowH) * rowH; y < H; y += rowH) ctx.fillRect(0, Math.round(y) + 0.5, W, 1) }
  else { for (let y = rowH; y < H; y += rowH) ctx.fillRect(from, Math.round(y) + 0.5, W - from, 1) }

  const scroll = clock * SPEED
  const shift = Math.floor(scroll / colW)
  const offset = scroll - shift * colW
  const cols = Math.ceil(W / colW) + 2
  const rows = Math.ceil(H / rowH) + 1
  let font = ''
  ctx.textBaseline = 'middle'
  ctx.textAlign = 'right'

  for (let c = -1; c < cols; c++) {
    const k = c - shift // colonna ASSOLUTA: identità stabile mentre scorre
    const gx = c * colW + offset + colW - 14 // filo destro della cella
    const ox = narrow ? 0 : smooth(from, to, gx)
    for (let r = 0; r < rows; r++) {
      const h = hash(k, r)
      if (h % 100 >= 58) continue // la tabella ha dei vuoti: respira
      const gy = r * rowH + rowH / 2
      const o = narrow ? smooth(from, to, gy) : ox

      // quanto è lontano dalla sua cella: tutto scala con (1 - o)
      const d = 1 - o
      const wob = Math.sin(clock * (0.11 + unit(h, 1) * 0.16) + unit(h, 2) * 6.283)
      const wob2 = Math.cos(clock * (0.09 + unit(h, 3) * 0.13) + unit(h, 4) * 6.283)
      const x = gx + d * ((unit(h, 5) - 0.5) * colW * 1.7 + wob * 34)
      const y = gy + d * ((unit(h, 6) - 0.5) * rowH * (narrow ? 4 : 7) + wob2 * (narrow ? 26 : 46))
      const cx = x - 40 // centro del testo: è allineato a destra su x
      if (Math.abs(cx - W / 2) < quietX && Math.abs(y - H / 2) < quietY) continue
      if (cx > W - 190 && y < 64) continue // sotto il selettore della lingua

      const size = Math.round(lerp(9 + unit(h, 7) * 15, cleanSize, o) * 2)
      const rot = d * ((unit(h, 8) - 0.5) * 1.1 + wob * 0.12)
      // al passaggio si accende appena: una cucitura di luce dove i dati si sistemano
      const seam = Math.exp(-Math.pow((o - 0.5) / 0.17, 2)) * 0.2
      const alpha = (lerp(0.05 + unit(h, 9) * 0.2, 0.19, o) + seam) * boost
      const pair = VALUES[h % VALUES.length]
      // pochi valori in accento, solo a tabella fatta — e solo valori VERI: un
      // trattino acceso non dice niente
      const lit = h % 23 === 0 && o > 0.62 && pair[1] !== '—'

      // sporco → pulito: il primo SPARISCE prima che il secondo compaia. Disegnati
      // insieme si impastavano, e sembrava un difetto invece di una trasformazione
      const out = 1 - smooth(0.38, 0.5, o)
      const inn = smooth(0.5, 0.62, o)
      const text = out > 0 ? pair[0] : pair[1]
      const a = Math.min(1, alpha * (out > 0 ? out : inn) * (lit ? 2.6 : 1))
      if (a < 0.012) continue

      if (FONTS[size] !== font) { font = FONTS[size]; ctx.font = font }
      ctx.fillStyle = lit ? (h % 2 ? colors.accent : colors.accent2) : colors.text
      ctx.globalAlpha = a
      if (rot > 0.002 || rot < -0.002) {
        const cs = Math.cos(rot) * dpr
        const sn = Math.sin(rot) * dpr
        ctx.setTransform(cs, sn, -sn, cs, x * dpr, y * dpr) // niente save/restore per cella
        ctx.fillText(text, 0, 0)
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      } else {
        ctx.fillText(text, x, y)
      }
    }
  }
  ctx.globalAlpha = 1
}

// con il movimento ridotto il canvas è fermo: se la card cambia misura (errore,
// SSO, lingua) la zona quieta va ridisegnata a mano
watch(() => [props.quietWidth, props.quietHeight], () => draw())

function frame(now: number) {
  raf = requestAnimationFrame(frame)
  // margine di 4 ms: a 60 Hz i fotogrammi arrivano ogni ~16,7 ms e un confronto
  // secco su 33,3 alterna passi da 33 e da 50 ms — scatti visibili su uno
  // scorrimento lento e uniforme
  if (now - last < 1000 / FPS - 4) return
  clock += Math.min(0.1, (now - last) / 1000) // dopo una pausa non si salta in avanti
  last = now
  draw()
}

function start() {
  cancelAnimationFrame(raf)
  if (reduced || document.hidden) { draw(); return }
  last = performance.now()
  raf = requestAnimationFrame(frame)
}

function onMotionPref() {
  reduced = !!mq?.matches
  start()
}

onMounted(() => {
  mq = window.matchMedia('(prefers-reduced-motion: reduce)')
  reduced = mq.matches
  // chi chiede meno movimento vede la stessa immagine, ferma: il senso resta
  clock = 40
  readTheme()
  resize()
  ro = new ResizeObserver(resize)
  if (canvas.value) ro.observe(canvas.value)
  mo = new MutationObserver(() => { readTheme(); draw() })
  mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
  mq.addEventListener('change', onMotionPref)
  document.addEventListener('visibilitychange', start)
  start()
})

onBeforeUnmount(() => {
  cancelAnimationFrame(raf)
  ro?.disconnect()
  mo?.disconnect()
  mq?.removeEventListener('change', onMotionPref)
  document.removeEventListener('visibilitychange', start)
})
</script>

<template>
  <canvas ref="canvas" class="ordering-field" aria-hidden="true" />
</template>

<style scoped>
.ordering-field {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  display: block;
  pointer-events: none;
}
@media (prefers-reduced-motion: no-preference) {
  .ordering-field { animation: field-in 1100ms cubic-bezier(0.16, 1, 0.3, 1) backwards; }
}
@keyframes field-in {
  from { opacity: 0; }
}
</style>

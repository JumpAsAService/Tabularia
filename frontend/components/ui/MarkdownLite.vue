<script setup lang="ts">
// Markdown essenziale per le risposte dell'assistente, reso come NODI Vue e mai
// come HTML: il testo di un modello non e' fidato, e qui non esiste un punto in
// cui possa diventare markup. Copre cio' che un modello scrive davvero:
// paragrafi, titoli, elenchi, tabelle GFM, blocchi e frammenti di codice,
// grassetto e corsivo. Il resto resta testo.
import { computed, defineComponent, h, type PropType } from 'vue'

const props = defineProps<{ text: string }>()

// frammenti in linea come nodi: mai HTML da stringa
const MdInline = defineComponent({
  name: 'MdInline',
  props: { parts: { type: Array as PropType<{ t: string; v: string }[]>, required: true } },
  setup(p) {
    return () => p.parts.map((x) => (x.t === 'b' ? h('strong', x.v) : x.t === 'i' ? h('em', x.v) : x.t === 'code' ? h('code', { class: 'md-code' }, x.v) : x.v))
  },
})

type Inline = { t: 'text' | 'b' | 'i' | 'code'; v: string }
type Block =
  | { k: 'p'; inl: Inline[] }
  | { k: 'h'; level: number; inl: Inline[] }
  | { k: 'ul' | 'ol'; items: Inline[][] }
  | { k: 'code'; v: string }
  | { k: 'table'; head: Inline[][]; align: string[]; rows: Inline[][][] }

function inline(src: string): Inline[] {
  const out: Inline[] = []
  // `codice` | **grassetto** | *corsivo* / _corsivo_
  const re = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\*[^*\n]+\*)|(\b_[^_\n]+_\b)/g
  let last = 0
  let m: RegExpExecArray | null
  while ((m = re.exec(src)) !== null) {
    if (m.index > last) out.push({ t: 'text', v: src.slice(last, m.index) })
    const s = m[0]
    if (m[1]) out.push({ t: 'code', v: s.slice(1, -1) })
    else if (m[2]) out.push({ t: 'b', v: s.slice(2, -2) })
    else out.push({ t: 'i', v: s.slice(1, -1) })
    last = m.index + s.length
  }
  if (last < src.length) out.push({ t: 'text', v: src.slice(last) })
  return out
}

const cells = (line: string) => line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim())
const isRule = (line: string) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line)

const blocks = computed<Block[]>(() => {
  const lines = (props.text || '').replace(/\r\n/g, '\n').split('\n')
  const out: Block[] = []
  let i = 0
  while (i < lines.length) {
    const line = lines[i]
    if (!line.trim()) { i++; continue }
    if (line.trim().startsWith('```')) {
      const buf: string[] = []
      i++
      while (i < lines.length && !lines[i].trim().startsWith('```')) buf.push(lines[i++])
      i++
      out.push({ k: 'code', v: buf.join('\n') })
      continue
    }
    const hm = /^(#{1,4})\s+(.*)$/.exec(line)
    if (hm) { out.push({ k: 'h', level: hm[1].length, inl: inline(hm[2]) }); i++; continue }
    if (line.includes('|') && i + 1 < lines.length && isRule(lines[i + 1])) {
      const head = cells(line).map(inline)
      const align = cells(lines[i + 1]).map((c) => (c.endsWith(':') && !c.startsWith(':') ? 'right' : c.startsWith(':') && c.endsWith(':') ? 'center' : 'left'))
      const rows: Inline[][][] = []
      i += 2
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) rows.push(cells(lines[i++]).map(inline))
      out.push({ k: 'table', head, align, rows })
      continue
    }
    const ul = /^\s*[-*•]\s+(.*)$/.exec(line)
    const ol = /^\s*\d+[.)]\s+(.*)$/.exec(line)
    if (ul || ol) {
      const kind = ul ? 'ul' : 'ol'
      const re = ul ? /^\s*[-*•]\s+(.*)$/ : /^\s*\d+[.)]\s+(.*)$/
      const items: Inline[][] = []
      let m: RegExpExecArray | null
      while (i < lines.length && (m = re.exec(lines[i])) !== null) { items.push(inline(m[1])); i++ }
      out.push({ k: kind, items })
      continue
    }
    const buf = [line]
    i++
    while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|\s*[-*•]\s|\s*\d+[.)]\s|```)/.test(lines[i]) && !(lines[i].includes('|') && i + 1 < lines.length && isRule(lines[i + 1]))) buf.push(lines[i++])
    out.push({ k: 'p', inl: inline(buf.join(' ')) })
  }
  return out
})
</script>

<template>
  <div class="md">
    <template v-for="(b, bi) in blocks" :key="bi">
      <p v-if="b.k === 'p'"><MdInline :parts="b.inl" /></p>
      <component :is="`h${Math.min(6, b.level + 2)}`" v-else-if="b.k === 'h'" class="md-h"><MdInline :parts="b.inl" /></component>
      <ul v-else-if="b.k === 'ul'"><li v-for="(it, ii) in b.items" :key="ii"><MdInline :parts="it" /></li></ul>
      <ol v-else-if="b.k === 'ol'"><li v-for="(it, ii) in b.items" :key="ii"><MdInline :parts="it" /></li></ol>
      <pre v-else-if="b.k === 'code'" class="md-pre"><code>{{ b.v }}</code></pre>
      <div v-else-if="b.k === 'table'" class="md-tablewrap">
        <table class="md-table">
          <thead><tr><th v-for="(c, ci) in b.head" :key="ci" :class="`al-${b.align[ci] || 'left'}`"><MdInline :parts="c" /></th></tr></thead>
          <tbody>
            <tr v-for="(r, ri) in b.rows" :key="ri">
              <td v-for="(c, ci) in r" :key="ci" :class="`al-${b.align[ci] || 'left'}`"><MdInline :parts="c" /></td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </div>
</template>

<style scoped>
.md { font-size: 13.5px; line-height: 1.55; color: var(--text); overflow-wrap: anywhere; }
.md :deep(p), .md p { margin: 0 0 10px; max-width: 72ch; }
.md > :last-child { margin-bottom: 0; }
.md-h { margin: 14px 0 6px; font-size: 14px; font-weight: 600; }
.md ul, .md ol { margin: 0 0 10px; padding-left: 20px; max-width: 72ch; }
.md li { margin: 2px 0; }
.md-code, .md-pre code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.md-code { padding: 1px 5px; border-radius: 4px; background: var(--panel-2); }
.md-pre { margin: 0 0 10px; padding: 10px 12px; border: 1px solid var(--border-soft); border-radius: 8px; background: var(--bg-soft); overflow-x: auto; }
.md-tablewrap { margin: 0 0 10px; overflow-x: auto; border: 1px solid var(--border-soft); border-radius: 8px; }
.md-table { border-collapse: collapse; font-size: 12px; font-variant-numeric: tabular-nums; width: 100%; }
.md-table th { padding: 5px 9px; background: var(--panel-2); color: var(--accent-hi); font-weight: 600; white-space: nowrap; }
.md-table .al-right { text-align: right; }
.md-table .al-center { text-align: center; }
.md-table .al-left { text-align: left; }
.md-table td { padding: 4px 9px; border-top: 1px solid var(--border-soft); white-space: nowrap; }
</style>

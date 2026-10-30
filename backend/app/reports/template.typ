// The reports' FIXED template (docs/specs/reports.md, «Rendering»). It lays out only what the
// engine worked out (backend/app/reports/model.py): the data arrives as JSON in sys.inputs, never
// as Typst source, so a customer called `#panic()`, `$x$` or `<script>` prints as text. Lengths
// are millimetres as the definition stores them, colours #rrggbb checked by the engine; images and
// charts are files the engine wrote next to this one (img/…, chart/…).
//
// What only the layout can know is done here: the page numbers (PAGE(), PAGES()), the header of a
// table and the headers of its groups repeated on every page they continue on (subheaders), the
// keep-together of a group's last row with its subtotal (one unbreakable cell spanning the
// table, with a grid of the same columns inside), the page breaks between groups (a table per
// stretch), and the limit of pages.

#let m = json(bytes(sys.inputs.model))
#let T = m.theme
#let SANS = "IBM Plex Sans"
// IBM's SemiBold file names its family «IBM Plex Sans SmBld»
#let family(w) = if w == 600 { "IBM Plex Sans SmBld" } else { SANS }

#let styled(st, body) = {
  let w = st.at("weight", default: 400)
  text(
    font: family(w),
    weight: w,
    style: if st.at("italic", default: false) { "italic" } else { "normal" },
    size: st.at("size", default: T.size) * 1pt,
    fill: rgb(st.at("color", default: T.color)),
    body,
  )
}

#let horizontal(a) = if a == "right" { right } else if a == "center" { center } else { left }
#let vertical(a) = if a == "bottom" { bottom } else if a == "horizon" { horizon } else { top }
#let placement(st) = horizontal(st.at("align", default: "left")) + vertical(st.at("valign", default: "top"))
#let stroke-of(st) = if "stroke" in st { st.stroke.w * 1mm + rgb(st.stroke.c) } else { none }
#let fill-of(st, default: none) = if "fill" in st { rgb(st.fill) } else { default }

// a text's parts: plain text, or the page numbers only the layout knows
#let parts(ps) = {
  for p in ps {
    if type(p) == str { p } else if "page" in p { context str(counter(page).get().first()) } else {
      context str(counter(page).final().first())
    }
  }
}

#let grows(it) = it.k == "text" and it.st.at("grow", default: false)

#let frame(it, body, height: auto) = {
  let st = it.st
  box(
    width: it.w * 1mm,
    height: if height == auto { it.h * 1mm } else { height },
    inset: st.at("pad", default: 0) * 1mm,
    fill: fill-of(st),
    stroke: stroke-of(st),
    radius: st.at("radius", default: 0) * 1mm,
    clip: true,
    align(placement(st), body),
  )
}

// a text's lines as the designer's canvas draws them, a line-height of 1.2 (IBM Plex Sans: 1.025
// above the baseline, 0.275 below, less a half-leading of 0.05 each): the box's top is the line's,
// not the top of the capitals, which left the ascenders and the accents of É to the box's clip
#let text-body(it) = {
  let size = it.st.at("size", default: T.size) * 1pt
  set text(top-edge: 0.975 * size, bottom-edge: -0.225 * size)
  set par(leading: 0pt)
  styled(it.st, parts(it.parts))
}

#let kpi(it) = {
  let st = it.st
  box(
    width: it.w * 1mm,
    height: it.h * 1mm,
    fill: fill-of(st, default: rgb(T.band)),
    stroke: stroke-of(st),
    radius: st.at("radius", default: 1.5) * 1mm,
    inset: 2.5mm,
    clip: true,
    {
      styled((size: 7.5, color: T.muted), it.label)
      v(0.6mm)
      // the value, and its change beside it: the sparkline keeps the bottom of the card
      styled((size: st.at("size", default: 16), weight: 700, color: st.at("color", default: T.color)), it.value)
      if it.change != none {
        let c = it.change
        let tone = if c.dir == "up" { T.positive } else if c.dir == "down" { T.negative } else { T.muted }
        let arrow = if c.dir == "up" { "↑ " } else if c.dir == "down" { "↓ " } else { "→ " }
        h(2mm)
        // whole: on the next line when it does not fit beside the value, the arrow with its number
        box(styled((size: 7.5, color: tone, weight: 600), arrow + c.t))
      }
      if it.spark != none {
        place(bottom + left, image(it.spark, width: 100%, height: it.spark_h * 1mm))
      }
    },
  )
}

#let item(it, height: auto) = {
  if it.k == "text" {
    frame(it, text-body(it), height: height)
  } else if it.k == "image" {
    box(width: it.w * 1mm, height: it.h * 1mm, clip: true, stroke: stroke-of(it.st),
      radius: it.st.at("radius", default: 0) * 1mm, image(it.src, width: 100%, height: 100%, fit: it.fit))
  } else if it.k == "line" {
    line(start: (0mm, 0mm), end: (it.w * 1mm, it.h * 1mm), stroke: it.stroke.w * 1mm + rgb(it.stroke.c))
  } else if it.k == "rect" {
    frame(it, none)
  } else if it.k == "chart" {
    image(it.src, width: it.w * 1mm, height: it.h * 1mm)
  } else if it.k == "kpi" {
    kpi(it)
  }
}

// a band: items placed at their x, y; a text that can grow pushes the band down
#let band(b, fixed: false) = context {
  let height = b.h * 1mm
  let heights = (:)
  if not fixed {
    for (i, it) in b.items.enumerate() {
      if grows(it) {
        let pad = it.st.at("pad", default: 0) * 1mm
        let need = measure(block(width: it.w * 1mm - 2 * pad, text-body(it))).height + 2 * pad
        let h = calc.max(it.h * 1mm, need)
        heights.insert(str(i), h)
        height = calc.max(height, it.y * 1mm + h)
      }
    }
  }
  block(width: 100%, height: height, breakable: false, {
    for (i, it) in b.items.enumerate() {
      place(top + left, dx: it.x * 1mm, dy: it.y * 1mm, item(it, height: heights.at(str(i), default: auto)))
    }
  })
}

// where a click sorts a column or opens a group: the cell's `body` as before, with two marks — after
// the text the point the screen always had (`x` where the content starts, `y` on its baseline), and
// before it the content's own box, measured where it is laid out, with the cell's padding `pad` (mm)
// around it; the final query widens a header's box to its row's (template's end)
#let hot(data, body, pad) = if not m.hot { body } else {
  layout(size => {
    let h = measure(block(width: size.width, body)).height
    [#metadata((w: size.width.mm() + 2 * pad.x, h: h.mm() + 2 * pad.y, pad: pad))<hot-box>]
    body + [#metadata(data)<hot>]
  })
}

// ── tables ─────────────────────────────────────────────────────────────────────────────────
#let rule = 0.4pt + rgb(T.rule)
#let cell-pad = (x: 1.5, y: 1.1)  // mm
#let cell-inset = (x: cell-pad.x * 1mm, y: cell-pad.y * 1mm)

#let table-part(t, rows) = {
  let n = t.cols.len()
  let widths = t.cols.map(c => if c.w == none { 1fr } else { c.w * 1mm })
  let aligns = t.cols.map(c => horizontal(c.align))
  let col-style = t.cols.map(c => c.at("st", default: (:)))
  let sorting = t.hot != none and t.hot.sort
  let toggling = t.hot != none and t.hot.toggle

  let content-of(c) = if type(c) == str { (c, (:)) } else { (c.t, c.st) }
  let row-cells(r, cell) = {
    if r.k == "d" {
      let out = ()
      for (j, c) in r.c.enumerate() {
        let (txt, st) = content-of(c)
        let fill = fill-of(col-style.at(j) + st, default: if r.at("z", default: false) { rgb(T.head) } else { none })
        out.push(cell(fill: fill, align: aligns.at(j) + horizon, styled(col-style.at(j) + st, txt)))
      }
      out
    } else {
      let total = r.k == "t"
      let fill = if total { rgb(T.total) } else { none }
      let base = if total { (weight: 700, color: "#ffffff") } else { (weight: 700) }
      let out = ()
      if r.span > 0 {
        out.push(cell(colspan: r.span, fill: fill, align: right + horizon, styled(base, r.label)))
      } else if r.label != "" {
        // every column has a total: the label on a row of its own, above the values
        out.push(cell(colspan: n, fill: fill, align: left + horizon, styled(base, r.label)))
      }
      for (j, c) in r.c.enumerate() {
        let (txt, st) = content-of(c)
        out.push(cell(fill: fill-of(st, default: fill), align: aligns.at(r.span + j) + horizon, styled(base + st, txt)))
      }
      out
    }
  }

  let cells = ()
  let head = ()
  for (hi, hr) in t.head.enumerate() {
    for (j, h) in hr.enumerate() {
      let mark = if t.sorted != none and t.sorted.col == j and hi == 0 {
        if t.sorted.dir == "desc" { " ↓" } else { " ↑" }
      } else { "" }
      let body = styled((weight: 600), h.t + mark)
      if sorting and hi == 0 { body = hot((kind: "sort", item: t.id, column: j), body, cell-pad) }
      head.push(table.cell(colspan: h.span, rowspan: h.rspan, fill: rgb(T.head), align: horizontal(h.align) + horizon, body))
    }
  }
  cells.push(table.header(level: 1, repeat: t.repeat, ..head))
  if rows.len() == 0 {
    cells.push(table.cell(colspan: n, align: center, styled((color: T.muted, italic: true), m.empty)))
  }
  let i = 0
  while i < rows.len() {
    let r = rows.at(i)
    if r.k == "g" {
      let mark = if toggling and "key" in r { if r.closed { "+ " } else { "− " } } else { "" }
      let body = styled((weight: if r.lv == 0 { 700 } else { 600 }), mark + r.t)
      let pad = (x: 1.5 + r.lv * 3, y: 1.3)
      if toggling and "key" in r { body = hot((kind: "toggle", item: t.id, key: r.key, closed: r.closed), body, pad) }
      let c = table.cell(colspan: n, fill: if r.lv == 0 { rgb(T.band) } else { rgb(T.band).lighten(50%) },
        inset: (x: pad.x * 1mm, y: pad.y * 1mm), body)
      // every group row is a header of its level, repeated or not: a header ends the repetition
      // of the one before it at that level — a closed group written as a plain cell let the
      // previous group's header come back on the next page with nothing under it
      cells.push(table.header(level: r.lv + 2, repeat: r.rep, c))
      i += 1
    } else if r.at("glue", default: false) or (r.k in ("f", "t") and r.span == 0) {
      // the rows kept together: one unbreakable cell over the whole table, the same columns inside (a
      // total whose label has a row of its own too, so the label never ends a page alone)
      let run = (r,)
      let j = i + 1
      while j < rows.len() and run.last().at("glue", default: false) {
        run.push(rows.at(j))
        j += 1
      }
      let inner = ()
      for x in run { inner += row-cells(x, grid.cell) }
      cells.push(table.cell(colspan: n, inset: 0pt, breakable: false,
        grid(columns: widths, inset: cell-inset, stroke: (x, y) => (bottom: rule), ..inner)))
      i = j
    } else {
      cells += row-cells(r, table.cell)
      i += 1
    }
  }
  table(columns: widths, inset: cell-inset, stroke: (x, y) => (bottom: rule), ..cells)
}

#let table-block(t) = {
  v(t.gap * 1mm)
  // a page break between groups splits the table: every stretch starts with the header
  let stretches = ((),)
  for r in t.rows {
    if r.k == "pb" { stretches.push(()) } else { stretches.at(-1).push(r) }
  }
  for (i, rows) in stretches.enumerate() {
    if i > 0 { pagebreak(weak: true) }
    if i == 0 or rows.len() > 0 {
      pad(left: t.x * 1mm, block(width: t.w * 1mm, table-part(t, rows)))
    }
  }
}

// ── the page ───────────────────────────────────────────────────────────────────────────────
#let P = m.page
#let (mt, mr, mb, ml) = P.margins
#let hh = if m.header != none { m.header.h } else { 0 }
#let fh = if m.footer != none { m.footer.h } else { 0 }

#set document(title: m.title, author: "Tabularia", date: none)
#set text(font: SANS, size: T.size * 1pt, fill: rgb(T.color), lang: m.lang, hyphenate: false)
#set par(leading: 0.45em, spacing: 0.6em)
#set page(
  width: P.w * 1mm,
  height: P.h * 1mm,
  margin: (top: (mt + hh) * 1mm, bottom: (mb + fh) * 1mm, left: ml * 1mm, right: mr * 1mm),
  header-ascent: 0mm,
  footer-descent: 0mm,
  header: if m.header != none {
    context {
      if m.header.first or counter(page).get().first() > 1 { band(m.header, fixed: true) }
    }
  },
  footer: if m.footer != none {
    context {
      if m.footer.last or counter(page).get().first() < counter(page).final().first() { band(m.footer, fixed: true) }
    }
  },
)

// the body's rows stand where their y says: the only space between them is their gap, not
// the spacing Typst leaves around a block
#set block(spacing: 0pt)
#for b in m.body {
  if b.k == "band" {
    v(b.gap * 1mm)
    band(b)
  } else {
    table-block(b)
  }
}

#context {
  let n = counter(page).final().first()
  if n > m.max_pages { panic("tabularia:pages:" + str(n)) }
  [#metadata(n)<pages>]
}

// where the screen can click: the sortable headers and the groups that open and close, each with
// its cell's box. A header's row is as tall as its tallest cell (a header that wraps): its boxes
// take the row's top and bottom, from every box of the table's header on that page.
#if m.hot {
  context {
    let boxes = query(<hot-box>).map(e => {
      let p = e.location().position()
      let v = e.value
      (page: p.page, x: p.x.mm() - v.pad.x, y: p.y.mm() - v.pad.y, w: v.w, h: v.h)
    })
    let marks = query(<hot>)
    let rows = (:)
    for (e, b) in marks.zip(boxes) {
      if e.value.kind == "sort" {
        let key = e.value.item + "/" + str(b.page)
        let r = rows.at(key, default: (top: b.y, bottom: b.y + b.h))
        rows.insert(key, (top: calc.min(r.top, b.y), bottom: calc.max(r.bottom, b.y + b.h)))
      }
    }
    [#metadata(marks.zip(boxes).map(((e, b)) => {
      let p = e.location().position()
      let r = rows.at(e.value.item + "/" + str(b.page), default: none)
      let (y, h) = if e.value.kind == "sort" and r != none { (r.top, r.bottom - r.top) } else { (b.y, b.h) }
      e.value + (page: p.page, x: p.x.mm(), y: p.y.mm(), box: (x: b.x, y: y, w: b.w, h: h))
    }))<hotspots>]
  }
}

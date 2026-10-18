<script setup lang="ts">
// The page's live thing: Tabularia's placeholder language, resolved in the visitor's browser
// for the seven days before today (the range `day = today-7 .. today step 1d`), as the
// product's forms preview a name while it is typed.
const t = useCopy()
const base = useRuntimeConfig().app.baseURL
const schedDays = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
const zones = ref(['Europe/Rome', 'UTC', 'Europe/London', 'America/New_York', 'Asia/Tokyo'])
const zone = ref('Europe/Rome')
const pattern = ref('sales_{day:yyyyMMdd}.csv')
// rendered on the server for a fixed date, then for the visitor's real today once mounted
const now = ref(new Date('2026-10-18T07:30:00Z'))
const live = ref(false)
onMounted(() => {
  now.value = new Date()
  live.value = true
  try { const z = Intl.DateTimeFormat().resolvedOptions().timeZone; if (z && !zones.value.includes(z)) zones.value = [z, ...zones.value]; if (z) zone.value = z } catch {}
})

const days = computed(() => lastDays(now.value, zone.value, 7))
const selected = ref(6)
const day = computed(() => days.value[selected.value]!)
const dayLabel = (d: CivilDate) => {
  const date = new Date(Date.UTC(d.y, d.m - 1, d.d))
  return {
    weekday: date.toLocaleDateString('en-GB', { weekday: 'short', timeZone: 'UTC' }),
    date: date.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' }),
  }
}

// An input template (a datasource's folder, file or API path) with the variables of this example:
// the range `day` (and its window end `day_end`), the list `symbol`, and a last refresh.
// Outputs follow the Output rules ({run_id}, {flow}, {date}, {datetime}; no {last_refresh}).
type Kind = 'input' | 'path' | 'file' | 'text'
function resolve (template: string, kind: Kind = 'input') {
  const variables: Record<string, Value> = {
    day: { kind: 'date', date: day.value },
    day_end: { kind: 'date', date: addDays(day.value, 1) },
    symbol: { kind: 'text', value: 'AAPL' },
  }
  try {
    if (kind === 'input') {
      check(template, { variables: ['symbol'], ranges: ['day'], hasInitial: true })
      return { ok: true, value: render(template, { now: now.value, zone: zone.value, lastRefresh: new Date(now.value.getTime() - 86400e3), variables }) }
    }
    check(template, { output: true })
    const octx = { now: now.value, zone: zone.value, variables: { run_id: { kind: 'text', value: '381' }, flow: { kind: 'text', value: 'Daily sales' } } as Record<string, Value> }
    return { ok: true, value: renderOutput(template, octx, kind, kind === 'text' ? 'subject' : 'folder') }
  } catch (e) {
    return { ok: false, value: e instanceof PlaceholderError ? e.message : 'not a valid name' }
  }
}
const mine = computed(() => resolve(pattern.value))
const bands = computed(() => t.connect.bands.map(b => ({ ...b, out: resolve(b.template, b.role === 'Input' ? 'input' : b.kind === 'Email' ? 'text' : 'path') })))

function onTabKey (e: KeyboardEvent, i: number) {
  const n = days.value.length
  let j = i
  if (e.key === 'ArrowDown' || e.key === 'ArrowRight') j = (i + 1) % n
  else if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') j = (i - 1 + n) % n
  else if (e.key === 'Home') j = 0
  else if (e.key === 'End') j = n - 1
  else return
  e.preventDefault()
  selected.value = j
  nextTick(() => (document.getElementById(`day-tab-${j}`) as HTMLElement | null)?.focus())
}
</script>

<template>
  <section id="connect" class="connect section" aria-labelledby="connect-title" data-rise-group>
    <h2 id="connect-title" class="section-title connect__title" data-rise>
      <span class="soft">{{ t.connect.titleA }}</span>
      <span>{{ t.connect.titleB }}</span>
    </h2>
    <div class="connect__intro">
      <div class="connect__lead">
        <p class="connect__sources" data-rise>{{ t.connect.sources }}</p>
        <p data-rise><NuxtLink to="/docs/placeholders" class="btn btn--accent">{{ t.connect.button }}</NuxtLink></p>
      </div>
      <figure class="connect__ds" data-rise>
        <img :src="`${base}shots/datasources.jpg`" width="1600" height="210" loading="lazy" decoding="async" :alt="t.connect.datasourcesAlt">
      </figure>
    </div>

    <div class="connect__try tile" data-ui data-rise>
      <label for="ph-input" class="connect__trylabel">{{ t.connect.tryLabel }}</label>
      <input
        id="ph-input" v-model="pattern" class="connect__input" type="text" spellcheck="false" autocomplete="off"
        autocapitalize="off" aria-describedby="ph-hint ph-result"
      >
      <label class="connect__zone">
        <span class="sr-only">{{ t.connect.zoneLabel }}</span>
        <select v-model="zone">
          <option v-for="z in zones" :key="z" :value="z">{{ z }}</option>
        </select>
      </label>
      <p id="ph-hint" class="connect__hint">{{ t.connect.tryHint }}</p>
    </div>

    <div class="connect__agenda" data-rise>
      <div class="connect__tabs" role="tablist" :aria-label="t.connect.daysLabel" aria-orientation="vertical">
        <button
          v-for="(d, i) in days" :id="`day-tab-${i}`" :key="`${d.y}-${d.m}-${d.d}`" type="button" role="tab"
          class="connect__tab" :aria-selected="i === selected" :tabindex="i === selected ? 0 : -1" aria-controls="day-panel"
          @click="selected = i" @keydown="onTabKey($event, i)"
        >
          <span class="connect__wd">{{ dayLabel(d).weekday }}</span>
          <span class="connect__dt tnum">{{ dayLabel(d).date }}</span>
        </button>
        <p class="connect__range small soft"><code>{{ t.connect.rangeNote }}</code></p>
      </div>

      <div id="day-panel" class="connect__bands" role="tabpanel" :aria-labelledby="`day-tab-${selected}`" aria-live="polite">
        <div id="ph-result" class="connect__band connect__band--mine" :class="{ 'is-bad': !mine.ok }">
          <span class="connect__time tnum">{{ live ? 'now' : 'today' }}</span>
          <span class="connect__what">
            <span class="connect__resolved">{{ mine.value }}</span>
            <code class="connect__tpl">{{ pattern }}</code>
          </span>
          <span class="connect__kind">{{ mine.ok ? 'Your name' : 'Refused' }}</span>
        </div>
        <div v-for="b in bands" :key="b.template" class="connect__band">
          <span class="connect__time tnum">{{ b.time }}</span>
          <span class="connect__what">
            <span class="connect__resolved">{{ b.out.value }}</span>
            <code class="connect__tpl">{{ b.template }}</code>
          </span>
          <span class="connect__kind">{{ b.kind }} <span class="soft">· {{ b.role }}</span></span>
        </div>
        <p class="connect__note small soft">{{ t.connect.sampleNote }}</p>
      </div>
    </div>

    <div class="connect__sched" data-rise-group>
      <div class="connect__schedtext">
        <h3 data-rise>{{ t.connect.scheduleTitle }}</h3>
        <p data-rise>{{ t.connect.scheduleText }}</p>
      </div>
      <figure class="connect__schedpic sched" data-ui data-rise :aria-label="t.connect.scheduleAlt" role="img">
        <p class="sched__title" aria-hidden="true"><strong>Schedule load</strong> <span>3 schedules · 8 runs in the next 7 days · Europe/Rome</span></p>
        <div class="sched__grid" aria-hidden="true">
          <template v-for="(d, di) in schedDays" :key="d">
            <span class="sched__day">{{ d }}</span>
            <span v-for="h in 24" :key="h" class="sched__cell" :class="{ 'is-on': h - 1 === 5 || (di === 0 && h - 1 === 7) }" />
          </template>
          <span />
          <span v-for="h in 24" :key="`h${h}`" class="sched__hour">{{ (h - 1) % 3 === 0 ? String(h - 1).padStart(2, '0') : '' }}</span><!-- taste-skill-ignore: hours of a clock -->
        </div>
        <p class="sched__ok" aria-hidden="true">No collisions: no minute exceeds the worker capacity (2).</p>
      </figure>
    </div>
  </section>
</template>

<style scoped>
.connect { padding-inline: var(--gutter); }
.connect__intro { display: grid; gap: 40px; margin-top: 32px; }
.connect__lead { display: grid; gap: 20px; justify-items: start; }
.connect__ds { margin: 0; }
.connect__ds img { width: 100%; height: auto; border-radius: var(--radius); background: #141926; }
.connect__sched { display: grid; grid-template-columns: minmax(0, 4fr) minmax(0, 8fr); gap: 40px; align-items: center; margin-top: 120px; }
.connect__schedtext { display: grid; gap: 14px; }
.connect__schedtext h3 { font-size: var(--fs-statement); line-height: 1.15; letter-spacing: -0.015em; font-weight: 600; }
.connect__schedtext p { font-size: var(--fs-body); color: var(--soft); max-width: 40ch; }
.connect__schedpic { margin: 0; }
/* the Flows page's schedule load, drawn from the sample's three schedules */
.sched { background: var(--tile); border-radius: var(--radius); padding: 20px; font-size: 14px; }
.sched__title strong { font-weight: 600; }
.sched__title span { color: var(--soft); }
.sched__grid { display: grid; grid-template-columns: 40px repeat(24, minmax(0, 1fr)); gap: 2px; margin-top: 14px; }
.sched__day { color: var(--soft); align-self: center; }
.sched__cell { height: 22px; background: var(--tile-2); }
.sched__cell.is-on { background: var(--accent); }
.sched__hour { color: var(--soft); font-size: 13px; padding-top: 4px; white-space: nowrap; }
.sched__ok { margin-top: 12px; color: var(--soft); }
.connect__title span { display: block; }
.connect__sources { font-size: var(--fs-body); max-width: 70ch; }

.connect__try {
  margin-top: 56px; padding: 20px; display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 12px 16px;
}
.connect__trylabel { font-weight: 600; font-size: var(--fs-body); }
.connect__input {
  width: 100%; min-height: 56px; padding: 0 16px; border: 2px solid transparent; border-radius: 6px;
  background: var(--ground); color: var(--text); font-family: var(--mono); font-size: var(--fs-body); outline: none; /* taste-skill-ignore: the field takes literal placeholder code */
}
.connect__input:focus { border-color: var(--accent); }
.connect__zone select {
  min-height: 56px; padding: 0 14px; border: 0; border-radius: 6px; background: var(--tile-3); color: var(--text); font-size: var(--fs-small); cursor: pointer;
}
.connect__hint { grid-column: 2 / -1; font-size: var(--fs-small); color: var(--soft); }

.connect__agenda { display: grid; grid-template-columns: 150px minmax(0, 1fr); gap: 40px; margin-top: 8px; padding-top: 32px; }
.connect__tabs { display: grid; gap: 6px; align-content: start; }
.connect__tab {
  display: grid; gap: 2px; justify-items: start; width: 150px; height: 64px; padding: 10px 14px; border: 0; border-radius: var(--radius);
  background: var(--tile); color: var(--text); cursor: pointer; text-align: left;
  transition: background-color 160ms ease, transform 160ms var(--ease-out);
}
.connect__tab:hover { background: var(--tile-2); }
.connect__tab:active { transform: scale(.98); }
.connect__tab[aria-selected="true"] { background: var(--paper); color: var(--on-paper); }
.connect__wd { font-size: var(--fs-body); font-weight: 600; }
.connect__dt { font-size: var(--fs-small); opacity: .8; }
.connect__range { margin-top: 8px; }
.connect__range code { font-size: var(--fs-small); }

.connect__bands { display: grid; gap: 4px; align-content: start; }
.connect__band {
  display: grid; grid-template-columns: 80px minmax(0, 1fr) minmax(140px, auto); align-items: center; gap: 16px;
  min-height: 76px; padding: 12px 20px; border-radius: var(--radius); background: var(--tile);
}
.connect__band:nth-child(odd) { background: var(--tile-2); }
.connect__band--mine, .connect__band--mine:nth-child(odd) { background: var(--accent); color: var(--on-accent); }
.connect__band--mine .connect__tpl, .connect__band--mine .soft { color: inherit; }
.connect__band--mine.is-bad { background: color-mix(in srgb, var(--bad) 22%, var(--tile)); color: var(--text); }
.connect__time { font-size: var(--fs-body); font-weight: 600; }
.connect__what { display: grid; gap: 4px; min-width: 0; }
.connect__resolved { font-size: var(--fs-body); font-weight: 500; overflow-wrap: anywhere; }
.connect__tpl { font-size: var(--fs-small); color: var(--soft); overflow-wrap: anywhere; }
.connect__kind { justify-self: end; font-size: var(--fs-small); font-weight: 600; text-align: right; }
.connect__note { margin-top: 12px; }

@media (max-width: 860px) {
  .connect__intro, .connect__sched { grid-template-columns: 1fr; gap: 24px; }
  .connect__sched { margin-top: 72px; }
  .connect__ds { overflow: hidden; border-radius: var(--radius); }
  .connect__ds img { width: 210%; max-width: none; border-radius: 0; }
  .sched { padding: 14px; }
  .sched__grid { grid-template-columns: 34px repeat(24, minmax(0, 1fr)); gap: 1px; }
  .sched__cell { height: 18px; }
  .connect__try { grid-template-columns: 1fr; }
  .connect__hint { grid-column: 1; }
  .connect__agenda { grid-template-columns: 1fr; gap: 12px; }
  .connect__tabs {
    position: sticky; top: 0; z-index: 2; grid-auto-flow: column; grid-auto-columns: minmax(0, 1fr);
    padding: 6px 0; background: var(--ground);
  }
  .connect__tab { width: auto; height: 56px; padding: 8px; }
  .connect__wd { font-size: var(--fs-small); }
  .connect__range { display: none; }
  .connect__band { grid-template-columns: 1fr; gap: 6px; padding: 16px; }
  .connect__kind { justify-self: start; text-align: left; }
}
</style>

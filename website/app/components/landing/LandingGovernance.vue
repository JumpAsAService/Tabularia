<script setup lang="ts">
// Six guarantees as a list: each row is a short text and one fragment of the interface,
// the fragment alternating sides down the list.
const t = useCopy()
const c = t.governance.cells
const base = useRuntimeConfig().app.baseURL
const sqlRefusal = 'sql: the query can read only the input of the node (`self` or `input`). The table `information_schema.tables` is not allowed: to combine other data use a Join or Union node.'
</script>

<template>
  <section id="governance" class="gov section" aria-labelledby="gov-title" data-rise-group>
    <div class="gov__head">
      <h2 id="gov-title" class="section-title" data-rise>{{ t.governance.title }}</h2>
      <p class="gov__lead" data-rise>{{ t.governance.lead }}</p>
    </div>
    <ul role="list" class="gov__list">
      <li class="row" data-rise>
        <div class="row__text"><h3>{{ c.contract.name }}</h3><p>{{ c.contract.line }}</p></div>
        <div class="row__ui" data-ui aria-hidden="true">
          <span class="f-status"><svg viewBox="0 0 24 24" width="1em" height="1em" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12.5 4.5 4.5L19 7.5" /></svg>Contract OK</span>
          <span class="f-rule"><span>amount ≥ 0</span><span class="f-sev">error</span></span>
          <span class="f-rule"><span>customer_id not null</span><span class="f-sev">error</span></span>
          <span class="f-rule"><span>rows ≥ 1000</span><span class="f-sev f-sev--warn">warning</span></span>
        </div>
      </li>
      <li class="row" data-rise>
        <div class="row__text"><h3>{{ c.sso.name }}</h3><p>{{ c.sso.line }}</p></div>
        <div class="row__ui" data-ui aria-hidden="true">
          <span class="f-btn f-btn--accent">Sign in</span>
          <span class="f-btn"><svg viewBox="0 0 24 24" width="1em" height="1em" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><circle cx="8" cy="15" r="4" /><path d="m11 12 9-9M17 6l3 3" /></svg>Sign in with Keycloak</span>
        </div>
      </li>
      <li class="row" data-rise>
        <div class="row__text"><h3>{{ c.rbac.name }}</h3><p>{{ c.rbac.line }}</p></div>
        <div class="row__ui" data-ui aria-hidden="true">
          <span class="f-row"><span class="f-who">Finance analysts</span><span class="f-chip">view</span><span class="f-chip f-chip--on">edit</span><span class="f-chip">manage</span></span>
          <span class="f-row"><span class="f-who">Data engineers</span><span class="f-chip">view</span><span class="f-chip">edit</span><span class="f-chip f-chip--on">connect</span></span>
        </div>
      </li>
      <li class="row" data-rise>
        <div class="row__text"><h3>{{ c.audit.name }}</h3><p>{{ c.audit.line }}</p></div>
        <div class="row__ui" data-ui aria-hidden="true">
          <span class="f-audit"><span class="f-time tnum">10:42:07</span><span class="f-chip f-chip--soft">export.download</span><span>Margin by Category and Month · CSV</span></span>
          <span class="f-audit"><span class="f-time tnum">10:40:51</span><span class="f-chip f-chip--soft">flow.run</span><span>Cost to Serve by Channel</span></span>
        </div>
      </li>
      <li class="row" data-rise>
        <div class="row__text"><h3>{{ c.sql.name }}</h3><p>{{ c.sql.line }}</p></div>
        <div class="row__ui row__ui--code" data-ui>
          <pre class="f-sql" aria-label="A query refused on the ClickHouse engine, with the product's message"><code><span class="k">SELECT</span> * <span class="k">FROM</span> information_schema.tables
<span class="bad">{{ sqlRefusal }}</span></code></pre>
        </div>
      </li>
      <li class="row" data-rise>
        <div class="row__text"><h3>{{ c.sample.name }}</h3><p>{{ c.sample.line }}</p></div>
        <figure class="row__ui row__ui--pic">
          <img :src="`${base}shots/dev-sample.jpg`" width="648" height="400" loading="lazy" decoding="async" :alt="t.governance.sampleAlt">
        </figure>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.gov { padding-inline: var(--gutter); }
.gov__head { display: grid; gap: 20px; max-width: 760px; }
.gov__lead { font-size: var(--fs-body); max-width: 60ch; }
.gov__list { list-style: none; margin: 64px 0 0; padding: 0; display: grid; gap: 56px; }
.row { display: grid; grid-template-columns: minmax(0, 5fr) minmax(0, 7fr); gap: 48px; align-items: center; }
.row:nth-child(even) .row__text { order: 2; }
.row__text { display: grid; gap: 8px; }
.row__text h3 { font-size: var(--fs-statement); line-height: 1.15; letter-spacing: -0.015em; font-weight: 600; }
.row__text p { font-size: var(--fs-body); max-width: 46ch; }

/* the fragments, at the interface's own size, on one stage tone */
.row__ui {
  margin: 0; min-height: 160px; padding: 28px; border-radius: var(--radius); background: var(--tile);
  display: flex; flex-direction: column; align-items: flex-start; justify-content: center; gap: 10px; font-size: 15px; overflow: hidden;
}
.row__ui--pic { padding: 0; background: #141926; }
.row__ui--pic img { width: 100%; height: auto; display: block; }
.f-status { display: inline-flex; align-items: center; gap: .35em; padding: .3em .7em; border-radius: 6px; background: var(--ok); color: var(--on-ok); font-weight: 600; font-size: 24px; }
.f-rule { display: inline-flex; gap: 10px; align-items: center; color: var(--text); }
.f-sev { padding: 1px 8px; border-radius: 4px; background: var(--tile-3); color: var(--text); font-size: 13px; font-weight: 600; }
.f-sev--warn { background: var(--warn-bg); color: var(--warn-ink); }
.f-btn { display: inline-flex; align-items: center; gap: 10px; min-height: 48px; padding: 0 18px; border-radius: 6px; background: var(--tile-3); font-weight: 600; font-size: 16px; }
.f-btn--accent { background: var(--accent); color: var(--on-accent); min-width: 260px; justify-content: center; }
.f-row { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.f-who { min-width: 140px; color: var(--soft); }
.f-chip { padding: 3px 10px; border-radius: 4px; background: var(--tile-3); color: var(--text); font-weight: 600; font-size: 14px; }
.f-chip--on { background: var(--accent); color: var(--on-accent); }
.f-chip--soft { background: var(--chip-bg); color: var(--chip-ink); }
.f-audit { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.f-time { color: var(--soft); }
.f-sql { margin: 0; font-family: var(--mono); font-size: 14px; line-height: 1.6; white-space: pre-wrap; overflow-wrap: anywhere; } /* taste-skill-ignore: literal SQL */
.f-sql .k { color: var(--accent-ink); }
.f-sql .bad { color: var(--bad); }

@media (max-width: 860px) {
  .gov__list { gap: 40px; }
  .row { grid-template-columns: 1fr; gap: 16px; }
  .row:nth-child(even) .row__text { order: 0; }
  .row__ui { padding: 20px; min-height: 0; }
  .row:nth-child(even) .row__ui { align-items: flex-end; }
}
</style>

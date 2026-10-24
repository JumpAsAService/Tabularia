<script setup lang="ts">
// Code and containers: four rows, each a short text and one fragment of the interface (the
// script editor in its environment, the assistant's note, a Container node's settings, a flow
// group's grid), the fragment alternating sides, as in the governance list.
const t = useCopy()
const r = t.code.rows
</script>

<template>
  <section id="code" class="code section" aria-labelledby="code-title" data-rise-group>
    <div class="code__head">
      <h2 id="code-title" class="section-title" data-rise>{{ t.code.title }}</h2>
      <p class="code__lead" data-rise>{{ t.code.lead }}</p>
    </div>
    <ul role="list" class="code__list">
      <li class="row" data-rise>
        <div class="row__text">
          <h3>{{ r.python.name }}</h3>
          <p>{{ r.python.line }}</p>
          <NuxtLink to="/docs/scripting" class="row__link">{{ t.code.docsLink }} →</NuxtLink>
        </div>
        <div class="row__ui row__ui--code" data-ui aria-hidden="true">
          <span class="f-env"><span class="f-dot" />Forecasting · Python 3.12 · v4</span>
          <pre class="f-code"><code><span class="k">import</span> os
<span class="k">import</span> polars <span class="k">as</span> pl

sales = pl.scan_parquet(os.environ[<span class="s">"TABULARIA_INPUT_SALES"</span>])
(sales.group_by(<span class="s">"region"</span>)
      .agg(pl.col(<span class="s">"amount"</span>).sum())
      .sink_parquet(os.environ[<span class="s">"TABULARIA_OUTPUT"</span>]))</code></pre>
          <span class="f-pkgs"><span class="f-chip f-chip--soft">polars 1.31</span><span class="f-chip f-chip--soft">scikit-learn</span><span class="f-chip f-chip--soft">group: analysts</span><span class="f-ok">Ruff ✓</span></span>
        </div>
      </li>
      <li class="row" data-rise>
        <div class="row__text">
          <h3>{{ r.assistant.name }}</h3>
          <p>{{ r.assistant.line }}</p>
        </div>
        <div class="row__ui" data-ui aria-hidden="true">
          <span class="f-q">The last run failed: explain why and fix the code.</span>
          <span class="f-a">There is no column «totale»: the amounts are in «amount».</span>
          <span class="f-proposal"><span class="f-file">main.py</span><span class="f-diff"><span class="add">+2</span> <span class="del">−3</span></span><span class="f-btn f-btn--accent f-btn--sm">Apply</span></span>
          <span class="f-note">{{ t.code.assistantNote }}</span>
        </div>
      </li>
      <li class="row" data-rise>
        <div class="row__text">
          <h3>{{ r.containers.name }}</h3>
          <p>{{ r.containers.line }}</p>
          <NuxtLink to="/docs/container-node" class="row__link">{{ t.code.containersLink }} →</NuxtLink>
        </div>
        <div class="row__ui" data-ui aria-hidden="true">
          <span class="f-kv"><span>Registry</span><b>Harbor · team</b></span>
          <span class="f-kv"><span>Image</span><b class="mono">team/forecast:v3</b></span>
          <span class="f-kv"><span>Command</span><b class="mono">python run.py --days 30</b></span>
          <span class="f-kv"><span>Resources</span><b class="mono">500m · 512Mi · 2 retries</b></span>
          <span class="f-status f-status--sm"><svg viewBox="0 0 24 24" width="1em" height="1em" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12.5 4.5 4.5L19 7.5" /></svg>Job succeeded · 41 s</span>
        </div>
      </li>
      <li class="row" data-rise>
        <div class="row__text">
          <h3>{{ r.groups.name }}</h3>
          <p>{{ r.groups.line }}</p>
        </div>
        <div class="row__ui f-grid" data-ui aria-hidden="true">
          <span class="f-step">1</span><span class="f-cell">Refresh · ERP orders</span><span class="f-cell">Refresh · CRM accounts</span>
          <span class="f-step">2</span><span class="f-cell">Flow · Daily sales</span><span class="f-cell f-cell--code">Script · Forecast</span>
          <span class="f-step">3</span><span class="f-cell f-cell--box">Container · Load to the ERP</span><span class="f-cell f-cell--mail">Pushmail · Morning report</span>
        </div>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.code { padding-inline: var(--gutter); }
.code__head { display: grid; gap: 20px; max-width: 760px; }
.code__lead { font-size: var(--fs-body); max-width: 60ch; }
.code__list { list-style: none; margin: 64px 0 0; padding: 0; display: grid; gap: 56px; }
.row { display: grid; grid-template-columns: minmax(0, 5fr) minmax(0, 7fr); gap: 48px; align-items: center; }
.row:nth-child(even) .row__text { order: 2; }
.row__text { display: grid; gap: 8px; justify-items: start; }
.row__text h3 { font-size: var(--fs-statement); line-height: 1.15; letter-spacing: -0.015em; font-weight: 600; }
.row__text p { font-size: var(--fs-body); max-width: 46ch; }
.row__link { font-weight: 600; color: var(--accent-ink); text-decoration: none; }
.row__link:hover { text-decoration: underline; }

/* the fragments, at the interface's own size, on one stage tone */
.row__ui {
  margin: 0; min-height: 160px; padding: 28px; border-radius: var(--radius); background: var(--tile);
  display: flex; flex-direction: column; align-items: flex-start; justify-content: center; gap: 10px; font-size: 15px; overflow: hidden;
}
.mono, .f-code, .f-file { font-family: var(--mono); }
.f-env { display: inline-flex; align-items: center; gap: 8px; font-size: 13px; color: var(--soft); }
.f-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--ok); }
.f-code { margin: 0; font-size: 14px; line-height: 1.6; white-space: pre; overflow-x: auto; max-width: 100%; } /* taste-skill-ignore: literal code */
.f-code .k { color: var(--accent-ink); }
.f-code .s { color: var(--soft); }
.f-pkgs { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.f-chip { padding: 3px 10px; border-radius: 4px; background: var(--tile-3); color: var(--text); font-weight: 600; font-size: 13px; }
.f-chip--soft { background: var(--chip-bg); color: var(--chip-ink); }
.f-ok { font-size: 13px; font-weight: 600; color: var(--ok-ink, var(--ok)); }
.f-q { align-self: flex-end; max-width: 85%; padding: 8px 12px; border-radius: 10px 10px 3px 10px; background: var(--tile-3); }
.f-a { max-width: 90%; }
.f-proposal { display: flex; align-items: center; gap: 12px; padding: 10px 12px; border-radius: 8px; border: 1px solid var(--accent); }
.f-file { font-size: 14px; }
.f-diff { font-family: var(--mono); font-size: 13px; }
.f-diff .add { color: var(--ok-ink, var(--ok)); }
.f-diff .del { color: var(--bad); }
.f-note { font-size: 13px; color: var(--soft); }
.f-btn { display: inline-flex; align-items: center; gap: 10px; min-height: 48px; padding: 0 18px; border-radius: 6px; background: var(--tile-3); font-weight: 600; font-size: 16px; }
.f-btn--accent { background: var(--accent); color: var(--on-accent); }
.f-btn--sm { min-height: 32px; padding: 0 14px; font-size: 14px; }
.f-kv { display: grid; grid-template-columns: 96px minmax(0, 1fr); gap: 12px; width: 100%; align-items: baseline; }
.f-kv > span { color: var(--soft); font-size: 13px; }
.f-kv b { font-weight: 600; overflow-wrap: anywhere; }
.f-status { display: inline-flex; align-items: center; gap: .35em; padding: .3em .7em; border-radius: 6px; background: var(--ok); color: var(--on-ok); font-weight: 600; }
.f-status--sm { font-size: 15px; margin-top: 4px; }
.f-grid { display: grid; grid-template-columns: 28px minmax(0, 1fr) minmax(0, 1fr); gap: 8px; align-items: stretch; }
.f-step { display: grid; place-items: center; font-family: var(--mono); font-size: 13px; color: var(--soft); }
.f-cell { padding: 10px 12px; border-radius: 6px; background: var(--tile-3); font-size: 14px; font-weight: 600; border-left: 3px solid var(--accent); }
.f-cell--code { border-left-color: var(--ok); }
.f-cell--box { border-left-color: var(--soft); }
.f-cell--mail { border-left-color: var(--warn-ink, var(--accent)); }

@media (max-width: 860px) {
  .code__list { gap: 40px; }
  .row { grid-template-columns: 1fr; gap: 16px; }
  .row:nth-child(even) .row__text { order: 0; }
  .row__ui { padding: 20px; min-height: 0; }
  .f-grid { grid-template-columns: 22px minmax(0, 1fr) minmax(0, 1fr); }
  .f-cell { font-size: 13px; padding: 8px 10px; }
}
</style>

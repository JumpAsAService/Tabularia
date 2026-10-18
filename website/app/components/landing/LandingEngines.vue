<script setup lang="ts">
// Text on one side, one working fragment on the other: the engine picker of a flow.
const t = useCopy()
const prod = ref('clickhouse')
const dev = ref('polars')
const sel = computed(() => t.engines.rows.find(r => r.id === prod.value)!)
</script>

<template>
  <section id="engines" class="engines section" aria-labelledby="engines-title" data-rise-group>
    <div class="engines__text">
      <h2 id="engines-title" class="section-title" data-rise>{{ t.engines.title }}</h2>
      <p class="engines__lead" data-rise>{{ t.engines.lead }}</p>
      <p class="engines__detail" data-rise>{{ t.engines.detail }}</p>
      <p class="engines__rules" data-rise>{{ t.engines.rules }}</p>
      <p data-rise><NuxtLink to="/docs/engines/engine-differences" class="link-arrow">{{ t.engines.rulesLink }}</NuxtLink></p>
    </div>

    <div class="engines__ui" data-ui data-rise>
      <p class="ui-h">{{ t.engines.panelTitle }}</p>
      <label class="ui-field">
        <span class="ui-label">{{ t.engines.devLabel }}</span>
        <select v-model="dev" class="ui-select">
          <option v-for="r in t.engines.rows" :key="r.id" :value="r.id">{{ r.name }}</option>
        </select>
      </label>
      <fieldset class="ui-field">
        <legend class="ui-label">{{ t.engines.prodLabel }}</legend>
        <div class="ui-options">
          <label v-for="r in t.engines.rows" :key="r.id" class="ui-opt" :class="{ 'is-on': prod === r.id }">
            <input v-model="prod" type="radio" name="prod-engine" :value="r.id">
            <span class="ui-opt__name">{{ r.name }}</span>
            <span v-if="r.status" class="ui-opt__tag">{{ r.status }}</span>
          </label>
        </div>
      </fieldset>
      <dl class="ui-detail" aria-live="polite">
        <div><dt>{{ t.engines.whereLabel }}</dt><dd>{{ sel.where }}</dd></div>
        <div><dt>{{ t.engines.needsLabel }}</dt><dd>{{ sel.needs }}</dd></div>
        <div><dt>{{ t.engines.billedLabel }}</dt><dd>{{ sel.billed }}</dd></div>
      </dl>
    </div>
  </section>
</template>

<style scoped>
.engines {
  display: grid; grid-template-columns: minmax(0, 5fr) minmax(0, 6fr); gap: 64px; align-items: center;
  padding-inline: var(--gutter);
}
.engines__text { display: grid; gap: 20px; align-content: start; }
.engines__lead { font-size: var(--fs-statement); line-height: 1.25; letter-spacing: -0.01em; max-width: 30ch; }
.engines__detail { font-size: var(--fs-body); max-width: 52ch; }
.engines__rules { font-size: var(--fs-body); max-width: 52ch; color: var(--soft); }

.engines__ui { background: var(--tile); border-radius: var(--radius); padding: 24px; font-size: 15px; max-width: 560px; justify-self: end; width: 100%; }
.ui-h { font-weight: 600; font-size: var(--fs-body); }
.ui-field { margin: 18px 0 0; padding: 0; border: 0; display: grid; gap: 8px; }
.ui-label { font-size: 14px; color: var(--soft); padding: 0; }
.ui-select {
  min-height: 44px; padding: 0 12px; border-radius: 6px; border: 1.5px solid var(--tile-3); background: var(--ground); color: var(--text); font-size: 15px;
}
.ui-options { display: grid; gap: 6px; }
.ui-opt {
  display: flex; align-items: center; gap: 10px; min-height: 46px; padding: 0 14px; border-radius: 6px; cursor: pointer;
  background: var(--ground); border: 1.5px solid transparent; transition: border-color 160ms ease, background-color 160ms ease;
}
.ui-opt:hover { border-color: var(--tile-3); }
.ui-opt.is-on { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 12%, var(--ground)); }
.ui-opt input { accent-color: var(--accent); width: 16px; height: 16px; margin: 0; }
.ui-opt__name { font-weight: 600; }
.ui-opt__tag { margin-left: auto; font-size: 13px; color: var(--muted); }
.ui-detail { margin: 18px 0 0; display: grid; gap: 10px; }
.ui-detail div { display: grid; grid-template-columns: 90px 1fr; gap: 12px; }
.ui-detail dt { color: var(--muted); font-size: 14px; }
.ui-detail dd { margin: 0; }

@media (max-width: 960px) {
  .engines { grid-template-columns: 1fr; gap: 40px; }
  .engines__ui { justify-self: stretch; max-width: none; padding: 18px; }
}
</style>

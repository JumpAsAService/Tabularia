<script setup lang="ts">
const t = useCopy()
const config = useRuntimeConfig()
</script>

<template>
  <section id="install" class="install section" aria-labelledby="install-title" data-rise-group>
    <div class="install__text">
      <h2 id="install-title" class="section-title" data-rise><span>{{ t.install.titleA }}</span> <span>{{ t.install.titleB }}</span></h2>
      <p class="install__lines" data-rise>{{ t.install.lines }}</p>
      <p class="install__lines install__note" data-rise>{{ t.install.release }} <a :href="`${config.public.repo}/blob/main/CHANGELOG.md`" class="link-arrow">{{ t.install.releaseLink }}</a>.</p>
      <p class="install__lines" data-rise>{{ t.install.config }} <NuxtLink to="/docs/configuration" class="link-arrow">{{ t.install.configLink }}</NuxtLink>.</p>
      <p class="install__actions" data-rise>
        <NuxtLink to="/docs/getting-started" class="btn btn--accent">{{ t.install.primary }}</NuxtLink>
        <a :href="`${config.public.repo}/releases`" class="btn">{{ t.install.secondary }}</a>
      </p>
    </div>
    <pre class="install__cmd" :aria-label="t.install.commandLabel" data-rise><code>{{ t.install.command }}</code></pre>
  </section>
</template>

<style scoped>
.install {
  display: grid; grid-template-columns: minmax(0, 5fr) minmax(0, 6fr); gap: clamp(32px, 5vw, 80px); align-items: center;
  padding-inline: var(--gutter);
}
.install__text { display: grid; gap: 22px; }
.install .section-title span { display: block; }
.install__lines { font-size: var(--fs-body); max-width: 46ch; }
.install__note { font-size: var(--fs-small); color: var(--soft); }
.install__actions { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; }
.install__cmd {
  margin: 0; padding: 24px 28px; border-radius: var(--radius); background: var(--tile); color: var(--text);
  font-size: var(--fs-small); line-height: 1.8; overflow-x: auto;
}
@media (max-width: 960px) { .install { grid-template-columns: 1fr; gap: 28px; } .install__cmd { padding: 18px; } }
</style>

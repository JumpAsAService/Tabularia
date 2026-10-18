<script setup lang="ts">
// the lockup files of brand/ (mark + outlined wordmark, proportions per brand/BRAND.md)
import lockupDark from '../../../../brand/lockup-dark.svg?url'
import lockupLight from '../../../../brand/lockup-light.svg?url'

const t = useCopy()
const config = useRuntimeConfig()
const href = (target: string) => ({
  repo: config.public.repo,
  releases: `${config.public.repo}/releases`,
  issues: `${config.public.repo}/issues`,
  dockerhub: config.public.dockerHub,
} as Record<string, string>)[target] || target
const isInternal = (target: string) => target.startsWith('/')
</script>

<template>
  <footer class="foot section">
    <div class="foot__band">
      <img :src="lockupDark" class="foot__lockup foot__lockup--dark" width="1456" height="480" alt="Tabularia">
      <img :src="lockupLight" class="foot__lockup foot__lockup--light" width="1456" height="480" alt="" aria-hidden="true">
    </div>
    <div class="foot__cols">
      <nav v-for="g in t.footer.groups" :key="g.title" :aria-label="g.title">
        <p class="foot__h">{{ g.title }}</p>
        <ul role="list">
          <li v-for="[label, target] in g.links" :key="label">
            <NuxtLink v-if="isInternal(target!)" :to="target!">{{ label }}</NuxtLink>
            <a v-else :href="href(target!)">{{ label }}</a>
          </li>
        </ul>
      </nav>
      <div class="foot__contact">
        <p class="foot__h">{{ t.footer.contactTitle }}</p>
        <p>{{ t.footer.contact }}</p>
        <p class="soft">{{ t.footer.origin }}</p>
      </div>
    </div>
    <div class="foot__last">
      <div>
        <p>{{ t.footer.copyright }}</p>
        <p class="small soft foot__tm">{{ t.footer.trademarks }}</p>
        <p class="small soft foot__tm">{{ t.footer.warranty }} <a :href="`${config.public.repo}/blob/main/LICENSE`">{{ t.footer.licence }}</a>.</p>
      </div>
      <div class="foot__actions">
        <ThemeToggle />
        <a href="#top" class="btn foot__top">{{ t.footer.top }}</a>
      </div>
    </div>
  </footer>
</template>

<style scoped>
.foot { padding: 0 var(--gutter) 8px; }
.foot__band { padding: 0 0 2vw; }
.foot__lockup { display: block; width: 100%; max-width: 1100px; height: auto; }
.foot__lockup--light { display: none; }
@media (prefers-color-scheme: light) {
  :root:not([data-theme="dark"]) .foot__lockup--dark { display: none; }
  :root:not([data-theme="dark"]) .foot__lockup--light { display: block; }
}
:root[data-theme="light"] .foot__lockup--dark { display: none; }
:root[data-theme="light"] .foot__lockup--light { display: block; }
.foot__cols { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)) minmax(0, 1.6fr); gap: 32px; margin-top: 56px; }
.foot__h { font-weight: 600; margin-bottom: 10px; }
.foot__cols ul { list-style: none; margin: 0; padding: 0; display: grid; gap: 4px; }
.foot__cols a { color: var(--soft); text-decoration: none; font-size: var(--fs-small); padding: 4px 0; display: inline-block; }
.foot__cols a:hover { color: var(--text); text-decoration: underline; text-underline-offset: 4px; }
.foot__contact p + p { margin-top: 10px; }
.foot__contact p { font-size: var(--fs-small); line-height: 1.5; max-width: 46ch; }
.foot__last { display: flex; justify-content: space-between; align-items: end; gap: 24px; margin-top: 64px; padding: 16px 0 8px; }
.foot__last p { font-size: var(--fs-small); }
.foot__tm { max-width: 70ch; margin-top: 6px; }
.foot__tm a { color: inherit; text-underline-offset: 3px; }
.foot__tm a:hover { color: var(--text); }
.foot__actions { display: flex; align-items: center; gap: 8px; flex: none; }
.foot__top { min-height: 44px; font-size: var(--fs-small); }
@media (max-width: 860px) {
  .foot__cols { grid-template-columns: 1fr 1fr; }
  .foot__contact { grid-column: 1 / -1; }
  .foot__last { flex-direction: column; align-items: start; }
}
</style>

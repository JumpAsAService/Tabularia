<script setup lang="ts">
const t = useCopy()
useSeoMeta({
  title: 'Tabularia',
  description: t.meta.description,
  ogTitle: t.meta.title,
  ogDescription: t.meta.description,
})
useReveal()
</script>

<template>
  <div class="landing">
    <OrderingField />
    <LandingHero />
    <main id="main" tabindex="-1">
      <LandingIntro />
      <LandingStory />
      <LandingEngines />
      <LandingOpen />
      <LandingStack />
      <LandingGovernance />
      <LandingConnect />
      <LandingInstall />
    </main>
    <LandingFooter />
  </div>
</template>

<style scoped>
.landing { position: relative; }
/* the content sits above the fixed ordering field */
.landing > :not(canvas) { position: relative; z-index: 1; }
main:focus { outline: none; }
</style>

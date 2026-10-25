<script setup lang="ts">
// Home (Explore): the projects' tree with the folder's content. The administration lives
// on its own page (/admin), linked from the navbar.
</script>

<template>
  <AppShell>
    <BannerBar />
    <ProjectBrowser class="grow" />
  </AppShell>
</template>

<style scoped>
.grow { flex: 1; }
</style>

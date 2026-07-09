<script setup lang="ts">
// Home (Explore): l'albero dei progetti con il contenuto della cartella.
// L'amministrazione vive nella sua pagina (/admin), linkata dalla navbar.
</script>

<template>
  <AppShell>
    <ProjectBrowser class="grow" />
  </AppShell>
</template>

<style scoped>
.grow { flex: 1; }
</style>

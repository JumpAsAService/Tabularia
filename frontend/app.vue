<script setup lang="ts">
// explicit import: in the container new components are not scanned
import IdleWarning from '~/components/ui/IdleWarning.vue'
</script>

<template>
  <!-- while a page's code arrives: a thin bar at the top, only past 100 ms, so a click is
       never answered by a still screen -->
  <NuxtLoadingIndicator color="var(--accent)" :height="2" :throttle="100" />
  <NuxtPage />
  <ToastHost />
  <IdleWarning />
</template>

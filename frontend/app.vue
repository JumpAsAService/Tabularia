<script setup lang="ts">
// explicit import: in the container new components are not scanned
import IdleWarning from '~/components/ui/IdleWarning.vue'
</script>

<template>
  <NuxtPage />
  <ToastHost />
  <IdleWarning />
</template>

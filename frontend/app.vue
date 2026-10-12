<script setup lang="ts">
// import esplicito: nel container i componenti nuovi non vengono scansionati
import IdleWarning from '~/components/ui/IdleWarning.vue'
</script>

<template>
  <NuxtPage />
  <ToastHost />
  <IdleWarning />
</template>

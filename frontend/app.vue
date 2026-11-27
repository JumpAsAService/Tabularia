<script setup lang="ts">
// explicit import: in the container new components are not scanned
import IdleWarning from '~/components/ui/IdleWarning.vue'
import AppKeys from '~/components/ui/AppKeys.vue'
import TourHost from '~/components/tour/TourHost.vue'
import ConfirmHost from '~/components/ui/ConfirmHost.vue'
</script>

<template>
  <!-- while a page's code arrives: a thin bar at the top, only past 100 ms, so a click is
       never answered by a still screen -->
  <NuxtLoadingIndicator color="var(--accent)" :height="2" :throttle="100" />
  <NuxtPage />
  <ToastHost />
  <!-- the questions of useConfirm(), in place of the browser's confirm() -->
  <ConfirmHost />
  <IdleWarning />
  <!-- the keys of every page: «g» and a letter, «,» «.», «/», «?» -->
  <AppKeys />
  <!-- the guided tour over the Sample folder's own objects (components/tour/TourHost.vue): idle until it is asked for -->
  <!-- in the browser only: it measures the window and listens to it, and the server that renders the
       first page (the sign-in, which the health probes ask for) has neither -->
  <ClientOnly><TourHost /></ClientOnly>
</template>

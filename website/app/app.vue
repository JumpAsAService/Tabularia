<script setup lang="ts">
const config = useRuntimeConfig()
const route = useRoute()
useHead({
  titleTemplate: (t?: string) => (t && t !== 'Tabularia' ? `${t} · Tabularia` : 'Tabularia · Messy in. Trusted out.'),
  link: [{ rel: 'canonical', href: computed(() => config.public.siteUrl.replace(/\/$/, '') + route.path.replace(/\/$/, '') + (route.path === '/' ? '/' : '')) }],
})
useSeoMeta({
  ogSiteName: 'Tabularia',
  ogType: 'website',
  ogImage: `${config.public.siteUrl}og.png`,
  ogImageWidth: 1200,
  ogImageHeight: 630,
  ogImageAlt: 'Tabularia. Messy in. Trusted out.',
  twitterCard: 'summary_large_image',
  twitterImage: `${config.public.siteUrl}og.png`,
})
</script>

<template>
  <div>
    <a href="#main" class="skip-link">Skip to content</a>
    <NuxtLayout>
      <NuxtPage />
    </NuxtLayout>
  </div>
</template>

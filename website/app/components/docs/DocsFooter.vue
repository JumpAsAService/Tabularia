<script setup lang="ts">
const config = useRuntimeConfig()
</script>

<template>
  <footer class="docs-footer">
    <p>Tabularia is open source under the AGPL-3.0. © 2025–2026 Leonardo Trivelli.</p>
    <p>
      <a :href="config.public.repo">Source on GitHub</a> ·
      <a :href="`${config.public.repo}/issues`">Issues</a> ·
      <a :href="config.public.dockerHub">Images on Docker Hub</a> ·
      <a :href="`${config.app.baseURL}third-party-notices.txt`">Open source licences</a> ·
      <a :href="config.public.privacyPolicy" class="iubenda-nostyle iubenda-noiframe iubenda-embed" title="Privacy Policy">Privacy Policy</a> ·
      <a :href="config.public.cookiePolicy" class="iubenda-nostyle iubenda-noiframe iubenda-embed" title="Cookie Policy">Cookie Policy</a>
    </p>
    <p v-if="config.public.analytics" class="docs-footer__note">
      Visits are counted with Cloudflare Web Analytics: no cookies, no personal data.
    </p>
  </footer>
</template>

<style scoped>
.docs-footer {
  display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px 24px;
  margin-top: 96px; padding: 24px 16px 32px; background: var(--tile); color: var(--soft); font-size: 14px;
}
.docs-footer a { color: var(--soft); }
.docs-footer a:hover { color: var(--text); }
.docs-footer__note { flex-basis: 100%; margin: 0; font-size: 13px; }
</style>

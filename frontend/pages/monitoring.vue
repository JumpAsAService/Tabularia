<script setup lang="ts">
// Monitoring: the Grafana dashboard "Data Prep — Overview" embedded in kiosk mode (no
// Grafana chrome, only the panels). Requires GF_SECURITY_ALLOW_EMBEDDING + anonymous
// Viewer (see docker-compose).
import { computed, watchEffect } from 'vue'
import { ExternalLink } from 'lucide-vue-next'

const { user } = useAuth()
const router = useRouter()
const config = useRuntimeConfig()

// the content is telemetry, not data: the guard is only UX consistent with /admin
watchEffect(() => {
  if (user.value && !user.value.is_superuser && !user.value.is_observer) router.replace('/')
})

// An OBSERVER reads the page; the commands stay the administrator's.
// `is_observer` from /auth/me is EFFECTIVE: it accounts for the groups.
const canSee = computed(() => !!user.value?.is_superuser || !!user.value?.is_observer)
const canWrite = computed(() => !!user.value?.is_superuser)

const dashboardUrl = computed(
  () =>
    `${config.public.grafanaUrl}/d/dataprep-overview/data-prep-overview` +
    `?orgId=1&kiosk&theme=dark&refresh=10s`,
)
const grafanaHome = computed(() => config.public.grafanaUrl)
</script>

<template>
  <AppShell>
    <template v-if="canSee">
      <div class="mon-head">
        <p class="muted">
          {{ $t('monitoring.liveMetrics') }}
        </p>
        <a v-if="canWrite" :href="grafanaHome" target="_blank" rel="noopener" class="ext">
          <ExternalLink :size="13" /> {{ $t('monitoring.openGrafana') }}
        </a>
      </div>
      <iframe
        :src="dashboardUrl"
        class="grafana"
        :title="$t('monitoring.dashboardTitle')"
        loading="lazy"
      />
    </template>
  </AppShell>
</template>

<style scoped>
.mon-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
}
.mon-head p { margin: 0; font-size: 12.5px; }
.ext {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  color: var(--muted);
  text-decoration: none;
  white-space: nowrap;
}
.ext:hover { color: var(--accent); }
.grafana {
  flex: 1;
  width: 100%;
  min-height: 70vh;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--panel);
}
</style>

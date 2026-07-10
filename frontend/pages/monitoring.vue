<script setup lang="ts">
// Monitoring (solo superuser): la dashboard Grafana "Data Prep — Overview"
// embeddata in kiosk mode (niente chrome di Grafana, solo i pannelli).
// Richiede GF_SECURITY_ALLOW_EMBEDDING + anonimo Viewer (vedi docker-compose).
import { computed, watchEffect } from 'vue'
import { ExternalLink } from 'lucide-vue-next'

const { user } = useAuth()
const router = useRouter()
const config = useRuntimeConfig()

// il contenuto è telemetria, non dati: la guardia è solo UX coerente con /admin
watchEffect(() => {
  if (user.value && !user.value.is_superuser) router.replace('/')
})

const dashboardUrl = computed(
  () =>
    `${config.public.grafanaUrl}/d/dataprep-overview/data-prep-overview` +
    `?orgId=1&kiosk&theme=dark&refresh=10s`,
)
const grafanaHome = computed(() => config.public.grafanaUrl)
</script>

<template>
  <AppShell>
    <template v-if="user?.is_superuser">
      <div class="mon-head">
        <p class="muted">
          Live metrics from the stack (VictoriaMetrics + Grafana), auto-refresh 10s.
        </p>
        <a :href="grafanaHome" target="_blank" rel="noopener" class="ext">
          <ExternalLink :size="13" /> Open Grafana
        </a>
      </div>
      <iframe
        :src="dashboardUrl"
        class="grafana"
        title="Grafana — Data Prep Overview"
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

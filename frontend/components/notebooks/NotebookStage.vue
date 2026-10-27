<script setup lang="ts">
// What fills a notebook's app or editor page while marimo is not on screen: the pod
// starting (with the seconds passing), why it could not start, or why this page will not
// start it. The page passes its own buttons in the default slot.
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { CircleAlert, Info, LoaderCircle } from 'lucide-vue-next'

const props = defineProps<{
  kind: 'starting' | 'failed' | 'info'
  title: string
  detail?: string
  /** seconds since the start, while starting */
  elapsed?: number
}>()

const { t } = useI18n()
// past a minute the reason is almost always the image being fetched on a new node
const slow = computed(() => props.kind === 'starting' && (props.elapsed ?? 0) >= 60)
</script>

<template>
  <section class="stage" :class="kind" :aria-busy="kind === 'starting'">
    <div class="box">
      <LoaderCircle v-if="kind === 'starting'" :size="22" class="spin icon" aria-hidden="true" />
      <CircleAlert v-else-if="kind === 'failed'" :size="22" class="icon" aria-hidden="true" />
      <Info v-else :size="22" class="icon" aria-hidden="true" />
      <!-- the heading is announced once; the ticking seconds are not (they would be read
           every second) -->
      <h2 :role="kind === 'failed' ? 'alert' : 'status'">{{ title }}</h2>
      <p v-if="kind === 'starting'" class="secs muted" aria-hidden="true">
        {{ t('notebook.session.seconds', { n: elapsed ?? 0 }) }}
      </p>
      <p v-if="detail" class="detail">{{ detail }}</p>
      <p v-if="slow" class="muted small">{{ t('notebook.session.slow') }}</p>
      <div v-if="$slots.default" class="acts"><slot /></div>
    </div>
  </section>
</template>

<style scoped>
.stage {
  flex: 1;
  min-height: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px 16px;
  background: var(--bg);
}
.box {
  max-width: 520px;
  width: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  text-align: center;
}
.box h2 { margin: 0; font-size: 16px; font-weight: 600; }
.box p { margin: 0; }
.icon { color: var(--accent); }
.failed .icon { color: var(--danger); }
.info .icon { color: var(--muted); }
.secs { font-variant-numeric: tabular-nums; font-size: 12.5px; }
.detail { font-size: 13px; overflow-wrap: anywhere; }
.failed .detail { color: var(--danger); }
.small { font-size: 12px; }
.acts { display: flex; flex-wrap: wrap; justify-content: center; gap: 8px; margin-top: 8px; }
@media (prefers-reduced-motion: reduce) {
  .spin { animation: none; }
}
</style>

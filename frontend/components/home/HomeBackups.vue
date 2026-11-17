<script setup lang="ts">
// The «Backups» widget, for administrators: the last backup of Tabularia's database, how it went
// and when the next one is due (Administration → Backups' own state).
import { onMounted, ref } from 'vue'
import { CheckCircle2, CircleSlash, XCircle } from 'lucide-vue-next'
import { bytesText, durationText } from '~/composables/homeLayout'
import { useBackups, type BackupState } from '~/composables/useBackups'
import { useFormat } from '~/composables/useFormat'

defineProps<{ options: Record<string, any> }>()
const api = useBackups()
const fmt = useFormat()
const b = ref<BackupState | null>(null)
const failed = ref(false)
async function read() {
  try { b.value = await api.get(); failed.value = false } catch { failed.value = true }
}
onMounted(read)
</script>

<template>
  <div class="bk">
    <p v-if="failed && !b" class="msg" role="alert">{{ $t('home.backups.failed') }} <button type="button" class="retry" @click="read">{{ $t('home.retry') }}</button></p>
    <p v-else-if="!b" class="msg"><span class="sk" style="width: 50%" /></p>
    <p v-else-if="!b.available" class="state off"><CircleSlash :size="16" aria-hidden="true" /> {{ $t('home.backups.unavailable') }}</p>
    <template v-else>
      <p v-if="b.last_error_at && (!b.last_ok_at || b.last_error_at > b.last_ok_at)" class="state ko"><XCircle :size="16" aria-hidden="true" /> {{ $t('home.backups.lastFailed', { when: fmt.ago(b.last_error_at) }) }}</p>
      <p v-else-if="b.last_ok_at" class="state ok"><CheckCircle2 :size="16" aria-hidden="true" /> {{ $t('home.backups.lastOk', { when: fmt.ago(b.last_ok_at) }) }}</p>
      <p v-else class="state off"><CircleSlash :size="16" aria-hidden="true" /> {{ $t('home.backups.never') }}</p>
      <p v-if="b.last_ok_at" class="sub">{{ bytesText(b.last_size) }} · {{ durationText(b.last_seconds) }}</p>
      <p class="sub">{{ b.enabled && b.next_at ? $t('home.backups.next', { when: fmt.dateTime(b.next_at) }) : $t('home.backups.disabled') }}</p>
      <NuxtLink to="/admin" class="more">{{ $t('home.backups.open') }}</NuxtLink>
    </template>
  </div>
</template>

<style scoped>
.bk { display: flex; flex-direction: column; gap: 6px; height: 100%; overflow: auto; }
.msg { margin: 2px 0; color: var(--muted); font-size: 13px; }
.msg .sk { display: inline-block; height: 12px; }
.retry { min-height: 0; padding: 0 2px; border: none; background: none; color: var(--accent); font-size: 13px; text-decoration: underline; }
.state { display: flex; align-items: center; gap: 8px; margin: 0; font-size: 14px; font-weight: 600; }
.state.ok svg { color: var(--accent-2); }
.state.ko { color: var(--danger-text, var(--danger)); }
.state.off { color: var(--muted); font-weight: 500; }
.sub { margin: 0; color: var(--muted); font-size: 12.5px; }
.more { margin-top: auto; color: var(--accent); font-size: 12.5px; text-decoration: none; }
.more:hover { text-decoration: underline; }
</style>

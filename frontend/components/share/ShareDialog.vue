<script setup lang="ts">
// «Share» in the viewer of a report or a dashboard (docs/specs/share-links.md): a link for one
// person outside Tabularia, with a temporary password they replace at the first entry, or a link
// embedded in the pages of listed origins with no password. The link is shown once (the gateway
// keeps only its hash); below, the links of this object, with a new temporary password and Revoke.
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Check, Copy, KeyRound, Link2, LoaderCircle, Plus, RefreshCw, ShieldAlert, Trash2, Wand2 } from 'lucide-vue-next'
import Modal from '~/components/ui/Modal.vue'
import { errMessage } from '~/composables/useApi'
import { useFormat } from '~/composables/useFormat'
import type { ReportParams } from '~/composables/useReports'
import {
  dayAfter, endOfDay, generatePassword, iframeSnippet, originsOf, paramsForLink, passwordProblem, shareUrl, useShareLinks,
  type ShareInfo, type ShareKind, type ShareLink,
} from '~/composables/shareLinks'

const props = defineProps<{ open: boolean; kind: ShareKind; objectId: number; objectName: string; params: ReportParams }>()
const emit = defineEmits<{ (e: 'close'): void }>()

const { t } = useI18n()
const fmt = useFormat()
const toast = useToast()
const api = useShareLinks()

const info = ref<ShareInfo | null>(null)
const loadError = ref('')
async function load() {
  try {
    info.value = await api.of(props.kind, props.objectId)
    loadError.value = ''
    if (!info.value.links.length) creating.value = true
  } catch (e) {
    loadError.value = errMessage(e)
  }
}
onMounted(load)

// ── a new link ───────────────────────────────────────────────────────────────
const creating = ref(false)
const label = ref('')
const mode = ref<'password' | 'embed'>('password')
const password = ref(generatePassword())
const originsText = ref('')
const confirmed = ref(false)
const expires = ref(dayAfter(30))
const downloads = ref<string[]>([])
const busy = ref(false)
const formError = ref('')
const maxDay = computed(() => dayAfter(info.value?.max_days ?? 90))
const minDay = computed(() => dayAfter(1))
const origins = computed(() => originsOf(originsText.value))
const pwProblem = computed(() => (mode.value === 'password' ? passwordProblem(password.value) : ''))
const values = computed(() => paramsForLink(props.params))
const valuesSaid = computed(() => Object.entries(values.value).map(([k, v]) => `${k} = ${v.join(', ')}`).join(' · '))
const canCreate = computed(() => !busy.value && !!expires.value && (mode.value === 'password'
  ? !pwProblem.value
  : origins.value.origins.length > 0 && !origins.value.bad.length && confirmed.value))

const created = ref<{ url: string; embed: boolean; password: string } | null>(null)
async function create() {
  if (!canCreate.value) return
  busy.value = true
  formError.value = ''
  try {
    const out = await api.create({
      kind: props.kind, object_id: props.objectId, label: label.value.trim(), expires_at: endOfDay(expires.value),
      embeddable: mode.value === 'embed', origins: origins.value.origins, confirmed: confirmed.value,
      password: mode.value === 'password' ? password.value : '', params: values.value, downloads: downloads.value,
    })
    const base = info.value?.public_base_url || undefined
    created.value = { url: shareUrl(out.token, base), embed: mode.value === 'embed', password: mode.value === 'password' ? password.value : '' }
    creating.value = false
    label.value = ''
    password.value = generatePassword()
    confirmed.value = false
    await load()
  } catch (e) {
    formError.value = errMessage(e)
  } finally {
    busy.value = false
  }
}

const copied = ref('')
async function copy(what: string, text: string) {
  try {
    await navigator.clipboard.writeText(text)
    copied.value = what
    setTimeout(() => { if (copied.value === what) copied.value = '' }, 2000)
  } catch { /* not over HTTPS: the text stays selectable */ }
}
const snippet = computed(() => (created.value?.embed ? iframeSnippet(created.value.url, props.objectName) : ''))

// ── the links of this object ─────────────────────────────────────────────────
const resetting = ref<number | null>(null)
const newPw = ref('')
const revoking = ref<number | null>(null)
function startReset(l: ShareLink) {
  resetting.value = l.id
  newPw.value = generatePassword()
}
async function reset(l: ShareLink) {
  if (passwordProblem(newPw.value)) return
  try {
    await api.newPassword(l.id, newPw.value)
    toast.success(t('share.passwordReset'))
    resetting.value = null
    await load()
  } catch (e) {
    toast.error(errMessage(e))
  }
}
async function revoke(l: ShareLink) {
  try {
    await api.revoke(l.id)
    toast.success(t('share.revoked'))
    revoking.value = null
    await load()
  } catch (e) {
    toast.error(errMessage(e))
  }
}
const stateLabel = (l: ShareLink) => (l.suspended ? t(`share.suspended_${l.suspended}`) : t(`share.state_${l.state}`))
const downloadLabel = (f: string) => ({ pdf: 'PDF', xlsx: 'Excel', png: t('share.image') }[f] ?? f)
</script>

<template>
  <Modal :open="open" :title="$t('share.dialogTitle', { name: objectName })" :width="720" @close="emit('close')">
    <div class="share">
      <p class="lead">{{ $t(kind === 'report' ? 'share.leadReport' : 'share.leadDashboard') }}</p>
      <p v-if="loadError" class="err" role="alert">{{ loadError }}</p>
      <p v-else-if="info && !info.enabled" class="note" role="status"><ShieldAlert :size="14" /> {{ $t('share.disabled') }}</p>

      <!-- the link just made: shown once -->
      <section v-if="created" class="made" role="status" aria-live="polite">
        <p class="made-head"><Link2 :size="14" /> {{ $t('share.madeTitle') }}</p>
        <div class="row">
          <input class="mono" type="text" readonly :value="created.url" :aria-label="$t('share.link')" @focus="($event.target as HTMLInputElement).select()" />
          <button type="button" class="copy" @click="copy('url', created.url)">
            <Check v-if="copied === 'url'" :size="14" /><Copy v-else :size="14" /> {{ copied === 'url' ? $t('share.copied') : $t('share.copy') }}
          </button>
        </div>
        <template v-if="created.embed">
          <span class="lbl">{{ $t('share.snippet') }}</span>
          <div class="row">
            <textarea class="mono" readonly rows="3" :value="snippet" :aria-label="$t('share.snippet')" />
            <button type="button" class="copy" @click="copy('snippet', snippet)">
              <Check v-if="copied === 'snippet'" :size="14" /><Copy v-else :size="14" /> {{ copied === 'snippet' ? $t('share.copied') : $t('share.copy') }}
            </button>
          </div>
        </template>
        <template v-else>
          <span class="lbl">{{ $t('share.temporaryPassword') }}</span>
          <div class="row">
            <input class="mono" type="text" readonly :value="created.password" :aria-label="$t('share.temporaryPassword')" />
            <button type="button" class="copy" @click="copy('pw', created.password)">
              <Check v-if="copied === 'pw'" :size="14" /><Copy v-else :size="14" /> {{ copied === 'pw' ? $t('share.copied') : $t('share.copy') }}
            </button>
          </div>
          <p class="hint">{{ $t('share.sendApart') }}</p>
        </template>
        <p class="hint">{{ $t('share.shownOnce') }}</p>
        <button type="button" class="done" @click="created = null">{{ $t('share.done') }}</button>
      </section>

      <!-- a new link -->
      <section v-if="info?.can_create && creating && !created" class="form" :aria-label="$t('share.newLink')">
        <label class="field">
          <span class="lbl">{{ $t('share.label') }}</span>
          <input v-model="label" type="text" maxlength="200" :placeholder="$t('share.labelPlaceholder')" />
        </label>

        <div v-roving class="field" role="radiogroup" :aria-label="$t('share.mode')">
          <span class="lbl">{{ $t('share.mode') }}</span>
          <div class="seg">
            <button type="button" role="radio" :aria-checked="mode === 'password'" :class="{ on: mode === 'password' }" @click="mode = 'password'">
              <KeyRound :size="14" /> {{ $t('share.modePassword') }}
            </button>
            <button type="button" role="radio" :aria-checked="mode === 'embed'" :class="{ on: mode === 'embed' }" @click="mode = 'embed'">
              <Link2 :size="14" /> {{ $t('share.modeEmbed') }}
            </button>
          </div>
        </div>

        <label v-if="mode === 'password'" class="field">
          <span class="lbl">{{ $t('share.temporaryPassword') }}</span>
          <span class="row">
            <input v-model="password" class="mono" type="text" autocomplete="off" spellcheck="false" :aria-invalid="!!pwProblem" />
            <button type="button" :title="$t('share.generate')" @click="password = generatePassword()"><Wand2 :size="14" /> {{ $t('share.generate') }}</button>
          </span>
          <span class="hint" :class="{ bad: !!pwProblem && password.length > 0 }">
            {{ pwProblem && password.length ? $t(`share.pw_${pwProblem}`) : $t('share.passwordHint') }}
          </span>
        </label>

        <template v-else>
          <label class="field">
            <span class="lbl">{{ $t('share.origins') }}</span>
            <textarea v-model="originsText" class="mono" rows="3" spellcheck="false" :placeholder="'https://intranet.example.com'" :aria-invalid="origins.bad.length > 0" />
            <span class="hint" :class="{ bad: origins.bad.length > 0 }">
              {{ origins.bad.length ? $t('share.originsBad', { list: origins.bad.join(', ') }) : $t('share.originsHint') }}
            </span>
          </label>
          <label class="confirm">
            <input v-model="confirmed" type="checkbox" />
            <span>{{ $t('share.embedConfirm') }}</span>
          </label>
        </template>

        <div class="two">
          <label class="field">
            <span class="lbl">{{ $t('share.expires') }}</span>
            <input v-model="expires" type="date" :min="minDay" :max="maxDay" required />
            <span class="hint">{{ $t('share.expiresHint', { n: info.max_days }) }}</span>
          </label>
          <fieldset v-if="info.downloads.length" class="field">
            <legend class="lbl">{{ $t('share.downloads') }}</legend>
            <label v-for="f in info.downloads" :key="f" class="check">
              <input v-model="downloads" type="checkbox" :value="f" /> {{ downloadLabel(f) }}
            </label>
          </fieldset>
        </div>

        <p class="hint">{{ valuesSaid ? $t('share.opensWith', { values: valuesSaid }) : $t('share.opensWithDefaults') }} {{ $t('share.liveRights') }}</p>
        <p v-if="formError" class="err" role="alert">{{ formError }}</p>
        <div class="actions">
          <button v-if="info.links.length" type="button" @click="creating = false">{{ $t('share.cancel') }}</button>
          <button type="button" class="primary" :disabled="!canCreate" @click="create">
            <LoaderCircle v-if="busy" :size="14" class="spin" /><Plus v-else :size="14" /> {{ $t('share.create') }}
          </button>
        </div>
      </section>

      <!-- the links of this object -->
      <section v-if="info && info.links.length" class="links">
        <div class="links-head">
          <h3>{{ $t('share.linksTitle', { n: info.links.length }) }}</h3>
          <button v-if="info.can_create && !creating" type="button" class="mini" @click="creating = true; created = null"><Plus :size="13" /> {{ $t('share.newLink') }}</button>
        </div>
        <ul>
          <li v-for="l in info.links" :key="l.id" :class="['link', l.state, { off: !!l.suspended }]">
            <div class="link-main">
              <span class="name">{{ l.label || $t('share.noLabel') }}</span>
              <span class="mono muted">…{{ l.hint }}</span>
              <span class="chip">{{ l.embeddable ? $t('share.modeEmbed') : $t('share.modePassword') }}</span>
              <span class="chip state">{{ stateLabel(l) }}</span>
            </div>
            <div class="link-meta muted">
              {{ $t('share.meta', { by: l.created_by?.full_name || l.created_by?.email || '—', expires: fmt.date(l.expires_at), n: l.openings }) }}
              <template v-if="l.last_opened_at"> · {{ $t('share.lastOpened', { when: fmt.dateTime(l.last_opened_at) }) }}</template>
            </div>
            <div v-if="l.state !== 'revoked' && l.state !== 'expired'" class="link-actions">
              <template v-if="resetting === l.id">
                <input v-model="newPw" class="mono" type="text" autocomplete="off" :aria-label="$t('share.temporaryPassword')" />
                <button type="button" class="mini" @click="copy(`r${l.id}`, newPw)"><Copy :size="12" /> {{ copied === `r${l.id}` ? $t('share.copied') : $t('share.copy') }}</button>
                <button type="button" class="mini primary" :disabled="!!passwordProblem(newPw)" @click="reset(l)">{{ $t('share.setPassword') }}</button>
                <button type="button" class="mini" @click="resetting = null">{{ $t('share.cancel') }}</button>
              </template>
              <template v-else-if="revoking === l.id">
                <span class="warn">{{ $t('share.revokeConfirm') }}</span>
                <button type="button" class="mini danger" @click="revoke(l)">{{ $t('share.revoke') }}</button>
                <button type="button" class="mini" @click="revoking = null">{{ $t('share.cancel') }}</button>
              </template>
              <template v-else>
                <button v-if="!l.embeddable" type="button" class="mini" @click="startReset(l)"><RefreshCw :size="12" /> {{ $t('share.newPassword') }}</button>
                <button type="button" class="mini" @click="revoking = l.id"><Trash2 :size="12" /> {{ $t('share.revoke') }}</button>
              </template>
            </div>
          </li>
        </ul>
      </section>
    </div>
    <template #footer>
      <span class="grow" />
      <button type="button" @click="emit('close')">{{ $t('share.close') }}</button>
    </template>
  </Modal>
</template>

<style scoped>
.share { display: flex; flex-direction: column; gap: 16px; }
.lead { margin: 0; line-height: 1.5; color: var(--text); max-width: 70ch; }
.note { margin: 0; display: flex; align-items: center; gap: 6px; color: var(--warn, var(--muted)); font-size: 13px; }
.err { margin: 0; color: var(--danger); font-size: 13px; }
.lbl { font-size: 12px; font-weight: 500; color: var(--muted); }
.hint { font-size: 12px; color: var(--muted); margin: 0; line-height: 1.45; }
.hint.bad, .warn { color: var(--danger); }
.form, .made { display: flex; flex-direction: column; gap: 12px; }
.made { padding: 12px 14px; border: 1px solid color-mix(in srgb, var(--accent) 40%, var(--border)); border-radius: 10px; background: var(--tint-accent); }
.made-head { margin: 0; display: flex; align-items: center; gap: 6px; font-weight: 600; }
.done { align-self: flex-start; }
.field { display: flex; flex-direction: column; gap: 6px; border: 0; padding: 0; margin: 0; min-width: 0; }
.row { display: flex; gap: 8px; align-items: stretch; }
.row input, .row textarea { flex: 1; min-width: 0; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
textarea { resize: vertical; }
.copy { flex-shrink: 0; white-space: nowrap; }
.seg { display: flex; gap: 6px; flex-wrap: wrap; }
.seg button.on { border-color: var(--accent); background: var(--tint-accent); color: var(--accent-hi); }
.confirm { display: flex; gap: 8px; align-items: flex-start; font-size: 13px; line-height: 1.45; }
.confirm input { margin-top: 3px; }
.two { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 560px) { .two { grid-template-columns: 1fr; } }
.check { display: inline-flex; gap: 6px; align-items: center; font-size: 13px; margin-right: 12px; }
.actions { display: flex; justify-content: flex-end; gap: 8px; }
.links { display: flex; flex-direction: column; gap: 8px; }
.links-head { display: flex; align-items: center; justify-content: space-between; }
.links h3 { margin: 0; font-size: 13px; font-weight: 600; }
.links ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.link { padding: 10px 12px; border: 1px solid var(--border-soft); border-radius: 10px; display: flex; flex-direction: column; gap: 4px; }
.link.revoked, .link.expired, .link.off { opacity: 0.7; }
.link-main { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.name { font-weight: 600; overflow-wrap: anywhere; }
.chip { font-size: 11px; padding: 1px 8px; border-radius: 999px; border: 1px solid var(--border); color: var(--muted); }
.link.active .chip.state { color: var(--accent-2); border-color: color-mix(in srgb, var(--accent-2) 45%, var(--border)); }
.link.locked .chip.state, .link.off .chip.state { color: var(--danger); border-color: color-mix(in srgb, var(--danger) 45%, var(--border)); }
.link-meta { font-size: 12px; }
.link-actions { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; margin-top: 4px; }
.link-actions input { width: 14em; }
.muted { color: var(--muted); }
.grow { flex: 1; }
.spin { animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spin { animation: none; } }
</style>

<script setup lang="ts">
// A report or a dashboard opened with a share link (docs/specs/share-links.md), with no account:
// the link's password (the first time, the temporary one, then one's own), then the viewer the
// creator would see, with their rights, live. An embeddable link asks no password and opens only
// inside a frame of an allowed page: the server writes the page's frame-ancestors and refuses it
// as a page of its own (server/plugins/csp.ts).
//
// The viewer is the app's own (pages/report.vue, pages/dashboard.vue), hosted: this page gives it
// the object, its address and the link's routes (composables/viewerHost.ts).
import { computed, nextTick, onMounted, provide, ref, shallowReactive } from 'vue'
import { useI18n } from 'vue-i18n'
import { FileChartColumn, KeyRound, LoaderCircle, LockKeyhole } from 'lucide-vue-next'
import ReportView from '~/pages/report.vue'
import DashboardView from '~/pages/dashboard.vue'
import { passwordProblem, PASSWORD_MIN, useShareClient, type ShareAbout } from '~/composables/shareLinks'
import { shareDashboardsApi, shareReportsApi, VIEWER_HOST, type ViewerHost } from '~/composables/viewerHost'
import { THEMES, useTheme, type Theme } from '~/composables/useTheme'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const token = String(route.params.token ?? '')
const client = useShareClient(token)
const { theme, setTheme } = useTheme()


// what the server knows before any script: whether the link may be framed, and how the page was asked
interface Frame { available: boolean; embeddable: boolean; dest: string; unknown?: boolean }
const frame = useState<Frame | null>(`share-frame-${token}`, () => null)
if (import.meta.server) {
  const event = useRequestEvent()
  const dest = String(event?.node.req.headers['sec-fetch-dest'] ?? '')
  const internal = String(useRuntimeConfig().gatewayInternalUrl || '').replace(/\/+$/, '')
  let got = { available: false, embeddable: false, frame_ancestors: "'none'" }
  // without the gateway's inside address the page cannot know: framed by nobody, and the browser asks
  const unknown = !internal
  if (unknown) got = { available: true, embeddable: false, frame_ancestors: "'none'" }
  else if (/^[A-Za-z0-9_-]{43}$/.test(token)) {
    try {
      const url: string = `${internal}/share/${token}/frame`
      got = await $fetch<typeof got>(url, { query: { dest }, timeout: 5000 })
    } catch { /* the gateway does not answer: framed by nobody */ }
  }
  if (event) event.context.shareFrame = got
  frame.value = { available: got.available, embeddable: got.embeddable, dest, unknown }
  // an embeddable link asked as a page of its own, or a link that is not there: the same neutral page
  if (!got.available || (got.embeddable && dest !== 'iframe')) {
    if (event) setResponseStatus(event, 404)
  }
}

type Step = 'checking' | 'password' | 'choose' | 'ready' | 'gone'
const step = ref<Step>('checking')
const about = ref<ShareAbout | null>(null)
const password = ref('')
const again = ref('')
const error = ref('')
const busy = ref(false)
const pwInput = ref<HTMLInputElement | null>(null)

useHead({
  title: () => (about.value ? about.value.name : t('share.pageTitle')),
  meta: [
    { name: 'robots', content: 'noindex, nofollow' },
    { name: 'referrer', content: 'no-referrer' },
  ],
})

const host = shallowReactive<ViewerHost>({ path: route.path, objectId: 1, downloads: [] })
provide(VIEWER_HOST, host)

function statusOf(e: any): number { return e?.response?.status ?? e?.statusCode ?? 0 }

async function enter() {
  try {
    const got = await client.about()
    host.downloads = got.downloads ?? []
    if (got.kind === 'dashboard') {
      host.dashboards = shareDashboardsApi(client, got, 1)
      host.reports = shareReportsApi(client, got, 1)
    } else {
      host.reports = shareReportsApi(client, got, 1)
    }
    // the values it opens with, unless the address already holds some
    const holds = Object.keys(route.query).length > 0
    if (!holds && Object.keys(got.params ?? {}).length) await router.replace({ path: route.path, query: got.params })
    about.value = got
    step.value = 'ready'
  } catch (e) {
    const s = statusOf(e)
    if (s === 401) {
      step.value = 'password'
      focusField()
    } else step.value = 'gone'
  }
}

function focusField() {
  nextTick(() => pwInput.value?.focus())
}

async function unlock() {
  if (!password.value || busy.value) return
  busy.value = true
  error.value = ''
  try {
    const got = await client.open(password.value)
    password.value = ''
    if (got.change_password) {
      step.value = 'choose'
      focusField()
      return
    }
    await enter()
  } catch (e: any) {
    const s = statusOf(e)
    if (s === 404) step.value = 'gone'
    else if (s === 429) error.value = t('share.pageThrottled', { n: e?.data?.retry_after ?? 60 })
    else if (s === 401) error.value = t('share.pageWrong')
    else error.value = t('share.pageError')
    focusField()
  } finally {
    busy.value = false
  }
}

const chooseProblem = computed(() => passwordProblem(password.value))
async function choose() {
  if (busy.value) return
  error.value = ''
  if (chooseProblem.value) {
    error.value = t(`share.pw_${chooseProblem.value}`)
    return
  }
  if (password.value !== again.value) {
    error.value = t('share.pageMismatch')
    return
  }
  busy.value = true
  try {
    await client.choose(password.value)
    password.value = ''
    again.value = ''
    await enter()
  } catch (e: any) {
    const s = statusOf(e)
    const code = e?.data?.code
    if (s === 404) step.value = 'gone'
    else if (s === 401) {
      step.value = 'password'
      error.value = t('share.pageAgain')
    } else if (code === 'same_password') error.value = t('share.pageSame')
    else if (code === 'already_chosen') error.value = t('share.pageAlready')
    else if (code === 'weak_password') error.value = e?.data?.detail ?? t('share.pw_short')
    else error.value = t('share.pageError')
    focusField()
  } finally {
    busy.value = false
  }
}

onMounted(async () => {
  // the theme: the one chosen on this browser, or the system's light or dark
  try {
    if (!localStorage.getItem('tabularia-theme') && window.matchMedia?.('(prefers-color-scheme: light)').matches) setTheme('light')
  } catch { /* storage denied: the default */ }
  let f = frame.value
  if (f?.unknown) {
    // an embeddable link cannot be framed here (frame-ancestors 'none'): it is not there
    try {
      const got = await client.frame()
      f = { ...f, available: got.available && !got.embeddable, embeddable: false }
    } catch { f = { ...f, available: false } }
  }
  if (!f || !f.available) {
    step.value = 'gone'
    return
  }
  if (f.embeddable) {
    // only in a frame: opened as a page, it is not there
    if (window.top === window.self) {
      step.value = 'gone'
      return
    }
    try {
      const s = await client.open()
      if (s.session) client.setSession(s.session)
      await enter()
    } catch {
      step.value = 'gone'
    }
    return
  }
  await enter()
})

const themes = THEMES
function pickTheme(e: Event) { setTheme((e.target as HTMLSelectElement).value as Theme) }
</script>

<template>
  <div class="sh" :class="`at-${step}`">
    <main v-if="step === 'ready' && about" class="viewer">
      <ReportView v-if="about.kind === 'report'" />
      <DashboardView v-else />
    </main>

    <main v-else class="door">
      <div v-if="step === 'checking'" class="card" role="status">
        <LoaderCircle :size="20" class="spin" aria-hidden="true" />
        <p class="muted">{{ $t('share.pageOpening') }}</p>
      </div>

      <div v-else-if="step === 'gone'" class="card" role="alert">
        <FileChartColumn :size="26" aria-hidden="true" />
        <h1>{{ $t('share.pageGoneTitle') }}</h1>
        <p class="muted">{{ $t('share.pageGoneBody') }}</p>
      </div>

      <form v-else-if="step === 'password'" class="card" @submit.prevent="unlock">
        <KeyRound :size="24" aria-hidden="true" />
        <h1>{{ $t('share.pagePasswordTitle') }}</h1>
        <p class="muted">{{ $t('share.pagePasswordBody') }}</p>
        <label class="field">
          <span class="lbl">{{ $t('share.pagePassword') }}</span>
          <input ref="pwInput" v-model="password" type="password" autocomplete="current-password" required :aria-invalid="!!error" aria-describedby="sh-err" />
        </label>
        <p id="sh-err" class="err" role="alert">{{ error }}</p>
        <button type="submit" class="primary" :disabled="busy || !password">
          <LoaderCircle v-if="busy" :size="14" class="spin" aria-hidden="true" /> {{ $t('share.pageOpen') }}
        </button>
      </form>

      <form v-else-if="step === 'choose'" class="card" @submit.prevent="choose">
        <LockKeyhole :size="24" aria-hidden="true" />
        <h1>{{ $t('share.pageChooseTitle') }}</h1>
        <p class="muted">{{ $t('share.pageChooseBody', { n: PASSWORD_MIN }) }}</p>
        <label class="field">
          <span class="lbl">{{ $t('share.pageNewPassword') }}</span>
          <input ref="pwInput" v-model="password" type="password" autocomplete="new-password" required :aria-invalid="!!error" aria-describedby="sh-err" />
        </label>
        <label class="field">
          <span class="lbl">{{ $t('share.pageRepeat') }}</span>
          <input v-model="again" type="password" autocomplete="new-password" required />
        </label>
        <p id="sh-err" class="err" role="alert">{{ error }}</p>
        <button type="submit" class="primary" :disabled="busy || !password || !again">
          <LoaderCircle v-if="busy" :size="14" class="spin" aria-hidden="true" /> {{ $t('share.pageSave') }}
        </button>
      </form>
    </main>

    <footer class="foot muted">
      <span>{{ $t('share.pageFoot') }}</span>
      <label class="theme">
        <span class="sr-only">{{ $t('settings.theme') }}</span>
        <select :value="theme" @change="pickTheme">
          <option v-for="th in themes" :key="th.value" :value="th.value">{{ th.label }}</option>
        </select>
      </label>
    </footer>
  </div>
</template>

<style scoped>
.sh { min-height: 100dvh; display: flex; flex-direction: column; background: var(--bg); color: var(--text); }
.viewer { flex: 1; min-height: 0; padding: 16px 20px 0; }
/* the viewers measure themselves against the app's bar: here there is only the foot */
.viewer :deep(.rv) { height: calc(100dvh - 16px - 44px); }
.door { flex: 1; display: grid; place-items: center; padding: 24px 16px; }
.card {
  width: min(420px, 100%); display: flex; flex-direction: column; gap: 12px; align-items: flex-start;
  padding: 28px 28px 24px; border: 1px solid var(--border-soft); border-radius: 14px; background: var(--panel);
  box-shadow: 0 12px 32px -18px rgb(0 0 0 / 0.45);
}
.card h1 { margin: 6px 0 0; font-size: 19px; font-weight: 650; letter-spacing: -0.01em; text-wrap: balance; }
.card p { margin: 0; line-height: 1.5; }
.card > svg { color: var(--accent); }
.field { display: flex; flex-direction: column; gap: 6px; width: 100%; }
.field input { width: 100%; }
.lbl { font-size: 12px; font-weight: 500; color: var(--muted); }
.err { min-height: 1.2em; margin: 0; color: var(--danger); font-size: 13px; }
.card button { align-self: stretch; justify-content: center; }
.muted { color: var(--muted); }
.foot { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 8px 20px; font-size: 12px; min-height: 44px; }
.theme select { font-size: 12px; padding: 2px 6px; }
.spin { animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spin { animation: none; } }
@media (max-width: 560px) { .viewer { padding: 10px 10px 0; } .card { padding: 22px 18px 18px; } }
</style>

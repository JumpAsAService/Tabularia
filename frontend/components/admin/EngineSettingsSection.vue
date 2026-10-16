<script setup lang="ts">
// Impostazioni del motore (pannello Admin): i parametri che il motore legge quando
// servono, cambiati qui senza riavvii. Ogni processo del motore li vede entro
// pochi secondi; il valore di partenza resta quello dell'ambiente, e ci si torna
// con un clic. L'osservatore vede e non tocca; con ENGINE__SETTINGS_PANEL=false
// nessuno li cambia da qui. Il motore controlla i limiti e i vincoli fra voci, e
// un rifiuto torna con un codice che qui si scrive nella lingua di chi guarda.
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  ArrowDownToLine, Cloud, Database, FileOutput, Layers, LoaderCircle, RotateCcw, SlidersHorizontal, Timer,
} from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useFormat } from '~/composables/useFormat'
import {
  GIB, SETTING_CATEGORIES, isSettingsCategory, settingI18nKey, useEngineSettings,
  type EngineSetting, type EngineSettings, type SettingCategory, type SettingError,
} from '~/composables/useEngineSettings'

const emit = defineEmits<{ (e: 'count', n: number): void }>()

const { t, locale } = useI18n()
const fmt = useFormat()
const api = useEngineSettings()
const toast = useToast()

const stato = ref<EngineSettings | null>(null)
const errore = ref('')
const caricando = ref(true)
const salvando = ref(false)
// quello che c'è scritto nei campi: una stringa, perché un campo a metà non è un numero
const testo = ref<Record<string, string>>({})
// quelle che tornano al valore dell'ambiente al salvataggio
const azzera = ref<Set<string>>(new Set())
// i rifiuti del motore all'ultimo salvataggio, per voce
const rifiuti = ref<Record<string, SettingError>>({})

const ICONE: Record<SettingCategory, any> = {
  import: ArrowDownToLine, export: FileOutput, execution: Timer, cache: Layers, clickhouse: Database, bigquery: Cloud,
}

async function carica() {
  caricando.value = true
  errore.value = ''
  try {
    stato.value = await api.get()
    emit('count', contate(stato.value))
  } catch (e) {
    errore.value = errMessage(e)
  } finally {
    caricando.value = false
  }
}
onMounted(carica)

// the ones set from the panel among those shown here (the processes per worker are
// engine settings too, with a section of their own: Admin → Workers)
const contate = (s: EngineSettings) => s.items.filter((v) => v.value !== null && isSettingsCategory(v.category)).length

const categorie = computed(() =>
  SETTING_CATEGORIES.filter((c) => stato.value?.items.some((v) => v.category === c)),
)
const perCategoria = (c: SettingCategory) => (stato.value?.items ?? []).filter((v) => v.category === c)
const spenta = (c: SettingCategory | 'workers') => !!stato.value?.unavailable.includes(c as SettingCategory)
const modificabile = (v: EngineSetting) => !!stato.value?.editable && !spenta(v.category)

// ── unità e formati ─────────────────────────────────────────────────────────

const inUnita = (v: EngineSetting, n: number) => (v.unit === 'bytes' ? n / GIB : n)
const daUnita = (v: EngineSetting, x: number) => (v.unit === 'bytes' ? Math.round(x * GIB) : x)
const unita = (v: EngineSetting) => t(`engineSettings.units.${v.unit}`)

// «1 h 30 min», nella lingua di chi guarda
function durata(secondi: number): string {
  const parti: [number, Intl.NumberFormatOptions['unit']][] = []
  let resto = secondi
  for (const [s, u] of [[86400, 'day'], [3600, 'hour'], [60, 'minute'], [1, 'second']] as const) {
    const n = Math.floor(resto / s)
    if (n) parti.push([n, u])
    resto -= n * s
  }
  return parti
    .slice(0, 2)
    .map(([n, u]) => new Intl.NumberFormat(locale.value, { style: 'unit', unit: u, unitDisplay: 'short' }).format(n))
    .join(' ')
}

function mostra(v: EngineSetting, n: number): string {
  if (v.unit === 'bytes') return `${fmt.number(inUnita(v, n), 2)} ${unita(v)}`
  if (v.unit === 'seconds') return n >= 60 ? `${fmt.count(n)} ${unita(v)} (${durata(n)})` : `${fmt.count(n)} ${unita(v)}`
  return `${fmt.count(n)} ${unita(v)}`
}

const nome = (v: EngineSetting) => t(`engineSettings.items.${settingI18nKey(v.key)}.label`)
const aiuto = (v: EngineSetting) => t(`engineSettings.items.${settingI18nKey(v.key)}.help`)
const idCampo = (v: EngineSetting) => `impostazione-${settingI18nKey(v.key)}`

// ── i campi ─────────────────────────────────────────────────────────────────

const salvato = (v: EngineSetting) => v.value ?? v.env_value
const mostrato = (v: EngineSetting) => (azzera.value.has(v.key) ? v.env_value : salvato(v))

// i separatori della lingua di chi guarda: «1.000.000» e «1,5» in italiano,
// «1,000,000» e «1.5» in inglese, lo spazio stretto delle migliaia in francese
function separatori() {
  const parti = new Intl.NumberFormat(locale.value).formatToParts(12345.6)
  return {
    gruppo: parti.find((x) => x.type === 'group')?.value ?? ',',
    decimale: parti.find((x) => x.type === 'decimal')?.value ?? '.',
  }
}

function campo(v: EngineSetting): string {
  if (v.key in testo.value) return testo.value[v.key]
  return new Intl.NumberFormat(locale.value, { maximumFractionDigits: 2 }).format(inUnita(v, mostrato(v)))
}

function scrivi(v: EngineSetting, s: string) {
  testo.value = { ...testo.value, [v.key]: s }
  azzera.value.delete(v.key)
  azzera.value = new Set(azzera.value)
  const { [v.key]: _via, ...altri } = rifiuti.value
  rifiuti.value = altri
}

function torna(v: EngineSetting) {
  const { [v.key]: _via, ...altri } = testo.value
  testo.value = altri
  if (v.value !== null) azzera.value = new Set([...azzera.value, v.key])
}

// il valore del campo nell'unità del motore, o null se non è un numero: chi
// scrive «250.000» in italiano intende duecentocinquantamila, non 250
function numero(v: EngineSetting): number | null {
  const { gruppo, decimale } = separatori()
  let s = (testo.value[v.key] ?? '').trim().replace(/[\s\u00a0\u202f']/g, '')
  if (!s) return null
  if (gruppo.trim() && s.includes(gruppo)) {
    // i gruppi delle migliaia sono da tre cifre: «12.5.3» è un errore, non 1253
    const e = (x: string) => x.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    if (!new RegExp(`^-?\\d{1,3}(${e(gruppo)}\\d{3})+(${e(decimale)}\\d+)?$`).test(s)) return null
    s = s.split(gruppo).join('')
  }
  s = s.replace(decimale, '.')
  if (!/^-?\d+(\.\d+)?$/.test(s)) return null
  const x = Number(s)
  return Number.isFinite(x) ? daUnita(v, x) : null
}

function messaggioDi(v: EngineSetting, e: SettingError): string {
  const altra = stato.value?.items.find((x) => x.key === e.other)
  return t(`engineSettings.errors.${e.code}`, {
    limit: e.limit !== undefined ? mostra(altra ?? v, e.limit) : '',
    other: altra ? nome(altra) : (e.other ?? ''),
  })
}

// prima i controlli che si possono fare qui, poi il rifiuto del motore
function problema(v: EngineSetting): string {
  if (v.key in testo.value) {
    const n = numero(v)
    if (n === null || !Number.isInteger(n)) return messaggioDi(v, { key: v.key, code: 'integer' })
    if (n < v.min) return messaggioDi(v, { key: v.key, code: 'min', limit: v.min })
    if (n > v.max) return messaggioDi(v, { key: v.key, code: 'max', limit: v.max })
  }
  const r = rifiuti.value[v.key]
  return r ? messaggioDi(v, r) : ''
}

// le modifiche da spedire: numero = imposta, null = torna all'ambiente
const cambi = computed<Record<string, number | null>>(() => {
  const out: Record<string, number | null> = {}
  for (const v of stato.value?.items ?? []) {
    if (azzera.value.has(v.key)) {
      if (v.value !== null) out[v.key] = null
    } else if (v.key in testo.value) {
      const n = numero(v)
      if (n !== null && n !== salvato(v)) out[v.key] = n
    }
  }
  return out
})
const numeroCambi = computed(() => Object.keys(cambi.value).length)
const nonSalvabile = computed(() => (stato.value?.items ?? []).some((v) => v.key in testo.value && !!problema(v)))
const daSalvare = (v: EngineSetting) => v.key in cambi.value

function annulla() {
  testo.value = {}
  azzera.value = new Set()
  rifiuti.value = {}
}

async function salva() {
  if (!numeroCambi.value || nonSalvabile.value) return
  salvando.value = true
  rifiuti.value = {}
  try {
    stato.value = await api.save(cambi.value)
    annulla()
    emit('count', contate(stato.value))
    toast.success(t('engineSettings.saved', { seconds: stato.value.refresh_seconds }))
  } catch (e: any) {
    const errori: SettingError[] | undefined = e?.data?.detail?.errors
    if (errori?.length) {
      rifiuti.value = Object.fromEntries(errori.map((x) => [x.key, x]))
      toast.error(t('engineSettings.rejected'))
    } else {
      toast.error(errMessage(e))
    }
  } finally {
    salvando.value = false
  }
}
</script>

<template>
  <div class="card">
    <h4><SlidersHorizontal :size="14" /> {{ $t('engineSettings.title') }}</h4>
    <p class="hint muted small">{{ $t('engineSettings.lead', { seconds: stato?.refresh_seconds ?? 10 }) }}</p>
    <p v-if="stato && !stato.panel_enabled" class="nota">{{ $t('engineSettings.lockedByEnv') }}</p>
    <p v-else-if="stato && !stato.editable" class="nota">{{ $t('engineSettings.readOnly') }}</p>
  </div>

  <p v-if="caricando" class="muted small"><LoaderCircle :size="13" class="spin" /> {{ $t('engineSettings.loading') }}</p>
  <p v-else-if="errore" class="card errore-pagina" role="alert">{{ errore }}</p>

  <template v-else-if="stato">
    <section v-for="c in categorie" :key="c" class="card" :class="{ spenta: spenta(c) }" :aria-labelledby="`categoria-${c}`">
      <h4 :id="`categoria-${c}`"><component :is="ICONE[c]" :size="14" /> {{ $t(`engineSettings.categories.${c}.title`) }}</h4>
      <p class="hint muted small">{{ $t(`engineSettings.categories.${c}.lead`) }}</p>
      <p v-if="spenta(c)" class="nota">{{ $t('engineSettings.unavailable') }}</p>

      <div v-for="v in perCategoria(c)" :key="v.key" class="voce">
        <div class="descrizione">
          <label :for="idCampo(v)" class="nome">{{ nome(v) }}</label>
          <p class="aiuto">{{ aiuto(v) }}</p>
          <p class="meta">
            {{ $t('engineSettings.envValue', { value: mostra(v, v.env_value) }) }}
            <code>{{ v.env_var }}</code>
            <template v-if="v.value !== null && v.updated_by">
              · {{ $t('engineSettings.setBy', { who: v.updated_by, when: fmt.ago(v.updated_at) }) }}
            </template>
          </p>
        </div>

        <div class="valore">
          <div class="campo" :class="{ modificato: daSalvare(v), sbagliato: !!problema(v) }">
            <input
              :id="idCampo(v)"
              type="text"
              inputmode="decimal"
              autocomplete="off"
              :value="campo(v)"
              :disabled="!modificabile(v) || salvando"
              :aria-invalid="!!problema(v)"
              :aria-describedby="`${idCampo(v)}-intervallo`"
              @input="scrivi(v, ($event.target as HTMLInputElement).value)"
            />
            <span class="unita">{{ unita(v) }}</span>
          </div>
          <p :id="`${idCampo(v)}-intervallo`" class="intervallo">
            {{ $t('engineSettings.range', { min: mostra(v, v.min), max: mostra(v, v.max) }) }}
          </p>
          <p v-if="problema(v)" class="errore" role="alert">{{ problema(v) }}</p>
          <div class="stato">
            <span v-if="daSalvare(v)" class="chip da-salvare">{{ $t('engineSettings.unsaved') }}</span>
            <span v-else-if="v.value !== null" class="chip dal-pannello">{{ $t('engineSettings.fromPanel') }}</span>
            <span v-if="v.value !== null && v.applied !== v.value && !daSalvare(v)" class="chip non-applicata" :title="$t('engineSettings.notAppliedHint')">
              {{ $t('engineSettings.notApplied') }}
            </span>
            <button
              v-if="modificabile(v) && ((v.value !== null && !azzera.has(v.key)) || v.key in testo)"
              type="button"
              class="torna"
              :disabled="salvando"
              @click="torna(v)"
            >
              <RotateCcw :size="12" /> {{ v.value !== null ? $t('engineSettings.resetToEnv') : $t('engineSettings.undo') }}
            </button>
          </div>
        </div>
      </div>
    </section>

    <div v-if="numeroCambi" class="barra" role="region" :aria-label="$t('engineSettings.pendingRegion')">
      <span>{{ $t('engineSettings.changes', { n: numeroCambi }, numeroCambi) }}</span>
      <button type="button" :disabled="salvando" @click="annulla">{{ $t('engineSettings.cancel') }}</button>
      <button type="button" class="primary" :disabled="salvando || nonSalvabile" @click="salva">
        <LoaderCircle v-if="salvando" :size="13" class="spin" /> {{ $t('engineSettings.save') }}
      </button>
    </div>
  </template>
</template>

<style scoped>
.card { display: flex; flex-direction: column; gap: 10px; padding: 16px; background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius); }
.card h4 { margin: 0; display: inline-flex; align-items: center; gap: 7px; }
.hint { margin: 0; line-height: 1.5; }
.small { font-size: 12px; }
.nota { margin: 0; font-size: 12.5px; padding: 8px 10px; border-radius: 8px; background: var(--bg-soft); border: 1px solid var(--border); }
.errore-pagina { color: var(--danger); font-size: 13px; }
.card.spenta .voce { opacity: 0.6; }

.voce {
  display: grid; grid-template-columns: minmax(0, 1fr) minmax(220px, 280px); gap: 8px 24px;
  padding: 14px 0 4px; border-top: 1px solid var(--border-soft);
}
.descrizione { min-width: 0; }
.nome { display: block; font-size: 13.5px; font-weight: 600; color: var(--text); }
.aiuto { margin: 4px 0 0; font-size: 12.5px; line-height: 1.55; color: var(--muted); max-width: 72ch; }
.meta { margin: 6px 0 0; font-size: 11.5px; color: var(--muted); }
.meta code { font-size: 11px; padding: 1px 5px; border-radius: 5px; background: var(--bg-soft); border: 1px solid var(--border-soft); }

.valore { display: flex; flex-direction: column; gap: 4px; }
.campo {
  display: flex; align-items: stretch; border: 1px solid var(--control-border); border-radius: 8px;
  background: var(--bg-soft); overflow: hidden;
}
.campo:focus-within { border-color: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 22%, transparent); }
.campo.modificato { border-color: color-mix(in srgb, var(--accent) 60%, var(--control-border)); }
.campo.sbagliato { border-color: var(--danger); }
.campo input {
  flex: 1; min-width: 0; border: 0; background: transparent; box-shadow: none; padding: 7px 9px;
  font-variant-numeric: tabular-nums; text-align: right;
}
.campo input:focus { outline: none; box-shadow: none; }
.unita { display: inline-flex; align-items: center; padding: 0 10px; font-size: 12px; color: var(--muted); border-left: 1px solid var(--border-soft); white-space: nowrap; }
.intervallo { margin: 0; font-size: 11.5px; color: var(--muted); }
.errore { margin: 0; font-size: 12px; color: var(--danger); }
.stato { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; min-height: 22px; }
.chip { font-size: 10.5px; letter-spacing: 0.02em; border-radius: 999px; padding: 1px 8px; border: 1px solid var(--border); background: var(--panel-2); color: var(--muted); }
.chip.dal-pannello { color: var(--accent-hi); border-color: color-mix(in srgb, var(--accent) 40%, var(--border)); background: var(--tint-accent); }
.chip.da-salvare { color: var(--text); border-style: dashed; }
.chip.non-applicata { color: var(--danger); border-color: color-mix(in srgb, var(--danger) 45%, var(--border)); }
.torna { display: inline-flex; align-items: center; gap: 4px; padding: 0 2px; min-height: 22px; border: 0; background: none; font-size: 12px; color: var(--muted); cursor: pointer; }
.torna:hover:not(:disabled) { color: var(--accent); box-shadow: none; }

.barra {
  position: sticky; bottom: 0; z-index: 2; display: flex; align-items: center; justify-content: flex-end; gap: 10px;
  padding: 10px 14px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--panel);
  box-shadow: 0 -6px 18px color-mix(in srgb, var(--text) 8%, transparent);
}
.barra span { margin-right: auto; font-size: 13px; }

@media (max-width: 720px) {
  .voce { grid-template-columns: 1fr; }
}
</style>

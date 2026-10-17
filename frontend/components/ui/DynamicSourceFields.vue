<script setup lang="ts">
// The «dynamic» part of a datasource that reads moving names (SFTP, SharePoint and
// S3 folders, the API call): the time zone that defines «today», the variables —
// lists `symbol = AAPL, MSFT` and ranges `day = today-7 .. today step 1d`, one per
// line, each row keeping its value in _param_<name> — the first value of
// {last_refresh}, how a refresh lands (snapshot, or append by key columns) and the
// cheat sheet of the placeholders. What is wrong with the zone or the variables
// shows under them (the `preview`'s global line).
import { ref, watch } from 'vue'
import { parseVariables, splitKeys, variablesToText, RANGE_EXAMPLE, type FileTemplatesDraft, type PlaceholderPreview } from '~/composables/usePlaceholders'
import PlaceholderCheat from '~/components/ui/PlaceholderCheat.vue'
import TemplatePreview from '~/components/ui/TemplatePreview.vue'

const props = withDefaults(defineProps<{
  preview?: PlaceholderPreview | null
  withMode?: boolean // the API form has its own «Each refresh» box (with the limits)
  keysPlaceholder?: string
}>(), { preview: null, withMode: true, keysPlaceholder: '_param_day, id' })
const model = defineModel<FileTemplatesDraft>({ required: true })

const variablesText = ref(variablesToText(model.value.variables))
const keysText = ref(model.value.keys.join(', '))
watch(variablesText, (v) => { model.value.variables = parseVariables(v) })
watch(keysText, (v) => { model.value.keys = splitKeys(v) })
// a new draft (the dialog reopened): the texts follow it
watch(() => model.value, (m) => {
  variablesText.value = variablesToText(m.variables)
  keysText.value = m.keys.join(', ')
})
</script>

<template>
  <div class="dyn">
    <label>{{ $t('placeholders.variablesLabel') }} <span class="dyn-soft">{{ $t('placeholders.variablesHint') }}</span></label>
    <textarea
      v-model="variablesText"
      rows="2"
      spellcheck="false"
      class="dyn-mono dyn-variables"
      :placeholder="`symbol = AAPL, MSFT\n${RANGE_EXAMPLE}`"
    />
    <PlaceholderCheat kind="input" />
    <div class="dyn-two">
      <div>
        <label>{{ $t('placeholders.timezoneLabel') }} <span class="dyn-soft">{{ $t('placeholders.timezoneHint') }}</span></label>
        <input v-model="model.timezone" type="text" spellcheck="false" class="dyn-timezone" placeholder="Europe/Rome" />
      </div>
      <div>
        <label>{{ $t('placeholders.lastRefreshLabel') }} <span class="dyn-soft">{{ $t('placeholders.lastRefreshHint') }}</span></label>
        <input v-model="model.last_refresh_initial" type="text" spellcheck="false" class="dyn-mono dyn-last-refresh" placeholder="today-30" />
      </div>
    </div>
    <TemplatePreview v-if="props.preview" :preview="props.preview" global />

    <fieldset v-if="withMode" class="dyn-box">
      <legend>{{ $t('placeholders.refreshTitle') }}</legend>
      <div class="dyn-mode">
        <Select
          v-model="model.mode"
          :options="[
            { value: 'snapshot', label: $t('placeholders.modeSnapshot') },
            { value: 'append', label: $t('placeholders.modeAppend') },
          ]"
        />
      </div>
      <template v-if="model.mode === 'append'">
        <label>{{ $t('placeholders.keysLabel') }} <span class="dyn-soft">{{ $t('placeholders.keysHint') }}</span></label>
        <input v-model="keysText" type="text" spellcheck="false" class="dyn-mono dyn-keys" :placeholder="keysPlaceholder" />
        <p v-if="!model.keys.length" class="dyn-err">{{ $t('placeholders.keysRequired') }}</p>
      </template>
    </fieldset>
  </div>
</template>

<style scoped>
.dyn { display: flex; flex-direction: column; gap: 6px; }
.dyn label { font-size: 12px; color: var(--muted); }
.dyn-soft { opacity: 0.7; font-size: 12px; }
.dyn-mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; }
.dyn textarea { resize: vertical; }
.dyn-two { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 10px; }
.dyn-two > div { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
@media (max-width: 520px) { .dyn-two { grid-template-columns: 1fr; } }
.dyn-box { border: 1px solid var(--border-soft); border-radius: 8px; padding: 6px 10px 8px; margin: 2px 0 0; display: flex; flex-direction: column; gap: 5px; min-width: 0; }
.dyn-box legend { font-size: 12px; color: var(--muted); padding: 0 4px; }
.dyn-err { color: var(--danger); font-size: 12px; margin: 0; }
</style>

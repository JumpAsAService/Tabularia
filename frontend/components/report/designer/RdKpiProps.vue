<script setup lang="ts">
// A KPI card's properties: its label, its value (an aggregate), what it is compared with
// (the change in % with its arrow), the format, and a sparkline (a value along a category).
import { computed } from 'vue'
import Select from '~/components/ui/Select.vue'
import RdExprField from '~/components/report/designer/RdExprField.vue'
import RdFormatInput from '~/components/report/designer/RdFormatInput.vue'
import RdSection from '~/components/report/designer/RdSection.vue'
import { useDesign } from '~/composables/reportDesignStore'
import type { DKpi } from '~/composables/reportDesignModel'

const props = defineProps<{ item: DKpi }>()
const store = useDesign()

const fields = computed(() => store.fieldsOf(props.item.dataset))
const datasetOptions = computed(() => store.doc.value.datasets.map((d) => ({ value: d.name, label: d.name })))
const id = computed(() => props.item.id)
const has = (prefix: string) => store.problems.value.some((p) => p.item === id.value && p.path.startsWith(prefix))

function setSpark(on: boolean) {
  props.item.sparkline = on ? { category: '', value: props.item.value || '' } : null
}
</script>

<template>
  <RdSection :title="$t('reportDesigner.kpi.card')" skey="kpi-card" :bad="has('dataset') || has('value') || has('compare') || has('format')">
    <label class="rp-f">
      <span>{{ $t('reportDesigner.props.dataset') }}</span>
      <Select :model-value="item.dataset" :options="datasetOptions" :placeholder="$t('reportDesigner.props.chooseDataset')" :aria-label="$t('reportDesigner.props.dataset')" @update:model-value="item.dataset = $event" />
    </label>
    <p v-if="store.problemAt(id, 'dataset')" class="rp-err" role="alert">{{ store.problemAt(id, 'dataset')?.message }}</p>
    <label class="rp-f">
      <span>{{ $t('reportDesigner.kpi.label') }}</span>
      <input v-model="item.label" type="text" maxlength="200" :placeholder="$t('reportDesigner.kpi.labelPh')" />
    </label>
    <RdExprField :obj="item" prop="value" :label="$t('reportDesigner.kpi.value')" path="value" :item="id" :fields="fields" placeholder="SUM([amount])" />
    <RdExprField :obj="item" prop="compare" :label="$t('reportDesigner.kpi.compare')" path="compare" :item="id" :fields="fields" optional
                 :placeholder="'SUM([amount_last_year])'" :hint="$t('reportDesigner.kpi.compareHint')" />
    <RdFormatInput v-model="item.format" :label="$t('reportDesigner.style.format')" :item="id" path="format" />
  </RdSection>
  <RdSection :title="$t('reportDesigner.kpi.sparkline')" skey="kpi-spark" :bad="has('sparkline')">
    <label class="rp-check">
      <input type="checkbox" :checked="!!item.sparkline" @change="setSpark(($event.target as HTMLInputElement).checked)" />
      {{ $t('reportDesigner.kpi.showSparkline') }}
    </label>
    <template v-if="item.sparkline">
      <RdExprField :obj="item.sparkline" prop="category" :label="$t('reportDesigner.kpi.sparkCategory')" path="sparkline.category" :item="id" :fields="fields" placeholder="[month]" />
      <RdExprField :obj="item.sparkline" prop="value" :label="$t('reportDesigner.kpi.sparkValue')" path="sparkline.value" :item="id" :fields="fields" placeholder="SUM([amount])" />
    </template>
  </RdSection>
</template>

<style scoped src="~/assets/reportprops.css" />

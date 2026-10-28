// The report designer's calls (docs/specs/reports.md, «Gateway API»): saving a definition
// as a new version, the preview and the check of a definition not saved yet, a dataset's
// fields, a copy, a version restored. They need EDIT on the report's folder. The reading calls and how a
// render is followed are the viewer's (useReports.ts): the preview is followed with its
// `followRender` and read with its `screenOf`.
import {
  useReports,
  type ReportDefinition, type ReportDetail, type ReportParams, type ReportScreen, type ReportState,
} from '~/composables/useReports'
import type { DDefinition, DField } from '~/composables/reportDesignModel'

export interface PreviewOutcome extends ReportScreen {
  warnings: string[]
  pages_count: number | null
}

export function useReportDesign() {
  const { apiFetch } = useApiClient()
  const reports = useReports()

  /** A new version of the definition (and the name, when it changed). A 422 carries the
   *  problems: `{message, where, item, problems: [{item, field, pos, length, message}]}`;
   *  with `base_version`, a 409 says another version became the current one since. */
  const save = (id: number, body: { name?: string; content: DDefinition; base_version?: number }) =>
    apiFetch<ReportDetail>(`/reports/${id}`, { method: 'PATCH', body: { ...body, content: body.content as unknown as ReportDefinition } })

  /** The pages of a definition not saved yet, on screen (svg): started, followed, read. */
  async function preview(id: number, definition: DDefinition, params: ReportParams, state: ReportState | null,
    signal?: AbortSignal): Promise<PreviewOutcome> {
    const started = await apiFetch<{ task_id: string; filename: string }>('/reports/preview', {
      method: 'POST', body: { report_id: id, definition, format: 'svg', params, state }, signal,
    })
    const outcome = await reports.followRender(started.task_id, { signal })
    const screen = await reports.screenOf(outcome)
    return { ...screen, warnings: outcome.warnings, pages_count: outcome.pages }
  }

  /** The server's check of a definition not saved yet (the gateway's rules and the
   *  engine's, with the datasets' fields): `{problems: [{item, field, pos, length, message}]}`,
   *  as a refused save lists them. Asked by the live check (reportLiveCheck.ts). */
  const check = (id: number, definition: unknown, signal?: AbortSignal) =>
    apiFetch<{ problems: unknown[] }>('/reports/check', { method: 'POST', body: { report_id: id, definition }, signal })

  /** A dataset's columns as the data has them, read from the definition being edited. */
  const fields = (id: number, dataset: string, definition: DDefinition) =>
    apiFetch<{ dataset: string; fields: DField[] }>(`/reports/${id}/fields`, {
      method: 'POST', body: { dataset, definition },
    })

  return {
    get: reports.get,
    parameters: reports.parameters,
    save,
    preview,
    check,
    fields,
    duplicate: (id: number) => apiFetch<ReportDetail>(`/reports/${id}/duplicate`, { method: 'POST' }),
    restore: (id: number, version: number) =>
      apiFetch<ReportDetail>(`/reports/${id}/versions/${version}/restore`, { method: 'POST' }),
  }
}

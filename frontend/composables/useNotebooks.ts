// Notebooks: marimo notebooks kept in a folder, with their versions, published as web
// apps (docs/specs/notebooks.md). The gateway shows a notebook only to whoever has VIEW
// on its folder AND may use its environment, as a saved script: for anyone else it does
// not exist. The app reads with the rights of whoever saved the version it runs.

export interface NotebookContent {
  /** the marimo .py file */
  source: string
}

export interface Notebook {
  id: number
  name: string
  description: string
  project_id: number
  environment_id: number
  environment_name: string | null
  language: 'python' | 'r' | null
  current_version: number
  created_at: string
  updated_at: string
  /** EDIT on its folder */
  editable: boolean
}

/** Whoever saved a version: the app reads with their rights. */
export interface NotebookAuthor {
  id: number
  email: string
  full_name: string
}

export interface NotebookDetail extends Notebook {
  content: NotebookContent
  /** of the current version; null when that account was deleted */
  author: NotebookAuthor | null
  /** the editor's unsaved work, for whoever may edit */
  draft: string | null
  draft_by: string | null
  draft_at: string | null
  /** how many people may have the app open at once; null = the installation's default */
  max_visitors: number | null
  /** the installation's default, and the most a notebook may set */
  visitors_default: number
  visitors_ceiling: number
}

export interface NotebookVersion {
  version: number
  /** a code the interface translates: created, edited, restored from vN */
  note: string
  created_at: string
  created_by: string | null
  current: boolean
}

export interface NotebookUpdate {
  name?: string
  description?: string
  project_id?: number
  environment_id?: number
  content?: NotebookContent
  /** the version `content` was written on: refused (409) when another one is current now */
  base_version?: number
  /** null: back to the installation's default; left out: unchanged */
  max_visitors?: number | null
}

/** app: the published notebook, read with its author's rights; edit: marimo's editor */
export type NotebookMode = 'app' | 'edit'

/** A live pod of a notebook: one app and at most one editor per notebook. */
export interface NotebookSession {
  mode: NotebookMode
  status: 'starting' | 'ready' | 'failed'
  /** why it failed, in words */
  error: string | null
  /** whose rights it reads with: the author of the version (app) or whoever edits */
  user: NotebookAuthor
  started_at: string
  last_seen_at: string | null
  /** the app's: the people with it open now, and the most it takes at once */
  visitors?: number | null
  max_visitors?: number | null
}

/** What opens a notebook's frame (POST /notebooks/{id}/ticket). */
export interface NotebookTicket {
  /** the notebooks' own address with a ticket used once, within a minute; null when they are
   *  served on the interface's address (NOTEBOOKS__ALLOW_SAME_ORIGIN) */
  url: string | null
  /** NOTEBOOKS__ALLOW_SAME_ORIGIN: a notebook's code could act as whoever opens it */
  same_origin: boolean
  expires_in: number
}

/** The 409 of a second editor: who is editing already. */
export function editorOf(e: unknown): NotebookAuthor | null {
  const data = (e as { data?: any } | null)?.data
  const who = data?.editor ?? data?.detail?.editor
  return who && typeof who.email === 'string' ? who as NotebookAuthor : null
}

/** A person's name as the interface shows it: the full name, or the email. */
export function personName(p: NotebookAuthor | null | undefined): string {
  return p ? (p.full_name?.trim() || p.email) : ''
}

export function useNotebooks() {
  const { apiFetch } = useApiClient()
  const base = useRuntimeConfig().public.apiBase as string
  return {
    list: () => apiFetch<Notebook[]>('/notebooks'),
    listByProject: (projectId: number) => apiFetch<Notebook[]>(`/projects/${projectId}/notebooks`),
    get: (id: number) => apiFetch<NotebookDetail>(`/notebooks/${id}`),
    /** without a content the gateway writes a starting notebook */
    create: (projectId: number, body: {
      name: string; description?: string; environment_id: number; content?: NotebookContent; max_visitors?: number | null
    }) =>
      apiFetch<NotebookDetail>(`/projects/${projectId}/notebooks`, { method: 'POST', body }),
    update: (id: number, body: NotebookUpdate) => apiFetch<NotebookDetail>(`/notebooks/${id}`, { method: 'PATCH', body }),
    discardDraft: (id: number) => apiFetch<NotebookDetail>(`/notebooks/${id}/draft`, { method: 'DELETE' }),
    duplicate: (id: number) => apiFetch<NotebookDetail>(`/notebooks/${id}/duplicate`, { method: 'POST' }),
    remove: (id: number) => apiFetch<void>(`/notebooks/${id}`, { method: 'DELETE' }),
    versions: (id: number) => apiFetch<NotebookVersion[]>(`/notebooks/${id}/versions`),
    version: (id: number, version: number) => apiFetch<NotebookContent>(`/notebooks/${id}/versions/${version}`),
    restore: (id: number, version: number) =>
      apiFetch<NotebookDetail>(`/notebooks/${id}/versions/${version}/restore`, { method: 'POST' }),
    // ── the runtime (Kubernetes): a pod per notebook and mode, started on demand ──
    /** starts the session, or returns (and touches) the live one */
    startSession: (id: number, mode: NotebookMode) =>
      apiFetch<NotebookSession>(`/notebooks/${id}/sessions`, { method: 'POST', body: { mode } }),
    /** the live sessions; the editor's only for whoever has EDIT */
    sessions: (id: number) => apiFetch<NotebookSession[]>(`/notebooks/${id}/sessions`),
    /** stopping the editor keeps its file as the notebook's draft first */
    stopSession: (id: number, mode: NotebookMode) =>
      apiFetch<void>(`/notebooks/${id}/sessions/${mode}`, { method: 'DELETE' }),
    /** a new version from the file marimo last saved; none when it did not change */
    saveFromEditor: (id: number, note: string | null) =>
      apiFetch<NotebookDetail>(`/notebooks/${id}/edit/save`, { method: 'POST', body: { note } }),
    /** a ticket for the app's or the editor's frame, on the notebooks' own address */
    ticket: (id: number, mode: NotebookMode) =>
      apiFetch<NotebookTicket>(`/notebooks/${id}/ticket`, { method: 'POST', body: { mode } }),
    /** where marimo answers on the interface's own address (NOTEBOOKS__ALLOW_SAME_ORIGIN; the
     *  trailing slash is marimo's base URL) */
    frameUrl: (id: number, mode: NotebookMode) => `${base}/notebooks/${id}/${mode}/`,
  }
}

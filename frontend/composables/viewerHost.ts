// Who hosts a report's or a dashboard's viewer. Inside Tabularia nobody does: the pages read their
// object from the address and ask the gateway's routes as the person signed in. A share link's
// page (pages/share/[token].vue) hosts them: it gives the object, the address to write the values
// in, and the same APIs over the link's routes, so the viewer a recipient sees is the one the
// creator sees, without what belongs to the app (versions, designer, folders, API links).
import { inject, type InjectionKey } from 'vue'
import { useReports, type ReportDetail } from '~/composables/useReports'
import { useDashboards, type DashboardDetail } from '~/composables/useDashboards'
import type { TileComment } from '~/composables/dashboardComments'
import { tileCommentOf, type ShareAbout, type ShareClient, type ShareComments } from '~/composables/shareLinks'

export interface ViewerHost {
  /** the address the viewer writes its values in */
  path: string
  /** the object's id as the viewer keeps it (the link's routes do not need it) */
  objectId: number
  /** the downloads allowed: 'pdf', 'xlsx', 'png' */
  downloads: string[]
  reports?: ReturnType<typeof useReports>
  dashboards?: ReturnType<typeof useDashboards>
}

export const VIEWER_HOST: InjectionKey<ViewerHost> = Symbol('viewer-host')

export function useViewerHost(): ViewerHost | null {
  return inject(VIEWER_HOST, null)
}

function detailOf(about: ShareAbout, id: number) {
  const now = new Date().toISOString()
  return {
    id, name: about.name, description: about.description, project_id: -1, current_version: 0, created_at: now, updated_at: now,
    editable: false, content: about.content,
    parameters: Array.isArray(about.content?.parameters) ? about.content.parameters.length : 0,
    datasets: Array.isArray(about.content?.datasets) ? about.content.datasets.length : 0,
    tiles: Array.isArray(about.content?.tiles) ? about.content.tiles.length : 0,
  }
}

const refusedHere = () => Promise.reject(Object.assign(new Error('not on a share link'), { statusCode: 404 }))

export function shareReportsApi(client: ShareClient, about: ShareAbout, id: number): ReturnType<typeof useReports> {
  const own = useReports()
  return {
    ...own,
    get: async () => detailOf(about, id) as unknown as ReportDetail,
    opened: async () => {},
    parameters: (_id, given = {}) => client.parameters(given),
    render: (_id, body, signal) => client.render(body, signal),
    followRender: client.followRender,
    versions: refusedHere,
    version: refusedHere,
    restore: refusedHere,
  }
}

export function shareDashboardsApi(client: ShareClient, about: ShareAbout, id: number): ReturnType<typeof useDashboards> {
  const own = useDashboards()
  // one request for the whole document's comments per values: every comment tile asks it
  let asked: { key: string; answer: Promise<ShareComments> } | null = null
  return {
    ...own,
    get: async () => detailOf(about, id) as unknown as DashboardDetail,
    opened: async () => {},
    parameters: (_id, given = {}) => client.parameters(given),
    data: (_id, body, signal) => client.data(body, signal),
    render: (_id, body) => client.render(body),
    comment: async (_id, body, signal) => {
      const key = JSON.stringify(body.params)
      if (!asked || asked.key !== key) asked = { key, answer: client.comments(body.params, signal) }
      const got = await asked.answer.catch((e) => { asked = null; throw e })
      return tileCommentOf(got.comments?.[body.tile], got.stale) as TileComment
    },
  }
}

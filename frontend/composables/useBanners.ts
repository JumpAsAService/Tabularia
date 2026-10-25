// The Explore's warning banners. Anyone authenticated reads them, only an administrator
// writes them (the gateway refuses the others' writes anyway).

export type BannerLevel = 'info' | 'warning' | 'danger'

export interface Banner {
  id: number
  message: string
  level: BannerLevel
  created_at: string | null
  created_by: number | null
}

export const BANNER_LEVELS: BannerLevel[] = ['info', 'warning', 'danger']

export function useBanners() {
  const { apiFetch } = useApiClient()

  return {
    list: () => apiFetch<Banner[]>('/banners'),

    create: (body: { message: string; level: BannerLevel }) =>
      apiFetch<Banner>('/banners', { method: 'POST', body }),

    remove: (id: number) => apiFetch<void>(`/banners/${id}`, { method: 'DELETE' }),
  }
}

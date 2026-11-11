// The list of keys, one for the whole app (the keyboard button of the top bar, the one on
// the editor's canvas, «?»): every surface DECLARES its keys here while it is on screen
// (`useKeyGroups`), and the list shows the keys of everywhere plus what was declared. One
// source: no surface keeps a list of another's keys.
import { computed, onBeforeUnmount, onMounted, shallowRef, type ComputedRef } from 'vue'
import { useI18n } from 'vue-i18n'
import { keyNames, platformIsMac, visibleSections, type NavSection } from '~/composables/appKeys'
import { useScriptingEditor } from '~/composables/useScripting'
import { useRegistriesEditor } from '~/composables/useRegistries'

export interface KeyGroup { title: string; keys: [string, string][]; note?: string }

interface Declared { order: number; groups: ComputedRef<KeyGroup[]> }
const declared = shallowRef<Declared[]>([])
// who shows the list on this page: the last button mounted (the top bar's, the editor's)
const hosts = shallowRef<(() => void)[]>([])

/**
 * Declares a surface's keys for as long as it is mounted. `order` sorts the surfaces in
 * the list (a page before the table inside it): the lower first.
 */
export function useKeyGroups(source: () => KeyGroup[], order = 0) {
  const entry: Declared = { order, groups: computed(source) }
  onMounted(() => { declared.value = [...declared.value, entry] })
  onBeforeUnmount(() => { declared.value = declared.value.filter((d) => d !== entry) })
}

/** The places «g» leads this person to. Read from what the app already knows of them: it
 *  asks the gateway nothing. */
export function useGoToSections(): ComputedRef<NavSection[]> {
  const { user } = useAuth()
  const scripting = useScriptingEditor()
  const registries = useRegistriesEditor()
  return computed(() => visibleSections({
    scripting: scripting.available.value || registries.available.value,
    observe: !!user.value?.is_superuser || !!user.value?.is_observer,
  }))
}

export function useKeyHelp() {
  const { t } = useI18n()
  const sections = useGoToSections()
  const names = keyNames(platformIsMac())

  const groups = computed<KeyGroup[]>(() => {
    const everywhere: KeyGroup = {
      title: t('appKeys.groupEverywhere'),
      keys: [
        [t('appKeys.gThenLetter'), t('appKeys.goTo')],
        [', · .', t('appKeys.subTabs')],
        [names.mac ? '← →' : '← → · Home · End', t('appKeys.tabRow')],
        ['/', t('appKeys.search')],
        ['?', t('appKeys.list')],
      ],
    }
    const page = [...declared.value].sort((a, b) => a.order - b.order).flatMap((d) => d.groups.value)
    const goTo: KeyGroup = {
      title: t('appKeys.groupGoTo'),
      keys: sections.value.map((s) => [`g ${s.key}`, t(s.label)] as [string, string]),
    }
    return [everywhere, ...page, goTo]
  })

  /** Opens or closes the list where this page shows it; false when no button is there. */
  function toggle(): boolean {
    const host = hosts.value[hosts.value.length - 1]
    host?.()
    return !!host
  }
  /** The button that shows the list on this page, for as long as it is mounted. */
  function host(open: () => void) {
    onMounted(() => { hosts.value = [...hosts.value, open] })
    onBeforeUnmount(() => { hosts.value = hosts.value.filter((h) => h !== open) })
  }
  const hosted = computed(() => hosts.value.length > 0)

  return { groups, sections, names, toggle, host, hosted }
}

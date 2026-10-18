// Entrance for the parts of each section: elements with data-rise rise in order once their
// section enters the viewport. Nothing is hidden before the app mounts (the rise-ready class
// gates the hidden state), what is already on screen never hides, and reduced motion only shows.
export function useReveal () {
  let io: IntersectionObserver | null = null
  onBeforeUnmount(() => io?.disconnect())
  onMounted(() => {
    const els = [...document.querySelectorAll<HTMLElement>('[data-rise]')]
    const groups = new Map<Element, HTMLElement[]>()
    for (const el of els) {
      const g = el.closest('[data-rise-group]') || el.parentElement!
      if (!groups.has(g)) groups.set(g, [])
      groups.get(g)!.push(el)
    }
    for (const list of groups.values()) list.forEach((el, i) => el.style.setProperty('--i', String(Math.min(i, 8))))
    if (!('IntersectionObserver' in window)) { els.forEach(e => e.classList.add('is-in')); return }
    // what is already on screen stays as it is; the rest waits for its section
    const onScreen = (e: HTMLElement) => e.getBoundingClientRect().top < window.innerHeight
    els.filter(onScreen).forEach(e => e.classList.add('is-in'))
    document.documentElement.classList.add('rise-ready')
    io = new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (!e.isIntersecting) continue
        e.target.classList.add('is-in')
        io!.unobserve(e.target)
      }
    }, { rootMargin: '0px 0px -8% 0px', threshold: 0.05 })
    els.forEach(e => io!.observe(e))
    // never leave anything hidden: a safety net for elements the observer misses
    setTimeout(() => els.forEach((e) => { if (e.getBoundingClientRect().top < innerHeight) e.classList.add('is-in') }), 2500)
  })
}

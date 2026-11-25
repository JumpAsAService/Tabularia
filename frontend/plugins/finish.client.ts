// Starts the surface finish at boot, so the saved choice holds on the pages with no settings menu.
export default defineNuxtPlugin(() => {
  useFinish()
})

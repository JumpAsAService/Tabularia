// English — the BASE locale (source of truth). Other locales fall back to this
// for any missing key, so the UI is never broken while translations catch up.
export default {
  nav: {
    explore: 'Explore',
    flows: 'Flows',
    datasources: 'Datasources',
    lineage: 'Lineage',
    viewer: 'Viewer',
    connections: 'Connections',
    runs: 'Runs',
    queue: 'Queue',
    monitoring: 'Monitoring',
    audit: 'Audit',
    admin: 'Admin',
  },
  settings: {
    title: 'Settings',
    theme: 'Theme',
    light: 'Light',
    dark: 'Dark',
    engine: 'Preferred engine',
    engineHint: 'Default for Viewer and new flows. On creation it is still chosen by hand.',
    language: 'Language',
    signOut: 'Sign out',
    admin: 'admin',
  },
  login: {
    subtitle: 'Sign in to continue',
    email: 'Email',
    password: 'Password',
    signIn: 'Sign in',
    signingIn: 'Signing in…',
  },
}

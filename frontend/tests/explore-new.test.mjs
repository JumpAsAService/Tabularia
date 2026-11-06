// Explore's «New»: where it creates. In the open folder; at the top level, where one lands
// after signing in and nothing but folders can be, in one's own folder.
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'
import { readFileSync } from 'node:fs'

register('./alias-hooks.mjs', import.meta.url)
const { personalFolderOf, creationFolder } = await import('../composables/creationFolder.ts')

const folder = (id, name, extra = {}) => ({ id, name, description: '', parent_id: null, owner_id: null, personal_of: null, ...extra })
const PROJECTS = [
  folder(1, 'Sample'),
  folder(2, 'admin', { owner_id: 7, personal_of: 7 }),
  folder(3, 'elena.moretti', { owner_id: 9, personal_of: 9 }),
  folder(4, 'Reports', { parent_id: 1 }),
]

test('inside a folder, New creates there', () => {
  assert.equal(creationFolder(4, PROJECTS, 7), 4)
  assert.equal(creationFolder(3, PROJECTS, 7), 3) // an administrator in someone else's folder
})

test('at the top level, New creates in one’s own folder, not in someone else’s', () => {
  assert.equal(creationFolder(null, PROJECTS, 7), 2)
  assert.equal(creationFolder(null, PROJECTS, 9), 3)
  assert.equal(personalFolderOf(PROJECTS, 9)?.name, 'elena.moretti')
})

test('without a folder of one’s own (an observer, a service account) there is nowhere at the top level', () => {
  assert.equal(creationFolder(null, PROJECTS, 42), null)
  assert.equal(creationFolder(null, PROJECTS, null), null)
  assert.equal(creationFolder(null, [], 7), null)
})

test('Explore offers the flow and the other objects wherever New has a folder to create in', () => {
  const vue = readFileSync(new URL('../components/ProjectBrowser.vue', import.meta.url), 'utf8')
  assert.match(vue, /<div v-if="!treeLoaded \|\| target !== null \|\| isSuper" class="newwrap">/)
  assert.match(vue, /<template v-if="target !== null">\s*<div v-if="isSuper \|\| canManage" class="menu-sep" \/>\s*<div v-if="currentId === null" class="menu-note">/)
  // every «create» goes where target says, none to the open folder alone
  for (const page of ['flow-group', 'script', 'notebook', 'sql-script']) {
    assert.ok(vue.includes(`navigateTo(\`/${page}?project=\${target.value}\`)`), page)
  }
  assert.equal((vue.match(/const folder = target\.value/g) ?? []).length, 2) // report, dashboard
})

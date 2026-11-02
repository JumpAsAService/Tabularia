// The engine's settings in Administration → Settings without a browser: the shapes the
// panel checks before saving or testing the ClickHouse connection are the engine's own
// (backend/app/core/panel_settings.py FORMATS, tested with the same samples in
// backend/tests/test_clickhouse_connection_panel.py), and the engine's items reach the
// panel with their kind, choices and limits.
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'

register('./alias-hooks.mjs', import.meta.url)
const { TEXT_FORMATS } = await import('../composables/useEngineSettings.ts')
const { fromEngine } = await import('../composables/usePanelSettings.ts')

const SAMPLES = {
  host: { good: ['ch.example.com', '10.0.0.7', '::1', 'chpanel'], bad: ['https://ch.example.com', 'ch.example.com/db', 'ch example', 'ch.example.com:8443/x', '---'] },
  name: { good: ['tab-prod_1', 'default', '_x'], bad: ['tab;DROP', 'due parole', '-x'] },
  url: { good: ['https://s3.fr-par.scw.cloud', 'http://minio:9000', 'https://s3.example.com/path'], bad: ['s3.fr-par.scw.cloud', 'ftp://x', 'https://x?y=1'] },
  user: { good: ['svc@acme', 'tab', 'a.b-c'], bad: ['due parole', 'tab\n', ''] },
}

test('the panel checks the texts with the engine’s shapes', () => {
  assert.deepEqual(Object.keys(TEXT_FORMATS).sort(), Object.keys(SAMPLES).sort())
  for (const [format, { good, bad }] of Object.entries(SAMPLES)) {
    for (const s of good) assert.ok(TEXT_FORMATS[format].test(s), `${format}: «${s}» is good`)
    for (const s of bad) assert.ok(!TEXT_FORMATS[format].test(s), `${format}: «${s}» is not`)
  }
})

test('a text reaches the panel with its length as limit, a secret as set or not', () => {
  const host = fromEngine({ key: 'clickhouse_external.host', category: 'clickhouse_connection', unit: '', kind: 'str',
    format: 'host', optional: false, rollout: ['engine'], min: 0, max: 253, env_var: 'CLICKHOUSE_EXTERNAL__HOST',
    env_value: 'chmotore', value: 'chpanel', applied: 'chpanel', updated_at: null, updated_by: null }, ['clickhouse'])
  assert.equal(host.maxLength, 253)
  assert.equal(host.min, null)
  assert.equal(host.format, 'host')
  assert.equal(host.off, false) // the connection's box is where ClickHouse is turned on: never greyed out
  const secret = fromEngine({ key: 'clickhouse_external.password', category: 'clickhouse_connection', unit: '', kind: 'secret',
    rollout: ['engine'], min: 0, max: 4096, env_var: 'CLICKHOUSE_EXTERNAL__PASSWORD', env_value: true, value: null,
    applied: true, updated_at: null, updated_by: null }, [])
  assert.equal(secret.kind, 'secret')
  assert.equal(secret.envValue, true)
  const transport = fromEngine({ key: 'clickhouse_external.transport', category: 'clickhouse_connection', unit: '', kind: 'choice',
    choices: ['s3', 'push'], rollout: ['engine'], min: 0, max: 0, env_var: 'CLICKHOUSE_EXTERNAL__TRANSPORT', env_value: 's3',
    value: null, applied: null, updated_at: null, updated_by: null }, [])
  assert.deepEqual(transport.choices, ['s3', 'push'])
})

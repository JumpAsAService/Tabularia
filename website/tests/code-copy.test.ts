// The copy button of the docs' code blocks: app/utils/code-copy.ts. Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { commandText, terminalLabel } from '../app/utils/code-copy.ts'

test('a block without prompts is copied as written, without the last newline', () => {
  assert.equal(commandText('kubectl get pods \\\n  --all-namespaces\n'), 'kubectl get pods \\\n  --all-namespaces')
  assert.equal(commandText('# a comment\nls'), '# a comment\nls')
})

test('with prompts, only the commands, without the prompt', () => {
  assert.equal(commandText('$ docker compose ps\nNAME   STATUS\napp    Up\n$ docker compose logs app'),
    'docker compose ps\ndocker compose logs app')
  assert.equal(commandText('PS> wsl --shutdown\nPS C:\\Users\\me> notepad x'), 'wsl --shutdown\nnotepad x')
})

test('the terminal label: declared, PowerShell anywhere, shells only inside a system block', () => {
  assert.equal(terminalLabel('bash', null, 'Ubuntu terminal'), 'Ubuntu terminal')
  assert.equal(terminalLabel('powershell', null), 'PowerShell')
  assert.equal(terminalLabel('powershell', 'windows'), 'PowerShell')
  assert.equal(terminalLabel('bash', null), '')
  assert.equal(terminalLabel('bash', 'windows'), 'Ubuntu terminal (WSL)')
  assert.equal(terminalLabel('bash', 'linux-macos'), 'Terminal')
  assert.equal(terminalLabel('bash', 'macos'), 'Terminal')
  assert.equal(terminalLabel('ini', 'windows'), '')
  assert.equal(terminalLabel('yaml', null), '')
})

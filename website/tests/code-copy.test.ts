// The copy button of the docs' code blocks: app/utils/code-copy.ts. Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { commandText } from '../app/utils/code-copy.ts'

test('a block without prompts is copied as written, without the last newline', () => {
  assert.equal(commandText('kubectl get pods \\\n  --all-namespaces\n'), 'kubectl get pods \\\n  --all-namespaces')
  assert.equal(commandText('# a comment\nls'), '# a comment\nls')
})

test('with prompts, only the commands, without the prompt', () => {
  assert.equal(commandText('$ docker compose ps\nNAME   STATUS\napp    Up\n$ docker compose logs app'),
    'docker compose ps\ndocker compose logs app')
  assert.equal(commandText('PS> wsl --shutdown\nPS C:\\Users\\me> notepad x'), 'wsl --shutdown\nnotepad x')
})

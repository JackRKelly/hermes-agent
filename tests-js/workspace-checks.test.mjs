import { expect, test } from 'vitest'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { spawnSync } from 'node:child_process'
import yaml from 'js-yaml'

const runner = path.resolve(import.meta.dirname, '../.github/scripts/run-workspace-checks.mjs')

function fixture(run) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'workspace-checks-'))
  try {
    fs.mkdirSync(path.join(root, 'node_modules'))
    fs.writeFileSync(path.join(root, 'package.json'), JSON.stringify({ private: true, workspaces: ['apps/*', 'tests-js'] }))
    for (const [dir, scripts] of [
      ['apps/desktop', { 'check:test:ui': 'npm run test:ui', 'test:ui': 'node record.cjs', 'check:lint': 'node record.cjs', check: 'node never.cjs' }],
      ['tests-js', { check: 'node record.cjs' }],
    ]) {
      const pkg = path.join(root, dir)
      fs.mkdirSync(pkg, { recursive: true })
      const name = dir.replaceAll('/', '-')
      fs.writeFileSync(path.join(pkg, 'package.json'), JSON.stringify({ name, version: '1.0.0', scripts }))
      fs.writeFileSync(path.join(pkg, 'record.cjs'), `require('node:fs').appendFileSync(${JSON.stringify(path.join(root, 'events.jsonl'))}, JSON.stringify({pkg:${JSON.stringify(dir)},argv:process.argv.slice(2),workers:process.env.VITEST_MAX_WORKERS||null})+'\\n')`)
      fs.symlinkSync(pkg, path.join(root, 'node_modules', name), process.platform === 'win32' ? 'junction' : 'dir')
    }
    const cli = (args, env = {}) => spawnSync(process.execPath, [runner, ...args], {
      cwd: root, encoding: 'utf8', env: { ...process.env, ...env },
    })
    run(cli, root)
  } finally { fs.rmSync(root, { recursive: true, force: true }) }
}

test('shared-runner partitions isolate both UI shards and retain every other workspace check', () => fixture((cli) => {
  const all = cli(['--list'])
  expect(all.status, all.stderr).toBe(0)
  expect(all.stdout.trim().split('\n')).toEqual(['apps/desktop :: check:test:ui', 'apps/desktop :: check:lint', 'tests-js :: check'])
  const other = cli(['--list', '--partition', 'other'])
  expect(other.status, other.stderr).toBe(0)
  expect(other.stdout.trim().split('\n')).toEqual(['apps/desktop :: check:lint', 'tests-js :: check'])
  for (const shard of ['1/2', '2/2']) {
    const ui = cli(['--list', '--partition', `desktop-ui-${shard}`])
    expect(ui.status, ui.stderr).toBe(0)
    expect(ui.stdout.trim()).toBe(`apps/desktop :: check:test:ui -- -- --shard=${shard}`)
  }
}), 15_000)

test('UI partition forwards its shard through both npm wrappers and caps nested Vitest workers', () => fixture((cli, root) => {
  const result = cli(['--partition', 'desktop-ui-1/2', '--vitest-workers', '3'])
  expect(result.status, result.stderr).toBe(0)
  const events = fs.readFileSync(path.join(root, 'events.jsonl'), 'utf8').trim().split('\n').map(JSON.parse)
  expect(events).toEqual([{ pkg: 'apps/desktop', argv: ['--shard=1/2'], workers: '3' }])
}), 15_000)

test('invalid resource controls fail closed before running or listing any checks', () => fixture((cli, root) => {
  for (const args of [
    ['--partition', 'desktop-ui-3/2'], ['--partition'], ['--unknown'],
    ['--concurrency', 'nope'], ['--concurrency', '0'], ['--concurrency', '1.5'], ['--concurrency'],
    ['--vitest-workers', 'NaN'], ['--vitest-workers', '0'], ['--vitest-workers', 'Infinity'], ['--vitest-workers'],
  ]) {
    const result = cli(args)
    expect(result.status, JSON.stringify(args)).not.toBe(0)
    expect(result.stderr, JSON.stringify(args)).toMatch(/invalid|unknown|expected/i)
  }
  expect(fs.existsSync(path.join(root, 'events.jsonl'))).toBe(false)
}), 15_000)

test('default CLI preserves inherited workers and reports all failures after running every command', () => fixture((cli, root) => {
  const result = cli([], { VITEST_MAX_WORKERS: '7' })
  expect(result.status, result.stderr).toBe(0)
  const events = fs.readFileSync(path.join(root, 'events.jsonl'), 'utf8').trim().split('\n').map(JSON.parse)
  expect(events).toHaveLength(3)
  expect(events.every(event => event.workers === '7' && event.argv.length === 0)).toBe(true)
  expect(result.stdout).toContain('all 3 checks passed')

  const times = path.join(root, 'times.jsonl')
  for (const dir of ['apps/desktop', 'tests-js']) {
    fs.writeFileSync(path.join(root, dir, 'record.cjs'), `
      const fs = require('node:fs'); const start = Date.now();
      setTimeout(() => {
        fs.appendFileSync(${JSON.stringify(times)}, JSON.stringify({start,end:Date.now(),script:process.env.npm_lifecycle_event})+'\\n');
        process.exitCode = process.env.npm_lifecycle_event === 'check:lint' ? 1 : 0;
      }, 100);
    `)
  }
  const failed = cli(['--concurrency', '1', '--vitest-workers', '1'])
  expect(failed.status).toBe(1)
  expect(failed.stderr).toContain('1 of 3 checks failed')
  const intervals = fs.readFileSync(times, 'utf8').trim().split('\n').map(JSON.parse)
  expect(intervals.map(interval => interval.script)).toEqual(['test:ui', 'check:lint', 'check'])
  expect(intervals.slice(1).every((interval, index) => interval.start >= intervals[index].end)).toBe(true)
}), 15_000)

test('workflow opts shared runners into isolated UI shards without changing the upstream lane or deadlines', () => {
  const workflow = yaml.load(fs.readFileSync(path.resolve(import.meta.dirname, '../.github/workflows/js-tests.yml'), 'utf8'))
  const job = workflow.jobs.check
  expect(job.name).toBe('${{ matrix.partition == \'all\' && \'JS & TS checks\' || format(\'JS & TS checks ({0})\', matrix.partition) }}')
  expect(job.strategy?.['fail-fast']).toBe(false)
  expect(job.strategy['max-parallel']).toBe(1)
  expect(job.strategy.matrix.partition).toBe('${{ fromJSON(vars.HERMES_CI_JS_RESOURCE_PROFILE == \'shared\' && \'["desktop-ui-1/2","desktop-ui-2/2","other"]\' || \'["all"]\') }}')
  expect(job['runs-on']).toBe('${{ vars.HERMES_CI_LINUX_RUNNER || \'blacksmith-32vcpu-ubuntu-2404\' }}')
  expect(job['timeout-minutes']).toBe(30)
  const step = job.steps.find((step) => step.name === 'Run all workspace checks')
  const desktopScripts = JSON.parse(fs.readFileSync(path.resolve(import.meta.dirname, '../apps/desktop/package.json'), 'utf8')).scripts
  // The extra npm separator in the runner is required by this real chain.
  expect(desktopScripts['check:test:ui']).toBe('npm run test:ui')
  expect(desktopScripts['test:ui']).toBe('vitest run --project ui')
  expect(step.env.CHECK_PARTITION).toBe('${{ matrix.partition }}')
  fixture((cli, root) => {
    fs.mkdirSync(path.join(root, '.github/scripts'), { recursive: true })
    fs.copyFileSync(runner, path.join(root, '.github/scripts/run-workspace-checks.mjs'))
    for (const partition of ['desktop-ui-1/2', 'desktop-ui-2/2', 'other']) {
      const result = spawnSync('bash', ['-e', '-o', 'pipefail', '-c', step.run], {
        cwd: root, encoding: 'utf8', env: { ...process.env, CHECK_PARTITION: partition },
      })
      expect(result.status, result.stderr).toBe(0)
    }
    const events = fs.readFileSync(path.join(root, 'events.jsonl'), 'utf8').trim().split('\n').map(JSON.parse)
    expect(events).toEqual([
      { pkg: 'apps/desktop', argv: ['--shard=1/2'], workers: '3' },
      { pkg: 'apps/desktop', argv: ['--shard=2/2'], workers: '3' },
      { pkg: 'apps/desktop', argv: [], workers: '1' },
      { pkg: 'tests-js', argv: [], workers: '1' },
    ])
  })
}, 15_000)

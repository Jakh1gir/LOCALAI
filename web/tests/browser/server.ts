import { spawn } from 'node:child_process';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createInterface } from 'node:readline';
import type { TestContext } from 'node:test';
import { fileURLToPath } from 'node:url';
import { setTimeout } from 'node:timers/promises';

export async function startAccessServer(t: TestContext) {
  const folder = await mkdtemp(join(tmpdir(), 'localai-access-'));
  const root = fileURLToPath(new URL('../../../', import.meta.url));
  const child = spawn(process.env.LOCALAI_TEST_PYTHON ?? 'python', [
    '-m', 'tests.e2e.serve_access', join(folder, 'browser.sqlite3'),
  ], { cwd: root, stdio: ['ignore', 'pipe', 'pipe'] });
  let errors = '';
  child.stderr.on('data', (chunk) => { errors += chunk.toString(); });
  t.after(async () => {
    if (child.exitCode === null) {
      const stopped = new Promise<void>((resolve) => child.once('exit', () => resolve()));
      child.kill('SIGTERM');
      await stopped;
    }
    await rm(folder, { recursive: true, force: true });
  });
  const ready = new Promise<{ url: string; login: string; password: string }>((resolve, reject) => {
    const lines = createInterface({ input: child.stdout });
    const timer = globalThis.setTimeout(() => reject(new Error('Сервер не запустился за 30 секунд.')), 30_000);
    child.once('error', reject);
    child.once('exit', (code) => reject(new Error(`Сервер завершился с кодом ${code}. ${errors}`)));
    lines.once('line', (line) => {
      globalThis.clearTimeout(timer);
      lines.close();
      try { resolve(JSON.parse(line)); } catch (error) { reject(error); }
    });
  });
  const config = await ready;
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try {
      if ((await fetch(`${config.url}/api/v1/system/health`)).ok) return config;
    } catch { /* Сокет открыт до запуска ASGI. */ }
    await setTimeout(50);
  }
  throw new Error(`Сервер не ответил. ${errors}`);
}

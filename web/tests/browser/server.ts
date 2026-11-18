import { spawn } from 'node:child_process';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { delimiter, join } from 'node:path';
import { createInterface } from 'node:readline';
import type { TestContext } from 'node:test';
import { fileURLToPath } from 'node:url';
import { setTimeout } from 'node:timers/promises';
import assert from 'node:assert/strict';
import { chromium, type Browser, type Page } from 'playwright-core';
import { preview } from 'vite';

export async function startAccessServer(t: TestContext) {
  const folder = await mkdtemp(join(tmpdir(), 'localai-access-'));
  const root = fileURLToPath(new URL('../../../', import.meta.url));
  const serverRoot = process.env.LOCALAI_TEST_SERVER_ROOT ?? root;
  const fixture = join(root, 'tests/e2e/serve_access.py');
  const child = spawn(
    process.env.LOCALAI_TEST_PYTHON ?? 'python',
    [fixture, join(folder, 'browser.sqlite3')],
    {
      cwd: serverRoot,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: {
        ...process.env,
        PYTHONPATH: [serverRoot, process.env.PYTHONPATH].filter(Boolean).join(delimiter),
      },
    },
  );
  const closed = new Promise<void>((resolve) => child.once('close', () => resolve()));
  let errors = '';
  child.stderr.on('data', (chunk) => {
    errors += chunk.toString();
  });
  t.after(async () => {
    if (child.exitCode === null && child.signalCode === null) child.kill('SIGTERM');
    await closed;
    await rm(folder, { recursive: true, force: true });
  });
  const ready = new Promise<{ url: string; login: string; password: string }>((resolve, reject) => {
    const lines = createInterface({ input: child.stdout });
    const timer = globalThis.setTimeout(
      () => reject(new Error('Сервер не запустился за 30 секунд.')),
      30_000,
    );
    const failed = (error: Error) => {
      globalThis.clearTimeout(timer);
      lines.close();
      reject(error);
    };
    child.once('error', failed);
    child.once('exit', (code) => failed(new Error(`Сервер завершился с кодом ${code}. ${errors}`)));
    lines.once('line', (line) => {
      globalThis.clearTimeout(timer);
      lines.close();
      try {
        resolve(JSON.parse(line));
      } catch (error) {
        reject(error);
      }
    });
  });
  const config = await ready;
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try {
      if ((await fetch(`${config.url}/api/v1/system/health`)).ok) return { ...config, folder };
    } catch {
      /* Сокет открыт до запуска ASGI. */
    }
    await setTimeout(50);
  }
  throw new Error(`Сервер не ответил. ${errors}`);
}

export async function openAccessPage(t: TestContext) {
  const config = await startAccessServer(t);
  const root = fileURLToPath(new URL('../../', import.meta.url));
  const web = await preview({
    root,
    logLevel: 'error',
    preview: { host: '127.0.0.1', port: 0, strictPort: true, proxy: { '/api/v1': config.url } },
  });
  let browser: Browser | undefined;
  t.after(async () => {
    await browser?.close();
    await web.close();
  });
  browser = await chromium.launch({ executablePath: process.env.CHROMIUM_EXECUTABLE });
  const address = web.httpServer.address();
  assert.ok(address && typeof address !== 'string');
  const origin = `http://127.0.0.1:${address.port}`;
  const context = await browser.newContext({
    baseURL: origin,
    viewport: { width: 1600, height: 1000 },
    permissions: ['clipboard-read', 'clipboard-write'],
  });
  const page = await context.newPage();
  return { page, browser, origin, config };
}

export async function confirmAccessAction(page: Page, action: string) {
  const dialog = page.getByRole('dialog').filter({
    has: page.getByRole('button', { name: action, exact: true }),
  });
  await dialog.getByRole('button', { name: action, exact: true }).click();
  await dialog.waitFor({ state: 'hidden' });
}

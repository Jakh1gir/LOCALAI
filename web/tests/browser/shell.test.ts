import assert from 'node:assert/strict';
import { test, type TestContext } from 'node:test';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright-core';
import type { Browser } from 'playwright-core';
import { createServer, preview } from 'vite';
import { createApiClient } from '../../src/api/client.ts';
import { text } from '../../src/text.ts';
import { startAccessServer } from './server.ts';

const root = fileURLToPath(new URL('../../', import.meta.url));
const host = '127.0.0.1';
const timeout = { timeout: 30_000 };

async function openPage(t: TestContext, mode: 'development' | 'server' | 'static', apiUrl?: string) {
  const options = { root, logLevel: 'error' as const };
  const web = mode === 'static'
    ? await preview({ ...options, preview: { host, port: 0, strictPort: true } })
    : await createServer({ ...options, mode, server: {
      host, port: 0, strictPort: true,
      ...(apiUrl ? { proxy: { '/api/v1': apiUrl } } : {}),
    } });
  let browser: Browser | undefined;
  t.after(async () => {
    await browser?.close();
    await web.close();
  });
  if ('listen' in web) await web.listen();
  const address = web.httpServer?.address();
  assert.ok(address && typeof address !== 'string');
  const origin = `http://${host}:${address.port}`;

  browser = await chromium.launch({ executablePath: process.env.CHROMIUM_EXECUTABLE });
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const outside: string[] = [];
  await context.route('**/*', async (route) => {
    const url = new URL(route.request().url());
    if (url.origin === origin) {
      await route.continue();
    } else {
      outside.push(url.href);
      await route.abort();
    }
  });
  const page = await context.newPage();
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  t.after(() => {
    assert.deepEqual(outside, [], 'Все запросы должны оставаться на локальном сервере.');
    assert.deepEqual(errors, [], 'Ошибки JavaScript в браузере.');
  });
  return { page, origin };
}

test('заглушка, глубокие ссылки и навигация с клавиатуры', timeout, async (t) => {
  const { page, origin } = await openPage(t, 'development');
  const initial = await page.goto(`${origin}/kks`);
  assert.equal(initial?.status(), 200);
  await page.getByRole('heading', { name: text.kks, level: 1 }).waitFor();
  await page.getByRole('status').filter({ hasText: text.mockReady }).waitFor();
  const nav = page.getByRole('navigation', { name: text.navigation });
  assert.equal(await nav.getByRole('link', { name: text.kks }).getAttribute('aria-current'), 'page');

  await page.keyboard.press('Control+k');
  const dialog = page.getByRole('dialog', { name: text.search });
  await dialog.waitFor();
  const search = dialog.getByRole('combobox', { name: text.searchSections });
  assert.ok(await search.evaluate((input) => input === document.activeElement));
  await search.fill('проверки');
  await search.press('Enter');
  await dialog.waitFor({ state: 'hidden' });
  const heading = page.getByRole('heading', { name: text.checks, level: 1 });
  await heading.waitFor();
  assert.ok(await heading.evaluate((element) => element === document.activeElement));
  assert.equal(new URL(page.url()).pathname, '/checks');
  await page.goBack();
  await page.getByRole('heading', { name: text.kks, level: 1 }).waitFor();
  await page.keyboard.press('Control+k');
  await dialog.waitFor();
  await page.keyboard.press('Escape');
  await dialog.waitFor({ state: 'hidden' });

  const api = createApiClient({ baseUrl: origin });
  const [dataset] = (await api.listDatasets()).items;
  assert.equal((await api.countRecords(dataset.id)).count, 10_000);
  const first = await api.queryRecords(dataset.id);
  const second = await api.queryRecords(dataset.id, { cursor: first.next_cursor });
  assert.equal(first.items.length, 50);
  assert.equal(second.items.length, 50);
  assert.notEqual(first.items[0].id, second.items[0].id);
});

test('статическая сборка открывает неизвестный адрес и загружает локальные шрифты', timeout, async (t) => {
  const { page, origin } = await openPage(t, 'static');
  const response = await page.goto(`${origin}/not-a-section`);
  assert.equal(response?.status(), 200);
  await page.getByRole('heading', { name: text.notFound, level: 1 }).waitFor();
  assert.equal(await page.getByRole('link', { name: text.backToRegistry }).getAttribute('href'), '/registries');
  await page.getByRole('link', { name: text.backToRegistry }).click();
  await page.getByRole('heading', { name: text.registry, level: 1 }).waitFor();
  const fonts = await page.evaluate(async () => {
    await document.fonts.ready;
    return [...document.fonts].filter((font) => font.family === 'Inter' && font.status === 'loaded').length;
  });
  assert.equal(fonts, 2);
});

/** Токен сессии для клиента из Node: браузер хранит его в cookie сам. */
async function signIn(baseUrl: string, login: string, password: string): Promise<string> {
  const response = await fetch(new URL('/api/v1/auth/login', baseUrl), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ login, password }),
  });
  assert.equal(response.status, 200, 'Вход в LOCALAI не выполнен.');
  const cookie = response.headers.getSetCookie().find((item) => item.startsWith('localai_session='));
  assert.ok(cookie, 'Сервер не выдал cookie сессии.');
  return cookie.split(';', 1)[0].slice('localai_session='.length);
}

function withToken(baseUrl: string, token: string) {
  return createApiClient({
    baseUrl,
    fetchRequest: (request) => {
      const headers = new Headers(request.headers);
      headers.set('Authorization', `Bearer ${token}`);
      return fetch(new Request(request, { headers }));
    },
  });
}

const serverLogin = process.env.LOCALAI_LOGIN;
const serverPassword = process.env.LOCALAI_PASSWORD;

test('оболочка входит в LOCALAI и получает ответ через прокси Vite', {
  timeout: 90_000,
}, async (t) => {
  const config = process.env.LOCALAI_API_URL && serverLogin && serverPassword
    ? { url: process.env.LOCALAI_API_URL, login: serverLogin, password: serverPassword }
    : await startAccessServer(t);
  const { page, origin } = await openPage(t, 'server', config.url);
  await page.goto(`${origin}/registries`);
  await page.getByRole('heading', { name: text.loginTitle, level: 1 }).waitFor();
  await page.getByLabel(text.loginField).fill(config.login);
  await page.getByLabel(text.passwordField).fill(config.password);
  await page.getByRole('button', { name: text.signIn }).click();
  await page.getByRole('heading', { name: text.registry, level: 1 }).waitFor();
  await page.getByRole('status').filter({ hasText: text.serverReady }).waitFor();
  const api = withToken(origin, await signIn(origin, config.login, config.password));
  const direct = withToken(
    config.url,
    await signIn(config.url, config.login, config.password),
  );
  assert.deepEqual(await api.health(), await direct.health());
  assert.deepEqual(await api.listDatasets(), await direct.listDatasets());

  await page.getByRole('button', { name: text.signOut }).click();
  await page.getByRole('heading', { name: text.loginTitle, level: 1 }).waitFor();
});

import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
import { mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { test, type TestContext } from 'node:test';
import type { Browser, Locator, Page } from 'playwright-core';
import type { Role, User } from '../../src/api/types.ts';
import { text } from '../../src/text.ts';
import { confirmAccessAction, openAccessPage } from './server.ts';

const copy = text.access;
const ruleText = text.kksRule;

async function write(page: Page, method: string, path: string, data?: unknown) {
  const response = await page.request.fetch(path, {
    method,
    data,
    headers: { 'Idempotency-Key': randomBytes(16).toString('hex') },
  });
  assert.ok(response.ok(), `${method} ${path} — HTTP ${response.status()}`);
  return response.json();
}

async function screenshot(page: Page, name: string) {
  if (!process.env.LOCALAI_SCREENSHOTS) return;
  await mkdir(process.env.LOCALAI_SCREENSHOTS, { recursive: true });
  await page.screenshot({
    path: join(process.env.LOCALAI_SCREENSHOTS, `${name}.webp`),
    type: 'webp',
    fullPage: true,
  });
}

async function login(page: Page, login: string, password: string) {
  await page.getByLabel(text.loginField, { exact: true }).fill(login);
  await page.getByLabel(text.passwordField, { exact: true }).fill(password);
  await page.getByRole('button', { name: text.signIn, exact: true }).click();
}

async function signedInUser(
  t: TestContext,
  browser: Browser,
  origin: string,
  user: User & { temporary_password: string },
) {
  const context = await browser.newContext({ baseURL: origin });
  t.after(() => context.close());
  const page = await context.newPage();
  await page.goto('/registries');
  await login(page, user.login, user.temporary_password);
  await page.getByRole('heading', { name: text.changeTitle }).waitFor();
  const password = randomBytes(24).toString('base64url');
  await page.getByLabel(text.currentPassword, { exact: true }).fill(user.temporary_password);
  await page.getByLabel(text.newPassword, { exact: true }).fill(password);
  await page.getByLabel(text.repeatPassword, { exact: true }).fill(password);
  await page.getByRole('button', { name: text.savePassword }).click();
  await page.getByRole('heading', { name: text.registry, level: 1 }).waitFor();
  return page;
}

async function audit(page: Page) {
  return (await (await page.request.get('/api/v1/audit?limit=100')).json()).items;
}

async function cancelAction(
  page: Page,
  trigger: Locator,
  title: string,
  consequence: string,
  path: string,
  cancel: 'escape' | 'button' | 'enter',
) {
  const beforeAudit = await audit(page);
  const before = await (await page.request.get(path)).json();
  const outgoing: string[] = [];
  const listener = (request: { method(): string; url(): string }) => {
    if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(request.method())) outgoing.push(request.url());
  };
  page.on('request', listener);
  try {
    await trigger.click();
    const dialog = page.getByRole('dialog', { name: title, exact: true });
    await dialog.waitFor();
    await dialog.getByText(consequence, { exact: true }).waitFor();
    const button = dialog.getByRole('button', { name: copy.cancel, exact: true });
    assert.equal(await button.evaluate((element) => element === document.activeElement), true);
    if (cancel === 'escape') await page.keyboard.press('Escape');
    else if (cancel === 'enter') await page.keyboard.press('Enter');
    else await button.click();
    await dialog.waitFor({ state: 'hidden' });
    assert.equal(await trigger.evaluate((element) => element === document.activeElement), true);
    assert.deepEqual(outgoing, [], 'Отмена не отправляет изменяющие запросы.');
    assert.deepEqual(await audit(page), beforeAudit, 'Журнал после отмены не изменился.');
    assert.deepEqual(await (await page.request.get(path)).json(), before);
  } finally {
    page.off('request', listener);
  }
}

async function confirmationScenarios(
  page: Page,
  userPage: Page,
  user: User,
  role: Role,
  network: { id: string; network: string },
) {
  const table = page.getByRole('table', { name: copy.users, exact: true });
  const row = table.getByRole('row').filter({ hasText: user.login });
  await cancelAction(
    page,
    row.getByRole('button', { name: copy.resetPassword, exact: true }),
    copy.confirmReset(user.name),
    copy.resetConsequence,
    `/api/v1/users/${user.id}`,
    'enter',
  );
  assert.equal((await userPage.request.get('/api/v1/auth/me')).status(), 200);
  await cancelAction(
    page,
    row.getByRole('button', { name: copy.block, exact: true }),
    copy.confirmBlock(user.name),
    copy.blockConsequence,
    `/api/v1/users/${user.id}`,
    'escape',
  );
  await row.getByRole('button', { name: copy.resetPassword, exact: true }).click();
  await screenshot(page, 'admin-confirm-reset');
  const dialog = page.getByRole('dialog', { name: copy.confirmReset(user.name), exact: true });
  await page.keyboard.press('Tab');
  assert.equal(
    await dialog
      .getByRole('button', { name: copy.resetPassword, exact: true })
      .evaluate((element) => element === document.activeElement),
    true,
  );
  await page.keyboard.press('Enter');
  const secret = page.getByRole('dialog', { name: copy.temporaryPassword, exact: true });
  await secret.waitFor();
  await secret.getByRole('button', { name: copy.close, exact: true }).click();
  assert.equal((await userPage.request.get('/api/v1/auth/me')).status(), 401);
  await userPage.reload();
  await userPage.getByRole('heading', { name: text.loginTitle, exact: true }).waitFor();
  assert.equal(
    (await audit(page)).filter(
      (item: { action: string; entity_id: string }) =>
        item.action === 'user.password_reset' && item.entity_id === user.id,
    ).length,
    1,
  );

  await row.getByRole('button', { name: copy.block, exact: true }).click();
  await confirmAccessAction(page, copy.block);
  await row.getByText(copy.blocked, { exact: true }).waitFor();
  await row.getByRole('button', { name: copy.unblock, exact: true }).click();
  await row.getByRole('button', { name: copy.block, exact: true }).waitFor();
  assert.equal(await page.getByRole('dialog').count(), 0);

  await page.getByRole('button', { name: copy.roles, exact: true }).click();
  const roleRow = page
    .getByRole('table', { name: copy.roles, exact: true })
    .getByRole('row')
    .filter({ has: page.getByRole('button', { name: role.name, exact: true }) });
  await cancelAction(
    page,
    roleRow.getByRole('button', { name: copy.disable, exact: true }),
    copy.confirmDisableRole(role.name),
    copy.disableRoleConsequence(1),
    `/api/v1/roles/${role.id}`,
    'button',
  );
  await roleRow.getByRole('button', { name: copy.disable, exact: true }).click();
  await confirmAccessAction(page, copy.disableRoleAction);
  await roleRow.getByRole('button', { name: copy.restore, exact: true }).click();
  await roleRow.getByRole('button', { name: copy.disable, exact: true }).waitFor();
  assert.equal(await page.getByRole('dialog').count(), 0);

  await page.getByRole('button', { name: copy.networks, exact: true }).click();
  const networkRow = page
    .getByRole('table', { name: copy.networks, exact: true })
    .getByRole('row')
    .filter({ hasText: network.network });
  await cancelAction(
    page,
    networkRow.getByRole('button', { name: copy.remove, exact: true }),
    copy.confirmRemoveNetwork(network.network),
    copy.removeNetworkConsequence,
    '/api/v1/system/allowed-ips',
    'escape',
  );
  await networkRow.getByRole('button', { name: copy.remove, exact: true }).click();
  await confirmAccessAction(page, copy.removeNetworkAction);
  await networkRow.waitFor({ state: 'detached' });
}

async function rulesScenarios(page: Page, site: { id: string }, otherSite: { id: string }) {
  await page.getByRole('button', { name: copy.kksRules, exact: true }).click();
  const original = await (await page.request.get('/api/v1/kks/rules')).json();
  await page.getByRole('code').filter({ hasText: original.signal_pattern }).waitFor();
  const pattern = page.getByLabel(ruleText.pattern, { exact: true });
  await pattern.fill('[');
  const rejected = page.waitForResponse(
    (response) => response.url().endsWith('/kks/rules') && response.request().method() === 'PUT',
  );
  await page.getByRole('button', { name: ruleText.save, exact: true }).click();
  const response = await rejected;
  assert.equal(response.status(), 400);
  const error = (await response.json()).error;
  assert.equal(error.code, 'kks_rule_invalid');
  assert.equal(await pattern.getAttribute('aria-invalid'), 'true');
  await page.getByRole('alert').filter({ hasText: error.message }).waitFor();
  assert.deepEqual(await (await page.request.get('/api/v1/kks/rules')).json(), original);

  const wanted = 'X[A-Z][0-9]{2}|-[0-9]{2}';
  await pattern.fill(wanted);
  await page.getByRole('button', { name: ruleText.save, exact: true }).click();
  await page.getByRole('status').filter({ hasText: ruleText.saved }).waitFor();
  const form = page.getByRole('form', { name: ruleText.check, exact: true });
  await form.getByLabel(ruleText.site, { exact: true }).selectOption(site.id);
  await form.getByLabel(ruleText.code, { exact: true }).fill('01TK12CKA110-03');
  const resolved = page.waitForResponse((response) => response.url().includes('/kks/resolve?'));
  await form.getByRole('button', { name: ruleText.check, exact: true }).click();
  const result = await (await resolved).json();
  assert.equal(result.via, 'signal');
  assert.equal(result.code, '01TK12CKA110');
  assert.equal(result.signal, '03');
  const panel = page.getByRole('status', { name: ruleText.found, exact: true });
  await panel.getByText(result.code, { exact: true }).waitFor();
  await panel.getByText(result.signal, { exact: true }).waitFor();
  await panel.getByText(ruleText.via.signal, { exact: true }).waitFor();
  await screenshot(page, 'admin-kks-rule');
  await form.getByLabel(ruleText.site, { exact: true }).selectOption(otherSite.id);
  await panel.waitFor({ state: 'hidden' });
  await form.getByRole('button', { name: ruleText.check, exact: true }).click();
  await form.getByRole('alert').waitFor();
}

async function readOnlyRules(
  t: TestContext,
  browser: Browser,
  origin: string,
  page: Page,
  adminRole: Role,
) {
  const source = await (await page.request.get(`/api/v1/roles/${adminRole.id}`)).json();
  const role = await write(page, 'POST', '/api/v1/roles', {
    name: 'Администратор без структуры',
    copy_from: adminRole.id,
  });
  await write(page, 'PUT', `/api/v1/roles/${role.id}/permissions`, {
    permissions: source.permissions.filter(
      (item: { scope: string; target: string }) =>
        item.scope !== 'section' || item.target !== 'structure',
    ),
  });
  const user = await write(page, 'POST', '/api/v1/users', {
    login: `readonly-${randomBytes(3).toString('hex')}`,
    name: 'Администратор доступа',
    role_id: role.id,
  });
  const reader = await signedInUser(t, browser, origin, user);
  const puts: string[] = [];
  reader.on('request', (request) => {
    if (request.method() === 'PUT') puts.push(request.url());
  });
  await reader.getByRole('link', { name: text.admin, exact: true }).click();
  await reader.getByRole('button', { name: copy.kksRules, exact: true }).click();
  await reader.getByText(ruleText.readOnly, { exact: true }).waitFor();
  assert.equal(await reader.getByLabel(ruleText.pattern, { exact: true }).count(), 0);
  assert.equal(await reader.getByRole('button', { name: ruleText.save, exact: true }).count(), 0);
  assert.deepEqual(puts, []);
  const forbidden = await reader.request.put('/api/v1/kks/rules', {
    data: { signal_pattern: '-[0-9]{2}' },
    headers: { 'Idempotency-Key': randomBytes(16).toString('hex') },
  });
  assert.equal(forbidden.status(), 403);
}

test(
  'C-202 — подтверждения и правило ККС на настоящем сервере',
  { timeout: 120_000 },
  async (t) => {
    const { page, browser, origin, config } = await openAccessPage(t);
    const errors: string[] = [];
    const nativeDialogs: string[] = [];
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('dialog', async (dialog) => {
      nativeDialogs.push(dialog.message());
      await dialog.dismiss();
    });
    await page.goto('/admin');
    await login(page, config.login, config.password);
    await page.getByRole('heading', { name: copy.users, level: 2 }).waitFor();
    const roles = (await (await page.request.get('/api/v1/roles')).json()).items as Role[];
    const engineer = roles.find((role) => role.name === 'Инженер')!;
    const admin = roles.find((role) => role.name === 'Администратор')!;
    const user = await write(page, 'POST', '/api/v1/users', {
      login: `operator-${randomBytes(3).toString('hex')}`,
      name: 'Петров П. П.',
      role_id: engineer.id,
    });
    const site = (await (await page.request.get('/api/v1/sites')).json()).items[0];
    const otherSite = await write(page, 'POST', '/api/v1/sites', { name: 'Другая площадка' });
    await write(page, 'POST', `/api/v1/sites/${site.id}/kks`, {
      code: '01TK12CKA110',
      name: 'Датчик температуры',
    });
    await write(page, 'POST', `/api/v1/sites/${site.id}/kks`, {
      code: '01MAJ81AA203',
      name: 'Запорная арматура',
    });
    const network = await write(page, 'POST', '/api/v1/system/allowed-ips', {
      network: '10.0.5.0/24',
    });
    await write(page, 'POST', '/api/v1/system/allowed-ips', { network: '192.0.2.0/24' });
    const userPage = await signedInUser(t, browser, origin, user);
    await page.reload();
    await page
      .getByRole('table', { name: copy.users, exact: true })
      .getByRole('row')
      .filter({ hasText: user.login })
      .waitFor();
    await t.test(
      'отмена всех действий сохраняет данные и аудит, сброс закрывает сессию',
      async () => {
        await confirmationScenarios(page, userPage, user, engineer, network);
      },
    );
    await t.test('ошибка правила, сохранение, разбор -03 и выбор площадки', async () => {
      await rulesScenarios(page, site, otherSite);
    });
    await t.test('чтение без раздела структуры и отказ сервера на изменение', async () => {
      await readOnlyRules(t, browser, origin, page, admin);
    });
    assert.deepEqual(errors, []);
    assert.deepEqual(nativeDialogs, []);
  },
);

import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import type { Page } from 'playwright-core';
import { openAccessPage, confirmAccessAction } from './server.ts';

async function api(page: Page, path: string, method = 'GET', data?: unknown) {
  const response = await page.request.fetch('/api/v1' + path, {
    method,
    data,
    headers: { 'Idempotency-Key': randomUUID() },
  });
  assert.ok(response.ok(), path + ' ' + (await response.text()));
  return response.json();
}
function file(name: string, data: string | Buffer) {
  return { name, mimeType: 'application/octet-stream', buffer: Buffer.from(data) };
}
async function upload(page: Page, title: string, type: string, sample: ReturnType<typeof file>) {
  await page
    .getByRole('navigation')
    .getByRole('link', { name: 'Документы и схемы', exact: true })
    .click();
  await page.getByLabel('Файл документа').setInputFiles(sample);
  await page.getByLabel('Название', { exact: true }).fill(title);
  await page.getByLabel('Тип документа', { exact: true }).selectOption({ label: type });
  await page.getByRole('button', { name: 'Загрузить', exact: true }).click();
  await page.getByRole('heading', { name: title, exact: true }).waitFor();
  await page
    .getByRole('status')
    .filter({ hasText: /^Готово$/ })
    .waitFor();
  return new URL(page.url()).searchParams.get('document')!;
}
async function search(page: Page, term: string) {
  await page.keyboard.press('Control+k');
  const dialog = page.getByRole('dialog');
  const response = page.waitForResponse((r) => {
    const url = new URL(r.url());
    return url.pathname === '/api/v1/search' && url.searchParams.get('q') === term;
  });
  await dialog.getByRole('searchbox').fill(term);
  assert.ok((await response).ok());
  return dialog;
}

test('C-402 · документы на настоящем сервере', { timeout: 150_000 }, async (t) => {
  const { page, origin, browser, config } = await openAccessPage(t);
  page.setDefaultTimeout(10000);
  const jsErrors: string[] = [];
  page.on('pageerror', (error) => jsErrors.push(error.message));
  await api(page, '/auth/login', 'POST', { login: config.login, password: config.password });
  const site = (await api(page, '/sites')).items[0].id;
  await api(page, '/sites/' + site + '/kks', 'POST', { code: '90LAC10AP995' });
  await page.goto('/documents');
  const stamp = await page.evaluate(() => performance.timeOrigin);
  await page.getByLabel('Файл документа').setInputFiles(file('test.bin', '123'));
  await page.getByRole('button', { name: 'Загрузить', exact: true }).click();
  await page
    .getByRole('alert')
    .filter({ hasText: /формат/i })
    .waitFor();
  const manual = await upload(
    page,
    'Инструкция насоса',
    'Документ',
    file('manual.txt', 'Оглавление\fУплотнитель 90LAC10AP995'),
  );
  const dialog = await search(page, 'Уплотнитель');
  await dialog.getByRole('link', { name: /Инструкция насоса.*Стр. 2/ }).waitFor();
  await dialog.getByRole('searchbox').press('Enter');
  await page.getByRole('heading', { name: 'Страница 2' }).waitFor();
  assert.equal(new URL(page.url()).searchParams.get('page'), '2');
  await page.getByRole('button', { name: 'Подтвердить', exact: true }).click();
  await page.getByText('Подтверждено', { exact: true }).waitFor();
  await page.getByRole('link', { name: '90LAC10AP995', exact: true }).click();
  await page
    .getByRole('region', { name: 'Связанные документы' })
    .getByRole('link', { name: 'Инструкция насоса' })
    .click();
  await page.getByLabel('Найти ККС или запись для привязки').fill('СН-0000001');
  await page.getByRole('button', { name: 'Привязать запись', exact: true }).first().click();
  await page.getByRole('link', { name: /^Запись ·/ }).click();
  await page
    .getByRole('region', { name: 'Связанные документы' })
    .getByRole('link', { name: 'Инструкция насоса' })
    .click();
  await page.getByLabel('Новый файл').setInputFiles(file('manual.txt', 'Выбранная копия'));
  const changed = await page.request.put('/api/v1/documents/' + manual + '/file', {
    params: { name: 'manual.txt', version: '1' },
    data: Buffer.from('Другая редакция'),
  });
  assert.equal(changed.status(), 200);
  await page.getByRole('button', { name: 'Заменить файл' }).click();
  await page
    .getByRole('alert')
    .filter({ hasText: /обновите карточку/i })
    .waitFor();
  await page.getByRole('button', { name: 'Обновить карточку' }).click();
  await page.getByText('Другая редакция', { exact: true }).waitFor();
  await page.getByLabel('Новый файл').setInputFiles(file('manual.txt', 'Обновлённая муфта'));
  await page.getByRole('button', { name: 'Заменить файл' }).click();
  await page.getByText('Обновлённая муфта', { exact: true }).waitFor();
  await page.getByText('История версий', { exact: true }).click();
  await page
    .getByRole('listitem')
    .filter({ hasText: /^Версия 3/ })
    .waitFor();
  const old = await search(page, 'Уплотнитель');
  await old.getByText('Ничего не найдено').waitFor();
  assert.equal(await old.getByRole('link').count(), 0);
  await page.keyboard.press('Escape');
  const sample = fileURLToPath(new URL('../../../tests/e2e/document_sample.py', import.meta.url));
  const bytes = execFileSync(process.env.LOCALAI_TEST_PYTHON ?? 'python', [sample]);
  const secret = await upload(page, 'Схема привода', 'Схема', file('drive.docx', bytes));
  await page.getByText('Диаграмма привода', { exact: true }).waitFor();
  assert.equal(
    await page.evaluate(() => performance.timeOrigin),
    stamp,
    'Документы открываются без перезагрузки',
  );
  const types = (await api(page, '/document-types')).items;
  const role = await api(page, '/roles', 'POST', { name: 'Читатель инструкций' });
  await api(page, '/roles/' + role.id + '/permissions', 'PUT', {
    permissions: [
      {
        scope: 'doc_type',
        target: types.find((v: { name: string }) => v.name === 'Документ').id,
        action: 'view',
      },
    ],
  });
  const user = await api(page, '/users', 'POST', {
    login: 'docreader',
    name: 'Читатель',
    role_id: role.id,
  });
  const ctx = await browser.newContext({ baseURL: origin });
  const reader = await ctx.newPage();
  reader.setDefaultTimeout(10000);
  await api(reader, '/auth/login', 'POST', { login: 'docreader', password: user.temporary_password });
  await api(reader, '/auth/password', 'POST', {
    current_password: user.temporary_password,
    new_password: randomUUID(),
  });
  await reader.goto('/documents');
  await reader.getByRole('link', { name: 'Инструкция насоса' }).waitFor();
  assert.equal(await reader.getByRole('link', { name: 'Схема привода' }).count(), 0);
  assert.equal(await reader.getByLabel('Файл документа').count(), 0);
  const hidden = await search(reader, 'Диаграмма');
  await hidden.getByText('Ничего не найдено').waitFor();
  assert.equal((await reader.request.get('/api/v1/documents/' + secret)).status(), 404);
  await reader.keyboard.press('Escape');
  await reader.getByRole('link', { name: 'Инструкция насоса' }).click();
  await reader.getByText('Обновлённая муфта').waitFor();
  await reader.getByText('Нет доступа', { exact: true }).waitFor();
  assert.equal(await reader.getByLabel('Новый файл').count(), 0);
  assert.equal(await reader.getByLabel('Найти ККС или запись для привязки').count(), 0);
  await page.goto('/documents?document=' + manual);
  await page.getByRole('button', { name: 'В архив', exact: true }).click();
  await confirmAccessAction(page, 'В архив');
  await page.getByText(/Версия 3.*В архиве/).waitFor();
  await reader.getByText(/Версия 3.*В архиве/).waitFor();
  await page.getByRole('link', { name: 'Все документы', exact: true }).click();
  await page.getByLabel('Архив', { exact: true }).check();
  await page.getByRole('link', { name: 'Инструкция насоса' }).waitFor();
  assert.deepEqual(jsErrors, []);
  await ctx.close();
});

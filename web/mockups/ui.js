(() => {
  const root = document.documentElement;
  const query = new URLSearchParams(location.search);
  root.dataset.theme = query.get('theme') === 'dark' ? 'dark' : 'light';
  root.dataset.density = query.get('density') === 'roomy' ? 'roomy' : 'dense';

  const paths = {
    brand: 'M5 4v15h14M9 14h4v-4h4V6',
    table: 'M3.5 5h17v14h-17zM3.5 10h17M9.5 10v9m5-9v9',
    nodes: 'M8 7h8m-9 1v8m10-8v8m-9 1h8M9 7a2 2 0 1 0-4 0 2 2 0 0 0 4 0m10 0a2 2 0 1 0-4 0 2 2 0 0 0 4 0M9 17a2 2 0 1 0-4 0 2 2 0 0 0 4 0m10 0a2 2 0 1 0-4 0 2 2 0 0 0 4 0',
    file: 'M6 3h8l4 4v14H6zM14 3v5h4M9 12h6m-6 4h6',
    download: 'M12 3v12m-4-4 4 4 4-4M5 16v4h14v-4',
    tools: 'm4 19 7-7m-4 3 3 3m2-7a5 5 0 0 1 6-6l-3 3 1 2 2 1 3-3a5 5 0 0 1-6 6l-7 7',
    calendar: 'M4 6h16v14H4zM4 10h16M8 4v4m8-4v4m-7 7 2 2 4-4',
    book: 'M3 5h5a4 4 0 0 1 4 4 4 4 0 0 1 4-4h5v14h-5a4 4 0 0 0-4 2 4 4 0 0 0-4-2H3zM12 9v12',
    chat: 'M5 6h14v10h-8l-4 4v-4H5zM9 10h6m-6 3h4',
    settings: 'M4 6h16M4 12h16M4 18h16M8 4v4m8 2v4m-6 2v4',
    search: 'M16 10a6 6 0 1 1-12 0 6 6 0 0 1 12 0m-2 4 5 5',
    moon: 'M20 15.2A8.1 8.1 0 0 1 8.8 4a8.1 8.1 0 1 0 11.2 11.2z',
    sun: 'M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0m-4-9v2m0 14v2m9-9h-2M5 12H3m15.4-6.4L17 7m-10 10-1.4 1.4m12.8 0L17 17M7 7 5.6 5.6',
    tasks: 'M5 5h14v15H5zM8 3v4m8-4v4M8 12h8m-8 4h5',
    edit: 'm5 15-1 5 5-1L20 8l-4-4-11 11zm9-9 4 4',
    filter: 'M3 5h18l-7 8v5l-4 2v-7z',
    left: 'm14 6-6 6 6 6',
    right: 'm10 6 6 6-6 6',
    close: 'm6 6 12 12M18 6 6 18',
    warning: 'M10.5 4.7a1.7 1.7 0 0 1 3 0l7 12.6A1.8 1.8 0 0 1 19 20H5a1.8 1.8 0 0 1-1.5-2.7zM12 9v4m0 3v.1',
    arrow: 'M4 12h15m-5-5 5 5-5 5',
    layout: 'M4 5h16v14H4zM14 5v14',
    diagram: 'M5 5h5v5H5zM14 14h5v5h-5zM10 7.5h6.5V14M7.5 10v6.5H14',
    link: 'm10 14 4-4m-6 6-1 1a3.5 3.5 0 0 1-5-5l4-4a3.5 3.5 0 0 1 5 0m2 8a3.5 3.5 0 0 0 5 0l4-4a3.5 3.5 0 0 0-5-5l-1 1',
  };

  function icon(name) {
    return `<svg class="icon" aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round"><path d="${paths[name]}"></path></svg>`;
  }

  const registry = root.dataset.screen === 'registry';
  const kks = root.dataset.screen === 'kks';
  const links = [
    ['Реестры', 'table', 'registry.html', !kks],
    ['Каталог ККС', 'nodes', 'kks.html', kks],
    ['Документы и схемы', 'file'],
    ['Импорт', 'download'],
    ['Инструменты', 'tools'],
    ['Проверки и табель', 'calendar'],
    ['Знания', 'book'],
    ['Панель ИИ', 'chat'],
    ['Администрирование', 'settings'],
  ];

  function renderChrome() {
    document.querySelector('[data-rail]').innerHTML = `
      <a class="brand" data-page-link href="registry.html">
        <span class="brand-mark">${icon('brand')}</span><span>LOCALAI</span>
      </a>
      <nav class="rail-nav" aria-label="Разделы приложения">
        ${links.map(([label, glyph, href, active]) => href
          ? `<a href="${href}" data-page-link ${active ? 'aria-current="page"' : ''}>${icon(glyph)}<span>${label}</span></a>`
          : `<span class="rail-item">${icon(glyph)}<span>${label}</span></span>`).join('')}
      </nav>
      <div class="rail-context"><span class="station-dot" aria-hidden="true"></span>
        <div><strong>ТЭЦ-1</strong><small>Демонстрационные данные</small></div>
      </div>`;
    document.querySelector('[data-topbar]').innerHTML = `
      <div class="breadcrumbs">
        <a href="${kks ? 'kks.html' : 'registry.html'}" data-page-link>${kks ? 'Каталог ККС' : 'Реестры'}</a>
        ${icon('right')}<span>${kks ? 'Объект' : 'Оборудование'}</span>
      </div>
      <div class="top-actions">
        <button type="button" class="command" data-command aria-label="Перейти к поиску">
          ${icon('search')}<span>Поиск и команды</span><kbd>Ctrl K</kbd>
        </button>
        <button type="button" class="icon-button" data-theme-button></button>
        <span class="top-task">${icon('tasks')}<span>Задачи</span></span>
        <span class="profile"><span class="avatar" aria-hidden="true">АК</span><span>А. Каримов</span></span>
      </div>`;
  }

  function themedLink(href) {
    const target = new URL(href, location.href);
    target.searchParams.set('theme', root.dataset.theme);
    target.searchParams.set('density', root.dataset.density);
    return target.toString();
  }

  function updateControls() {
    for (const link of document.querySelectorAll('[data-page-link]')) {
      link.href = themedLink(link.getAttribute('href'));
    }
    const themeButton = document.querySelector('[data-theme-button]');
    const toDark = root.dataset.theme === 'light';
    themeButton.innerHTML = icon(toDark ? 'moon' : 'sun');
    themeButton.title = toDark ? 'Тёмная тема' : 'Светлая тема';
    themeButton.setAttribute('aria-label', themeButton.title);
    for (const item of document.querySelectorAll('[data-set-density]')) {
      item.setAttribute('aria-pressed', String(root.dataset.density === item.dataset.setDensity));
    }
  }

  function command() {
    if (registry) document.getElementById('registry-search').focus();
    else location.href = themedLink('registry.html#registry-search');
  }

  function setPreference(name, value) {
    root.dataset[name] = value;
    try {
      history.replaceState(null, '', themedLink(location.href));
    } catch {
      // На file:// сохранение параметров при переходе обеспечивают ссылки.
    }
    updateControls();
  }

  window.mockup = Object.freeze({ icon, themedLink });

  document.addEventListener('DOMContentLoaded', () => {
    renderChrome();
    for (const placeholder of document.querySelectorAll('[data-icon]')) {
      placeholder.innerHTML = icon(placeholder.dataset.icon);
    }
    updateControls();
    document.querySelector('[data-theme-button]').addEventListener('click', () => {
      setPreference('theme', root.dataset.theme === 'light' ? 'dark' : 'light');
    });
    document.querySelector('[data-command]').addEventListener('click', command);
    for (const item of document.querySelectorAll('[data-set-density]')) {
      item.addEventListener('click', () => setPreference('density', item.dataset.setDensity));
    }
    document.addEventListener('keydown', (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        command();
      }
    });
    if (location.hash === '#registry-search') document.getElementById('registry-search')?.focus();
  });
})();

{
  const compact = document.documentElement.dataset.screen === 'inspector';
  const { icon, themedLink } = window.mockup;
  const makers = ['Siemens', 'ABB', 'Электросила', 'Метран'];
  const equipment = [
    ['Клапан запорный на входе ПЭН-1', '10LAC10AA', 'ABB'],
    ['Датчик давления на нагнетании ПЭН-1', '10LAC10CP', 'Метран'],
    ['Насос питательный', '10LAC10AP', 'Siemens'],
    ['Электродвигатель ПЭН-1', '10LAC10AM', 'Электросила'],
    ['Задвижка рециркуляции ПЭН-1', '10LAC11AA', 'ABB'],
    ['Расходомер питательной воды', '10LAC10CF', 'Siemens'],
    ['Датчик температуры подшипника', '10LAC10CT', 'Метран'],
    ['Насос конденсатный', '10LCA10AP', 'Электросила'],
    ['Клапан обратный после эжектора', '10LCA10AA', 'ABB'],
    ['Шкаф управления приводами', '10LAC20GH', 'Siemens'],
    ['Датчик уровня деаэратора', '10LAA10CL', 'Метран'],
    ['Электродвигатель резервного насоса', '10LAC20AM', 'Электросила'],
  ];
  const states = [
    { label: 'В работе', tone: 'good' },
    { label: 'В резерве', tone: 'neutral' },
    { label: 'На проверке', tone: 'warning' },
  ];
  const selected = {
    kks: '10LAC10AP001',
    name: 'Насос питательный ПЭН-1',
    inventory: 'ЭК-0127',
    maker: 'Siemens',
    quantity: 3,
    state: states[0],
    date: '02.12.2026',
  };
  const records = Array.from({ length: 10_000 }, (_, index) => {
    if (index === 2) return selected;
    const [name, prefix, maker] = equipment[index % equipment.length];
    const serial = String(Math.floor(index / equipment.length) + 11).padStart(3, '0');
    const month = index % 3 ? '12' : '11';
    const day = month === '12' ? 1 + index % 26 : 18 + index % 12;
    return {
      name,
      kks: prefix + serial,
      inventory: `ЭК-${String(index + 200).padStart(4, '0')}`,
      maker,
      quantity: 1 + index % 6,
      state: states[index % 7 === 4 ? 1 : index % 9 === 5 ? 2 : 0],
      date: `${String(day).padStart(2, '0')}.${month}.2026`,
    };
  });
  const number = new Intl.NumberFormat('ru-RU');
  const rows = document.querySelector('[data-rows]');
  const scroller = rows.closest('.scroll-table');
  const summary = document.querySelector('[data-page-summary]');
  const position = document.querySelector('[data-page-number]');
  const steps = [...document.querySelectorAll('[data-page-step]')];
  const search = document.getElementById('registry-search');
  const conflict = document.querySelector('[data-conflict]');
  const comparison = document.querySelector('[data-draft]');
  const pageSize = 50;
  let page = 0;
  let filterMakers = [];
  let editing = !compact;
  let draft = '4';

  function quantity(row) {
    if (row !== selected || compact) return number.format(row.quantity);
    if (editing) {
      return `<input class="quantity-input" type="number" min="0" step="1" required
        aria-label="Количество для 10LAC10AP001" data-quantity value="${draft}" />`;
    }
    return `<button class="cell-button" type="button" aria-label="Править количество для 10LAC10AP001"
      data-edit-start>${number.format(row.quantity)} ${icon('edit')}</button>`;
  }

  function rowHtml(row) {
    const active = row === selected;
    const code = active
      ? `<a class="code row-link" href="${themedLink('kks.html')}" data-page-link>${row.kks}</a>`
      : `<span class="code">${row.kks}</span>`;
    return `<tr${active ? ' class="selected" data-selected' : ''}>
      <td>${code}</td><td class="row-name">${row.name}</td>
      ${compact ? '' : `<td class="tabular muted">${row.inventory}</td><td>${row.maker}</td>`}
      <td class="align-end tabular">${quantity(row)}</td>
      <td><span class="badge" data-tone="${row.state.tone}"><span class="badge-dot"></span>${row.state.label}</span></td>
      ${compact ? '' : `<td class="tabular">${row.date}</td>`}
    </tr>`;
  }

  function updateTable(resetScroll = true) {
    const terms = (search?.value ?? '').toLocaleLowerCase('ru').trim().split(/\s+/).filter(Boolean);
    const filtered = records.filter((row) => {
      if (filterMakers.length && !filterMakers.includes(row.maker)) return false;
      const searchable = [row.kks, row.name, row.inventory, row.maker].join(' ').toLocaleLowerCase('ru');
      return terms.every((term) => searchable.includes(term));
    });
    const start = page * pageSize;
    const items = filtered.slice(start, start + pageSize);
    rows.innerHTML = items.length
      ? items.map(rowHtml).join('')
      : `<tr><td class="empty-row" colspan="${compact ? 4 : 7}">Нет записей с такими условиями.</td></tr>`;
    summary.textContent = items.length
      ? `${number.format(start + 1)}–${number.format(start + items.length)} из ${number.format(filtered.length)}`
      : '0 из 0';
    const pages = Math.max(1, Math.ceil(filtered.length / pageSize));
    position.textContent = `${number.format(page + 1)} / ${number.format(pages)}`;
    steps[0].disabled = page === 0;
    steps[1].disabled = page + 1 >= pages;
    if (resetScroll) scroller.scrollTop = 0;
  }

  for (const step of steps) {
    step.addEventListener('click', () => {
      page += Number(step.dataset.pageStep);
      updateTable();
    });
  }
  updateTable();
  if (!compact) bindRegistryControls();

  function bindRegistryControls() {
    const filter = document.getElementById('maker-filter');
    const filterButton = document.querySelector('[data-filter-button]');
    const makerList = filter.querySelector('[data-makers]');
    makerList.innerHTML = makers.map((maker) =>
      `<label><input type="checkbox" name="maker" value="${maker}" />${maker}</label>`,
    ).join('');

    function applyMakers(chosen) {
      filterMakers = chosen;
      page = 0;
      filterButton.dataset.active = String(chosen.length > 0);
      updateTable();
      filter.hidePopover();
      filterButton.focus();
    }

    function openFilter() {
      if (filter.matches(':popover-open')) {
        filter.hidePopover();
        return;
      }
      for (const checkbox of filter.elements.namedItem('maker')) checkbox.checked = filterMakers.includes(checkbox.value);
      filter.showPopover();
      const anchor = filterButton.getBoundingClientRect();
      filter.style.insetInlineStart = `${Math.max(8, Math.min(anchor.right - filter.offsetWidth, innerWidth - filter.offsetWidth - 8))}px`;
      filter.style.insetBlockStart = `${Math.max(8, Math.min(anchor.bottom + 8, innerHeight - filter.offsetHeight - 8))}px`;
    }

    filterButton.addEventListener('click', openFilter);
    filter.addEventListener('toggle', (event) => {
      filterButton.setAttribute('aria-expanded', String(event.newState === 'open'));
    });
    filter.addEventListener('submit', (event) => {
      event.preventDefault();
      applyMakers(new FormData(filter).getAll('maker'));
    });
    filter.querySelector('[data-filter-reset]').addEventListener('click', () => applyMakers([]));
    search.addEventListener('input', () => {
      page = 0;
      updateTable();
    });

    function finishEdit(useDraft) {
      if (useDraft && (draft === '' || !Number.isSafeInteger(Number(draft)) || Number(draft) < 0)) {
        rows.querySelector('[data-quantity]')?.reportValidity();
        return;
      }
      if (useDraft) selected.quantity = Number(draft);
      editing = false;
      conflict.hidden = true;
      updateTable(false);
      rows.querySelector('[data-edit-start]')?.focus();
    }

    rows.addEventListener('input', (event) => {
      if (!event.target.matches('[data-quantity]')) return;
      draft = event.target.value;
      comparison.textContent = draft || '—';
    });
    rows.addEventListener('click', (event) => {
      if (!event.target.closest('[data-edit-start]')) return;
      editing = true;
      draft = String(selected.quantity);
      comparison.textContent = draft;
      updateTable(false);
      rows.querySelector('[data-quantity]').select();
    });
    rows.addEventListener('keydown', (event) => {
      if (!event.target.matches('[data-quantity]')) return;
      if (event.key === 'Escape') finishEdit(false);
      if (event.key === 'Enter') {
        event.preventDefault();
        if (!event.target.reportValidity()) return;
        if (conflict.hidden) finishEdit(true);
        else conflict.querySelector('[data-resolve="draft"]').focus();
      }
    });
    for (const action of conflict.querySelectorAll('[data-resolve]')) {
      action.addEventListener('click', () => finishEdit(action.dataset.resolve === 'draft'));
    }
  }
}

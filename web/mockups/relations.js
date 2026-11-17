(() => {
  const registry = document.querySelector('[data-registry-select]');
  const role = document.querySelector('[data-role-select]');
  const groups = [...document.querySelectorAll('tbody[data-registry]')];
  const summary = document.querySelector('[data-linked-count]');
  const empty = document.querySelector('[data-no-links]');

  function filter() {
    let count = 0;
    for (const group of groups) {
      const sameRegistry = !registry.value || group.dataset.registry === registry.value;
      let visible = 0;
      for (const row of group.querySelectorAll('[data-role]')) {
        row.hidden = !(sameRegistry && (!role.value || row.dataset.role === role.value));
        if (!row.hidden) visible += 1;
      }
      group.hidden = visible === 0;
      group.querySelector('[data-group-count]').textContent = visible;
      count += visible;
    }
    empty.hidden = count > 0;
    summary.textContent = `Действующих записей — ${count}`;
  }

  registry.addEventListener('change', filter);
  role.addEventListener('change', filter);
})();

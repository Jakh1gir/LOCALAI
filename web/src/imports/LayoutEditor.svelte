<script lang="ts">
  import type { Layout } from './model.ts';
  let { headers, layout = $bindable() }: { headers: string[]; layout: Layout } = $props();
  const simple = {
    kks: 'ККС объекта',
    signal: 'Код сигнала',
    cable: 'Номер кабеля',
    channel: 'Канал',
    service: 'Служебный',
  };
  const places = {
    route_start: 'Место · начало',
    route_end: 'Место · конец',
    route_via: 'Место · через',
    related: 'Место · связано',
  };
  function role(h: string) {
    if (layout.kks === h) return 'kks';
    if (layout.signal === h) return 'signal';
    if (layout.cable === h) return 'cable';
    if (Object.hasOwn(layout.places, h)) return layout.places[h];
    const contact = Object.entries(layout.contacts).find(([, v]) => v.includes(h));
    if (contact) return 'contact:' + contact[0];
    if (layout.channel.includes(h)) return 'channel';
    if (layout.service.includes(h)) return 'service';
    return '';
  }
  function change(h: string, next: string) {
    const l: Layout = JSON.parse(JSON.stringify(layout));
    for (const key of ['kks', 'signal', 'cable'] as const) if (l[key] === h) l[key] = null;
    delete l.places[h];
    if (!Object.hasOwn(places, next)) delete l.contacts[h];
    for (const place of Object.keys(l.contacts))
      l.contacts[place] = l.contacts[place].filter((v) => v !== h);
    l.channel = l.channel.filter((v) => v !== h);
    l.service = l.service.filter((v) => v !== h);
    if (next === 'kks' || next === 'signal' || next === 'cable') l[next] = h;
    else if (next === 'channel' || next === 'service') l[next].push(h);
    else if (next.startsWith('contact:')) (l.contacts[next.slice(8)] ??= []).push(h);
    else if (Object.hasOwn(places, next)) l.places[h] = next;
    layout = l;
  }
</script>

<table class="access-table">
  <thead>
    <tr>
      <th>Столбец Excel</th>
      <th>Роль в раскладке</th>
    </tr>
  </thead>
  <tbody>
    {#each headers as h}<tr>
        <th>{h}</th>
        <td>
          <select
            aria-label={'Роль · ' + h}
            value={role(h)}
            onchange={(e) => change(h, e.currentTarget.value)}
          >
            <option value="">Обычное значение</option>
            {#each Object.entries({ ...simple, ...places }) as [k, v]}<option value={k}>
                {v}
              </option>{/each}
            {#each Object.keys(layout.places).filter((p) => p !== h) as p}<option
                value={'contact:' + p}
              >
                Клемма · {p}
              </option>{/each}
          </select>
        </td>
      </tr>{/each}
  </tbody>
</table>

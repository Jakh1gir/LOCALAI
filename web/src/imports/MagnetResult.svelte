<script lang="ts">
  import { shown, type Issue } from './model.ts';
  let { plan, issues = [] }: { plan?: Record<string, unknown>; issues?: Issue[] } = $props();
  const labels: Record<string, string> = {
    rows: 'Строк',
    signals: 'Сигналов',
    spares: 'Резервных каналов',
    devices: 'Аппаратов',
    cables: 'Кабелей',
    device_from_kks_column: 'Аппарат из столбца ККС',
    device_from_signal_code: 'Аппарат из кода сигнала',
    devices_not_kks_like: 'Нестандартные коды',
    cables_with_several_devices: 'Кабелей с несколькими аппаратами',
    skipped_empty_rows: 'Пустых строк пропущено',
    issues: 'Расхождения по кодам',
  };
</script>

{#if plan}<h3>Раскладка ККС</h3>
  <dl class="import-metrics">
    {#each Object.entries(plan) as [k, v]}<div>
        <dt>{labels[k] ?? k}</dt>
        <dd>{shown(v) || '—'}</dd>
      </div>{/each}
  </dl>{/if}
{#if issues.length}<h3>Расхождения и ошибки</h3>
  <ul>
    {#each issues as i}<li>Строка {i.row} · {i.message} ({i.code ?? i.reason})</li>{/each}
  </ul>{/if}

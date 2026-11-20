import type { Dataset, Permissions, TypeField } from '../api/types.ts';

export const actions = {
  create: 'Новые',
  update: 'Изменённые',
  unchanged: 'Без изменений',
  conflict: 'Спорные',
  error: 'Ошибки',
} as const;
export const kinds = {
  text: 'Текст',
  integer: 'Целое число',
  number: 'Число',
  date: 'Дата',
  boolean: 'Да / нет',
  kks: 'ККС',
} as const;
export type Action = keyof typeof actions;
export type Mode = 'records' | 'new_type' | 'magnet';
export type Sheet = {
  name: string;
  rows: number;
  columns: number;
  header_row: number;
  top: unknown[][];
};
export type Layout = {
  kks: string | null;
  signal: string | null;
  cable: string | null;
  places: Record<string, string>;
  contacts: Record<string, string[]>;
  channel: string[];
  service: string[];
};
export type FieldSpec = { column: string; name: string; kind: keyof typeof kinds; unique: boolean };
export type Config = {
  mode: Mode;
  dataset_id?: string;
  key?: string;
  mapping?: Record<string, string>;
  new_type?: { type_name: string; dataset_name: string; site_id: string; fields: FieldSpec[] };
  site_id?: string;
  name?: string;
  layout?: Layout;
};
export type Plan = Config & { sheet: string; header_row: number };
export type Profile = {
  id: string;
  name: string;
  settings: Config;
  created_by: string;
  updated_at: string;
};
export type Preview = {
  headers: string[];
  rows: number;
  sample: { row: number; cells: Record<string, unknown> }[];
  suggested: { mode: Mode; mapping: Record<string, string>; layout: Layout };
  profiles: Profile[];
};
export type Issue = { row: number; message: string; reason?: string; code?: string };
export type Summary = {
  counts: Record<Action, number>;
  rows: number;
  mode: Mode;
  datasets?: string[];
  plan?: Record<string, unknown>;
  issues?: Issue[];
  write_errors?: Issue[];
};
export type Batch = {
  id: string;
  sheets: Sheet[];
  file: { name: string };
  summary: Summary | null;
};
export type Row = {
  row: number;
  action: Action;
  cells: Record<string, unknown>;
  changes: Record<string, [unknown, unknown]> | null;
  message: string | null;
  reason: string | null;
  decision: 'skip' | 'apply' | null;
};
export type Report = {
  counts: Record<Action, number>;
  skipped?: Issue[];
  dataset_id?: string;
  datasets?: string[];
  result?: { issues?: Issue[]; plan?: Record<string, unknown> };
  profile?: { name: string };
};

export function allowed(p: Permissions) {
  return (
    p.sections.includes('structure') || Object.values(p.datasets).some((a) => a.includes('import'))
  );
}
export function canImport(p: Permissions, id: string) {
  return !!(p.datasets['*']?.includes('import') || p.datasets[id]?.includes('import'));
}
export function targets(items: Dataset[], p: Permissions) {
  return items.filter((d) => !d.archived_at && canImport(p, d.id));
}
export function writable(fields: TypeField[], p: Permissions) {
  return fields.filter((f) => !f.archived_at && !p.fields[f.id]);
}
export function eligibleKey(f: TypeField) {
  return f.unique_values && !(f.kind === 'kks' && f.kks_role === 'object');
}
export function shown(value: unknown): string {
  if (value === undefined || value === null || value === '') return '—';
  if (typeof value === 'boolean') return value ? 'Да' : 'Нет';
  if (Array.isArray(value)) return value.map(shown).join(', ');
  if (typeof value === 'object')
    return Object.entries(value)
      .map(([k, v]) => k + ': ' + shown(v))
      .join(', ');
  return String(value);
}
export const registryPath = (id: string) => '/registries?' + new URLSearchParams({ dataset: id });
export const wizardPath = (id: string) => '/imports?' + new URLSearchParams({ dataset: id });

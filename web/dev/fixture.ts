import type {
  Dataset, Health, NamedEntity, RecordRow, Refs, TypeDetails, TypeField,
} from '../src/api/types.ts';

const when = '2026-01-01T00:00:00.000Z';
const id = (number: number) => `00000000-0000-4000-8000-${number.toString(16).padStart(12, '0')}`;
const base = (number: number, name: string) => ({
  id: id(number), name, archived_at: null, created_at: when, updated_at: when,
});

export const mockHealth: Health = {
  status: 'ok', version: '0.1.0', schema_version: 1, database_id: id(4),
};
export const site: NamedEntity = base(1, 'ТЭЦ-1');
export const dataset: Dataset = { ...base(3, 'В эксплуатации'), site_id: site.id, type_id: id(2) };
const fieldSpecs = [
  ['name', 'Наименование', 'text'],
  ['manufacturer', 'Изготовитель', 'choice'],
  ['quantity', 'Количество', 'integer'],
  ['next_inspection', 'Следующая проверка', 'date'],
  ['kks', 'Код ККС', 'kks'],
] as const;
const fields: TypeField[] = fieldSpecs.map(([key, name, kind], position) => ({
  id: id(10 + position), type_id: dataset.type_id, key, name, kind, position,
  multiple: false, required: key === 'name',
  searchable: true, sortable: true, filterable: true, archived_at: null,
  dictionary_id: kind === 'choice' ? id(5) : null,
  input_mode: kind === 'choice' ? 'strict' : null,
  kks_role: kind === 'kks' ? 'object' : null,
}));
export const registryType: TypeDetails = {
  ...base(2, 'Оборудование'), description: null, schema_rev: 1, fields,
};

export const choiceLabels: Record<string, string> = {
  [id(20)]: 'Siemens',
  [id(21)]: 'ABB',
  [id(22)]: 'Электросила',
};
export const kksLabels: Record<string, string> = Object.fromEntries(
  Array.from({ length: 1000 }, (_, index) => [id(20000 + index), `10LAC10AP${String(index + 1).padStart(4, '0')}`]),
);

export function makeRows(): RecordRow[] {
  const names = ['Насос питательный', 'Клапан регулирующий', 'Датчик давления'];
  return Array.from({ length: 10_000 }, (_, index) => ({
    id: id(1000 + index),
    dataset_id: dataset.id,
    version: 1,
    doc: {
      name: `${names[index % names.length]} № ${index + 1}`,
      manufacturer: id(20 + index % 3),
      quantity: 1 + index % 30,
      next_inspection: `2027-06-${String(1 + index % 28).padStart(2, '0')}`,
      kks: id(20000 + index % 1000),
    },
    archived_at: null,
    created_at: when,
    updated_at: when,
  }));
}

export function refsFor(rows: RecordRow[]): Refs {
  const refs: Refs = { choices: {}, kks: {} };
  for (const row of rows) {
    const { manufacturer, kks } = row.doc;
    if (typeof manufacturer === 'string' && choiceLabels[manufacturer]) {
      refs.choices[manufacturer] = choiceLabels[manufacturer];
    }
    if (typeof kks === 'string' && kksLabels[kks]) refs.kks[kks] = kksLabels[kks];
  }
  return refs;
}

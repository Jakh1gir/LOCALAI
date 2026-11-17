import type { components } from './contract.d.ts';

type Schemas = components['schemas'];

export type Items<T> = { items: T[] };
export type Health = {
  status: 'ok';
  version: string;
  schema_version: number;
  database_id: string;
};
export type NamedEntity = {
  id: string;
  name: string;
  archived_at: string | null;
  created_at: string;
  updated_at: string;
};
export type Dataset = NamedEntity & { site_id: string; type_id: string };
export type RecordType = NamedEntity & { description: string | null; schema_rev: number };
export type TypeField = {
  id: string;
  type_id: string;
  key: string;
  name: string;
  kind: Schemas['FieldCreate']['kind'];
  position: number;
  multiple: boolean;
  required: boolean;
  searchable: boolean;
  sortable: boolean;
  filterable: boolean;
  dictionary_id: string | null;
  input_mode: Exclude<Schemas['FieldCreate']['input_mode'], undefined>;
  kks_role: Exclude<Schemas['FieldCreate']['kks_role'], undefined>;
  archived_at: string | null;
};
export type TypeDetails = RecordType & { fields: TypeField[] };

export type Scalar = string | number | boolean | null;
export type RecordDoc = Record<string, Scalar | Scalar[]>;
export type RecordValues = Schemas['RecordPatch']['values'];
export type RecordUpdate = Schemas['RecordPatch'];
export type RecordCreate = Schemas['RecordCreate'];
export type RecordRow = {
  id: string;
  dataset_id: string;
  version: number;
  doc: RecordDoc;
  archived_at: string | null;
  created_at: string;
  updated_at: string;
};
export type Refs = { choices: Record<string, string>; kks: Record<string, string> };
export type RecordDetail = { record: RecordRow; refs: Refs };
export type RecordPage = Items<RecordRow> & { next_cursor: string | null; refs: Refs };

type Comparison = 'eq' | 'ne' | 'lt' | 'lte' | 'gt' | 'gte' | 'contains' | 'starts';
type FilterValue = Exclude<Scalar, null> | { id: string };
export type Condition =
  | { field: string; op: Comparison; value: FilterValue }
  | { field: string; op: 'in'; value: FilterValue[] }
  | { field: string; op: 'empty' | 'not_empty' };
export type Sort = { field: string; dir: 'asc' | 'desc' };
export type CountQuery = Omit<Schemas['RecordCount'], 'where'> & {
  where?: (Condition | { any: Condition[] })[] | null;
};
export type RecordQuery = CountQuery & Pick<Schemas['RecordQuery'], 'limit' | 'cursor'> & {
  sort?: Sort | null;
};

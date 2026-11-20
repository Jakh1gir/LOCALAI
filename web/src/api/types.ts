import type { components } from './contract.d.ts';

type Schemas = components['schemas'];

export type Items<T> = { items: T[] };
export type Health = {
  status: 'ok';
  version: string;
  schema_version: number;
  database_id: string;
  /** В базе ещё нет пользователей: администратора создаёт установщик. */
  setup_required?: boolean;
};
export type Permissions = {
  sections: string[];
  datasets: Record<string, string[]>;
  fields: Record<string, 'hidden' | 'readonly'>;
};
/** Ответ входа, смены пароля и GET /auth/me. */
export type Me = {
  user: { id: string; login: string; name: string };
  role: { id: string; name: string };
  must_change_password: boolean;
  permissions: Permissions;
  session_expires_at?: string;
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

export type Permission = Schemas['Permission'];
export type PermissionCatalog = {
  sections: Record<string, string>;
  dataset_actions: Record<string, string>;
  field_limits: Record<string, string>;
  doc_type_actions: Record<string, string>;
};
export type Role = NamedEntity & { description: string | null; users: number };
export type RoleDetails = Role & { permissions: Permission[] };
export type User = {
  id: string;
  login: string;
  name: string;
  role_id: string;
  role_name: string;
  must_change_password: boolean;
  blocked_at: string | null;
  locked_until: string | null;
  last_login_at: string | null;
  password_changed_at: string | null;
  created_at: string;
  updated_at: string;
};
export type TemporaryUser = User & { temporary_password?: string };
export type AllowedNetwork = { id: string; network: string; note: string | null; created_at: string };
export type UserCreate = Schemas['UserCreate'];
export type UserUpdate = Schemas['UserPatch'];
export type RoleCreate = Schemas['RoleCreate'];
export type RoleUpdate = Schemas['RolePatch'];
export type NetworkCreate = Schemas['NetworkCreate'];
export type Backup = {
  name: string;
  size: number;
  created_at: string;
  label: string | null;
  schema_version: number;
  database_id: string | null;
  app_version: string | null;
};
export type BackupEntry = Backup | { name: string; size: number; error: string };
export type BackupRestoreResult = {
  restored: Backup;
  protective_backup: Backup;
  schema_version: number;
};
export type KksRules = {
  signal_pattern: string;
  updated_at: string;
  example: { code: string; object: string; signal: string | null };
};
export type KksResolution = {
  id: string;
  site_id: string;
  code: string;
  name: string | null;
  via: 'code' | 'alias' | 'signal';
  alias?: string;
  signal?: string;
  signal_code?: string;
};

export type Scalar = string | number | boolean | null;
export type KksCard = {
  id: string; code: string; name: string | null; site_id: string; archived_at: string | null;
  aliases: { id: string; alias: string }[];
  usage: { dataset_id: string; role: string; records: number }[];
  via_links: { dataset_id: string; records: number }[];
};
export type SearchKks = {
  id: string; code: string; name: string | null; site_id: string; site: string;
  archived: boolean; exact: boolean; via?: 'code' | 'alias' | 'signal' | 'glossary'; signal?: string;
};
export type SearchRecord = {
  id: string; dataset_id: string; dataset: string; label: string; version: number;
  match: { field: string; text: string } | null;
};
export type SearchResult = {
  query: string; kks: SearchKks[]; records: SearchRecord[]; more: { records: boolean }; took_ms: number;
};
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
export type Refs = { choices: Record<string, string>; kks: Record<string, string>; records?: Record<string, string> };
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

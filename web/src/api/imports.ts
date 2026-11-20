import type { ApiPath, Options } from './client.ts';
import type { Batch, Plan, Preview, Profile, Report, Row, Action } from '../imports/model.ts';
import type { Items } from './types.ts';

export function importMethods(request: <T>(path: ApiPath, options?: Options) => Promise<T>) {
  return {
    upload: (file: File, signal: AbortSignal) =>
      request<Batch>('/api/v1/imports', {
        method: 'POST',
        query: { name: file.name },
        file,
        signal,
      }),
    preview: (id: string, sheet: string, header: number, dataset: string, signal: AbortSignal) =>
      request<Preview>('/api/v1/imports/{id}/preview', {
        id,
        query: { sheet, header_row: String(header), dataset_id: dataset || undefined },
        signal,
      }),
    plan: (id: string, body: Plan, signal: AbortSignal) =>
      request<Batch>('/api/v1/imports/{id}/plan', { method: 'POST', id, body, signal }),
    rows: (id: string, action: Action | '', after: number, signal: AbortSignal) =>
      request<Items<Row> & { more: boolean; last: number | null }>('/api/v1/imports/{id}/rows', {
        id,
        query: { action: action || undefined, after: String(after) },
        signal,
      }),
    decide: (id: string, row: number, decision: 'skip' | 'apply', signal: AbortSignal) =>
      request<Row>('/api/v1/imports/{id}/rows/{row}/decision', {
        method: 'PUT',
        id,
        row,
        body: { decision },
        signal,
      }),
    apply: (id: string, name: string, signal: AbortSignal) =>
      request<Report>('/api/v1/imports/{id}/apply', {
        method: 'POST',
        id,
        body: name.trim() ? { save_profile: name.trim() } : {},
        signal,
      }),
    profiles: (signal: AbortSignal) =>
      request<Items<Profile>>('/api/v1/import-profiles', { signal }),
    removeProfile: (id: string, signal: AbortSignal) =>
      request<void>('/api/v1/import-profiles/{id}', { method: 'DELETE', id, signal }),
  };
}

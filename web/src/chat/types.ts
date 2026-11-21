export type Change = {
  seq: number;
  at: string;
  kind: string;
  action: string;
  id: string | null;
  dataset_id: string | null;
  actor: string;
};
export type EventPage = { cursor: number; more: boolean; events: Change[] };
export type ChatRef = {
  kind: 'kks' | 'record' | 'document';
  id: string;
  start: number;
  end: number;
  access: boolean;
  dataset_id?: string;
  dataset?: string;
};
export type Message = {
  id: string;
  seq: number;
  author: { id: string; name: string };
  created_at: string;
  edited_at: string | null;
  version: number;
  state: 'active' | 'deleted' | 'hidden';
  text: string | null;
  refs: ChatRef[];
  can: { edit: boolean; delete: boolean; hide: boolean; unhide: boolean; history: boolean };
};
export type ReadState = { last_read: number; unread: number };
export type ChatPage = ReadState & {
  items: Message[];
  first: number | null;
  last: number | null;
  more: boolean;
};
export type Revision = { version: number; text: string; at: string };

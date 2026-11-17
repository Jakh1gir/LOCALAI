import { text } from './text.ts';

export const sections = [
  { path: '/registries', title: text.registry, icon: 'registry' },
  { path: '/kks', title: text.kks, icon: 'kks' },
  { path: '/documents', title: text.documents, icon: 'documents' },
  { path: '/imports', title: text.imports, icon: 'imports' },
  { path: '/tools', title: text.tools, icon: 'tools' },
  { path: '/checks', title: text.checks, icon: 'checks' },
  { path: '/knowledge', title: text.knowledge, icon: 'knowledge' },
  { path: '/assistant', title: text.assistant, icon: 'assistant' },
  { path: '/admin', title: text.admin, icon: 'admin' },
] as const;

export const homePath = sections[0].path;

export function findSection(pathname: string) {
  const path = pathname.replace(/\/+$/, '') || homePath;
  return sections.find((section) => section.path === path);
}

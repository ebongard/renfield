/**
 * KnowledgePage offers delete only where the backend allows it (#1462):
 *  - a document's delete button needs `can_delete` on its KB (owner, kb.all,
 *    or a share with the owner's switch) — a level alone does not give it;
 *  - the KB's delete button needs `can_delete_kb` (owner or kb.all only;
 *    the switch covers documents, never the KB itself);
 *  - the share dialog is reachable for the owner and admin grantees.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { http, HttpResponse } from 'msw';
import { screen, fireEvent, within } from '@testing-library/react';
import { server } from '../mocks/server';
import { renderWithProviders } from '../test-utils';
import KnowledgePage from '../../../../src/frontend/src/pages/KnowledgePage';
import type { KnowledgeBaseRow } from '../../../../src/frontend/src/api/resources/knowledge';
import { TEST_CONFIG } from '../config';

const BASE = TEST_CONFIG.API_BASE_URL;

const BASES: KnowledgeBaseRow[] = [
  // Own KB: everything.
  { id: 1, name: 'Eigene', document_count: 1, owner_id: 2, permission: 'owner', can_delete: true, can_delete_kb: true },
  // Write share without the switch: no delete at all, no share management.
  { id: 2, name: 'Geteilt', document_count: 1, owner_id: 9, permission: 'write', can_delete: false, can_delete_kb: false },
  // Read share WITH the switch: documents yes, the KB no.
  { id: 3, name: 'Schalter', document_count: 1, owner_id: 9, permission: 'read', can_delete: true, can_delete_kb: false },
  // Admin share without the switch: manages shares, deletes nothing.
  { id: 4, name: 'Verwaltet', document_count: 1, owner_id: 9, permission: 'admin', can_delete: false, can_delete_kb: false },
];

function doc(id: number, kbId: number | null, title: string) {
  return {
    id, filename: `${title}.pdf`, title, display_name: title, status: 'completed',
    file_type: 'pdf', chunk_count: 1, page_count: 1, knowledge_base_id: kbId,
    created_at: '2026-11-01T00:00:00',
  };
}

const DOCS = [
  doc(11, 1, 'EigenesDok'),
  doc(12, 2, 'GeteiltesDok'),
  doc(13, 3, 'SchalterDok'),
  doc(14, 4, 'VerwaltetDok'),
  doc(15, null, 'LosesDok'),
];

function wire() {
  server.use(
    http.get(`${BASE}/api/knowledge/documents`, () => HttpResponse.json(DOCS)),
    http.get(`${BASE}/api/knowledge/bases`, () => HttpResponse.json(BASES)),
    http.get(`${BASE}/api/knowledge/stats`, () => HttpResponse.json({
      document_count: 5, completed_documents: 5, chunk_count: 5, knowledge_base_count: 4,
    })),
    http.get(`${BASE}/api/config/features`, () => HttpResponse.json({})),
    http.get(`${BASE}/api/knowledge/bases/4/permissions`, () => HttpResponse.json([])),
    http.get(`${BASE}/api/users`, () => HttpResponse.json({ users: [] })),
  );
}

/** The action row of the document titled `title`. */
function rowOf(title: string): HTMLElement {
  const el = screen.getAllByText(title)[0];
  let node: HTMLElement | null = el;
  while (node && !within(node).queryByTitle('Neu indexieren')) node = node.parentElement;
  if (!node) throw new Error(`no row for ${title}`);
  return node;
}

describe('KnowledgePage — delete only where allowed', () => {
  beforeEach(() => {
    Element.prototype.scrollIntoView = vi.fn();
    wire();
  });

  it('shows a document delete button only with can_delete on its KB', async () => {
    renderWithProviders(<KnowledgePage />, { route: '/knowledge' });
    await screen.findByText('EigenesDok');
    // Wait for the KB list too — before it lands no KB grants delete.
    await screen.findByRole('button', { name: /Freigaben verwalten: Eigene/ });

    const del = (title: string) => within(rowOf(title)).queryByTitle('Löschen');
    expect(del('EigenesDok')).not.toBeNull();
    expect(del('SchalterDok')).not.toBeNull();
    expect(del('GeteiltesDok')).toBeNull();
    expect(del('VerwaltetDok')).toBeNull();
    // KB-less: the atom owner's, which the list does not carry — backend decides.
    expect(del('LosesDok')).not.toBeNull();
  });

  it('shows the KB delete button only with can_delete_kb, sharing for owner/admin', async () => {
    renderWithProviders(<KnowledgePage />, { route: '/knowledge' });
    await screen.findByRole('button', { name: /Freigaben verwalten: Eigene/ });

    expect(screen.getByRole('button', { name: /Knowledge Base löschen: Eigene/ })).toBeInTheDocument();
    for (const name of ['Geteilt', 'Schalter', 'Verwaltet']) {
      expect(screen.queryByRole('button', { name: new RegExp(`Knowledge Base löschen: ${name}`) })).toBeNull();
    }
    expect(screen.getByRole('button', { name: /Freigaben verwalten: Verwaltet/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Freigaben verwalten: Geteilt/ })).toBeNull();
    expect(screen.queryByRole('button', { name: /Freigaben verwalten: Schalter/ })).toBeNull();
  });

  it('opens the share dialog of a KB and closes it again', async () => {
    renderWithProviders(<KnowledgePage />, { route: '/knowledge' });
    fireEvent.click(await screen.findByRole('button', { name: /Freigaben verwalten: Verwaltet/ }));

    expect(await screen.findByText('Diese Wissensbasis ist mit niemandem geteilt.')).toBeInTheDocument();
    // Admin grantee: no switch offered.
    expect(screen.queryByRole('checkbox', { name: 'darf löschen' })).toBeNull();
    fireEvent.keyDown(document, { key: 'Escape' });
    await screen.findByText('EigenesDok');
  });
});

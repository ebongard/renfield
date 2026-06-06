/**
 * WissenDetailDrawer — PR3 universal detail drawer.
 *
 * The CRITICAL guarantee (plan test #5): tier edits route to the right id-space.
 * kg_node → useUpdateKgEntityTier by KG integer id; atom-backed types
 * (conversation_memory/kb_document/document_fact) → usePatchAtomTier by atom
 * UUID. Using the wrong one silently no-ops, so each path is asserted.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, screen } from '@testing-library/react';
import { renderWithRouter, userEvent } from '../test-utils';
import WissenDetailDrawer from '../../../../src/frontend/src/components/wissen/WissenDetailDrawer';
import type { AtomMatch } from '../../../../src/frontend/src/api/resources/brain';
import { usePatchAtomTier, useResetFactTier } from '../../../../src/frontend/src/api/resources/brain';
import { useUpdateKgEntityTier } from '../../../../src/frontend/src/api/resources/knowledgeGraph';
import { useMemoriesBySubjectQuery } from '../../../../src/frontend/src/api/resources/memories';

vi.mock('../../../../src/frontend/src/api/resources/brain', async (orig) => ({
  ...(await orig<typeof import('../../../../src/frontend/src/api/resources/brain')>()),
  usePatchAtomTier: vi.fn(),
  useResetFactTier: vi.fn(),
}));
vi.mock('../../../../src/frontend/src/api/resources/knowledgeGraph', async (orig) => ({
  ...(await orig<typeof import('../../../../src/frontend/src/api/resources/knowledgeGraph')>()),
  useUpdateKgEntityTier: vi.fn(),
}));
vi.mock('../../../../src/frontend/src/api/resources/memories', async (orig) => ({
  ...(await orig<typeof import('../../../../src/frontend/src/api/resources/memories')>()),
  useMemoriesBySubjectQuery: vi.fn(),
}));

const patchSpy = vi.fn();
const kgSpy = vi.fn();
const resetSpy = vi.fn();

beforeEach(() => {
  patchSpy.mockReset();
  kgSpy.mockReset();
  resetSpy.mockReset();
  vi.mocked(usePatchAtomTier).mockReturnValue({ mutate: patchSpy } as unknown as ReturnType<typeof usePatchAtomTier>);
  vi.mocked(useResetFactTier).mockReturnValue({ mutate: resetSpy } as unknown as ReturnType<typeof useResetFactTier>);
  vi.mocked(useUpdateKgEntityTier).mockReturnValue({ mutate: kgSpy } as unknown as ReturnType<typeof useUpdateKgEntityTier>);
  // default: no linked memories (EntityMemories renders nothing) — keeps the
  // pre-existing kg_node tests unaffected.
  vi.mocked(useMemoriesBySubjectQuery).mockReturnValue(
    { data: { memories: [], total: 0 }, isLoading: false } as unknown as ReturnType<typeof useMemoriesBySubjectQuery>,
  );
});

const kgNode: AtomMatch = {
  atom: { atom_id: 'kg_node:42', atom_type: 'kg_node', tier: 1, payload: { entity_id: 42, name: 'Müller GmbH', entity_type: 'organization' } },
  score: 1, snippet: 'Müller GmbH', rank: 1,
};
const memory: AtomMatch = {
  atom: { atom_id: 'mem-uuid', atom_type: 'conversation_memory', tier: 2, payload: { memory_id: 5, content: 'Mag Espresso', category: 'preference' } },
  score: 1, snippet: 'Mag Espresso', rank: 1,
};

describe('WissenDetailDrawer', () => {
  it('CRITICAL: kg_node tier edit uses the KG int-id endpoint, not the atom endpoint', async () => {
    renderWithRouter(<WissenDetailDrawer atom={kgNode} onClose={() => {}} />);
    expect(screen.getByText('Müller GmbH')).toBeInTheDocument();
    // "open in Graph" carries the entity int id.
    expect(screen.getByRole('link', { name: /Im Graph öffnen/ })).toHaveAttribute('href', '/wissen/graph?focus=42');

    fireEvent.change(screen.getByRole('combobox'), { target: { value: '3' } });

    expect(kgSpy).toHaveBeenCalledWith({ id: 42, circleTier: 3 });
    expect(patchSpy).not.toHaveBeenCalled();
  });

  it('CRITICAL: atom-backed (memory) tier edit uses the atom UUID endpoint', () => {
    renderWithRouter(<WissenDetailDrawer atom={memory} onClose={() => {}} />);
    expect(screen.getByText('Mag Espresso')).toBeInTheDocument();

    fireEvent.change(screen.getByRole('combobox'), { target: { value: '4' } });

    expect(patchSpy).toHaveBeenCalledWith({ atomId: 'mem-uuid', policy: { tier: 4 } });
    expect(kgSpy).not.toHaveBeenCalled();
  });

  it('Escape closes the drawer', async () => {
    const onClose = vi.fn();
    renderWithRouter(<WissenDetailDrawer atom={kgNode} onClose={onClose} />);
    await userEvent.keyboard('{Escape}');
    expect(onClose).toHaveBeenCalled();
  });

  it('renders nothing when no atom is open', () => {
    const { container } = renderWithRouter(<WissenDetailDrawer atom={null} onClose={() => {}} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('Phase 3c: memory drawer surfaces the subject (de)', () => {
    const memWithSubject: AtomMatch = {
      atom: {
        atom_id: 'mem-uuid-2', atom_type: 'conversation_memory', tier: 0,
        payload: { memory_id: 7, content: 'mag Tee', category: 'preference', subject_name: 'Jutta' },
      },
      score: 1, snippet: 'mag Tee', rank: 1,
    };
    renderWithRouter(<WissenDetailDrawer atom={memWithSubject} onClose={() => {}} />);
    expect(screen.getByText(/Über: Jutta/)).toBeInTheDocument();
  });

  it('Phase 3c: kg_node drawer lists memories about the entity', () => {
    vi.mocked(useMemoriesBySubjectQuery).mockReturnValue(
      {
        data: { memories: [{ id: 1, content: 'Jutta mag Tee', category: 'preference', importance: 0.6, access_count: 0, created_at: '' }], total: 1 },
        isLoading: false,
      } as unknown as ReturnType<typeof useMemoriesBySubjectQuery>,
    );
    renderWithRouter(<WissenDetailDrawer atom={kgNode} onClose={() => {}} />);
    expect(screen.getByText('Erinnerungen über diesen Knoten')).toBeInTheDocument();
    expect(screen.getByText('Jutta mag Tee')).toBeInTheDocument();
  });

  it('per-fact tier override: shows reset and routes it to the fact id', async () => {
    const overriddenFact: AtomMatch = {
      atom: {
        atom_id: 'fact-uuid', atom_type: 'document_fact', tier: 4,
        payload: { fact_id: 77, document_id: 9, kind: 'issuer', value: 'Finanzverwaltung NRW', tier_overridden: true },
      },
      score: 1, snippet: 'Finanzverwaltung NRW', rank: 1,
    };
    renderWithRouter(<WissenDetailDrawer atom={overriddenFact} onClose={() => {}} />);
    const reset = screen.getByRole('button', { name: /Auf Dokument-Tier zurücksetzen/ });
    await userEvent.click(reset);
    expect(resetSpy).toHaveBeenCalledWith(77);
  });

  it('per-fact tier override: no reset control when the fact is not overridden', () => {
    const inheritedFact: AtomMatch = {
      atom: {
        atom_id: 'fact-uuid2', atom_type: 'document_fact', tier: 0,
        payload: { fact_id: 78, document_id: 9, kind: 'amount', value: '89,90 EUR', tier_overridden: false },
      },
      score: 1, snippet: '89,90 EUR', rank: 1,
    };
    renderWithRouter(<WissenDetailDrawer atom={inheritedFact} onClose={() => {}} />);
    expect(screen.queryByRole('button', { name: /Auf Dokument-Tier zurücksetzen/ })).toBeNull();
  });
});

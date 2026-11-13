import { describe, it, expect, afterEach } from 'vitest';
import { screen, fireEvent } from '@testing-library/react';

import { FocusNeighborhood } from '../../../../../src/frontend/src/components/wissensbasis/FocusNeighborhood';
import { WissensbasisProvider } from '../../../../../src/frontend/src/context/WissensbasisContext';
import { renderWithRouter } from '../../test-utils';

const baseData = {
  focus: {
    entity_id: 'r1',
    display_name: 'REL-100',
    entity_type: 'release',
    importance: 0.5,
  },
  hop1: [],
  hop2: [],
  edges: [],
  overflow_hop1: 0,
  overflow_hop2: 0,
};

describe('FocusNeighborhood', () => {
  it('renders empty placeholder when no data', () => {
    renderWithRouter(
      <WissensbasisProvider>
        <FocusNeighborhood data={null} />
      </WissensbasisProvider>,
    );
    expect(screen.getByText(/Klicke auf einen Zitations-Chip/)).toBeInTheDocument();
  });

  it('renders focus card + hop1 chips + overflow link', () => {
    renderWithRouter(
      <WissensbasisProvider>
        <FocusNeighborhood
          data={{
            focus: {
              entity_id: 'r1',
              display_name: 'PRODUCT-A 1.3.5',
              entity_type: 'release',
              importance: 0.5,
            },
            hop1: [
              { entity_id: 'PAY-1', display_name: 'PAY-1', entity_type: 'ticket', importance: 0.3 },
              { entity_id: 'PAY-2', display_name: 'PAY-2', entity_type: 'ticket', importance: 0.2 },
            ],
            hop2: [],
            edges: [],
            overflow_hop1: 5,
            overflow_hop2: 0,
          }}
        />
      </WissensbasisProvider>,
    );

    expect(screen.getByText('PRODUCT-A 1.3.5')).toBeInTheDocument();
    // Tests default to DE — the importance line reads "Wichtigkeit 50%".
    expect(screen.getByText(/Wichtigkeit 50%/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Fokus auf PAY-1/ })).toBeInTheDocument();
    // Overflow link with +N more text
    expect(screen.getByText('+5 weitere')).toBeInTheDocument();
  });

  it('shows error message when one is provided', () => {
    renderWithRouter(
      <WissensbasisProvider>
        <FocusNeighborhood data={null} errorMessage="Backend down" />
      </WissensbasisProvider>,
    );
    expect(screen.getByRole('alert')).toHaveTextContent('Backend down');
  });

  // Sprint 2 — observed_fields + source_priority rendering.

  it('renders observed values when API includes them', () => {
    renderWithRouter(
      <WissensbasisProvider>
        <FocusNeighborhood
          data={{
            ...baseData,
            source_priority: 1,
            observed_fields: [
              {
                field_path: 'status',
                value: 'IN_PROGRESS',
                fetched_at: new Date().toISOString(),
                source_type: 'release',
              },
              {
                field_path: 'owner',
                value: 'alice',
                fetched_at: new Date().toISOString(),
                source_type: 'release',
              },
            ],
          }}
        />
      </WissensbasisProvider>,
    );
    expect(screen.getByText('status')).toBeInTheDocument();
    expect(screen.getByText('IN_PROGRESS')).toBeInTheDocument();
    expect(screen.getByText('owner')).toBeInTheDocument();
    expect(screen.getByText('alice')).toBeInTheDocument();
  });

  it('caps observed values at 5 and reveals more via the expand button', () => {
    const now = new Date().toISOString();
    const fields = Array.from({ length: 8 }, (_, i) => ({
      field_path: `field_${i}`,
      value: `value_${i}`,
      fetched_at: now,
      source_type: 'release',
    }));
    renderWithRouter(
      <WissensbasisProvider>
        <FocusNeighborhood data={{ ...baseData, observed_fields: fields }} />
      </WissensbasisProvider>,
    );
    // 5 visible by default — fields 0..4 should be rendered.
    expect(screen.getByText('field_0')).toBeInTheDocument();
    expect(screen.getByText('field_4')).toBeInTheDocument();
    // field_5+ are hidden until expand.
    expect(screen.queryByText('field_5')).not.toBeInTheDocument();
    // "+3 weitere Werte" button.
    const more = screen.getByRole('button', { name: /\+3 weitere/ });
    fireEvent.click(more);
    expect(screen.getByText('field_5')).toBeInTheDocument();
    expect(screen.getByText('field_7')).toBeInTheDocument();
  });

  it('renders source priority label when present', () => {
    renderWithRouter(
      <WissensbasisProvider>
        <FocusNeighborhood data={{ ...baseData, source_priority: 1 }} />
      </WissensbasisProvider>,
    );
    // German default — sourcePriority1 = "Aus zwischengespeicherten Snapshots".
    expect(screen.getByText(/Aus zwischengespeicherten Snapshots/)).toBeInTheDocument();
  });

  it('omits observed-values section when array is empty', () => {
    renderWithRouter(
      <WissensbasisProvider>
        <FocusNeighborhood data={{ ...baseData, observed_fields: [] }} />
      </WissensbasisProvider>,
    );
    // The "Beobachtete Werte" heading should not appear when the array is empty.
    expect(screen.queryByText(/Beobachtete Werte/)).not.toBeInTheDocument();
  });

  it('stringifies non-scalar observed values without crashing', () => {
    renderWithRouter(
      <WissensbasisProvider>
        <FocusNeighborhood
          data={{
            ...baseData,
            observed_fields: [
              {
                field_path: 'status',
                value: { name: 'To Do', category: 'To Do' }, // nested object (jira shape)
                fetched_at: new Date().toISOString(),
                source_type: 'jira',
              },
            ],
          }}
        />
      </WissensbasisProvider>,
    );
    expect(screen.getByText(/"name":"To Do"/)).toBeInTheDocument();
  });
});

describe('FocusNeighborhood — observed value age from a zone-less timestamp', () => {
  const originalTz = process.env.TZ;

  afterEach(() => {
    if (originalTz === undefined) delete process.env.TZ;
    else process.env.TZ = originalTz;
  });

  it('reads a naive timestamp as UTC, so 30 minutes ago stays 30 minutes', () => {
    // Read as local time in Berlin, the same value would be 1-2 h older.
    process.env.TZ = 'Europe/Berlin';
    const naive = new Date(Date.now() - 30 * 60_000 - 5_000).toISOString().replace('Z', '');
    renderWithRouter(
      <WissensbasisProvider>
        <FocusNeighborhood
          data={{
            ...baseData,
            observed_fields: [
              { field_path: 'status', value: 'OPEN', fetched_at: naive, source_type: 'release' },
            ],
          }}
        />
      </WissensbasisProvider>,
    );
    expect(screen.getByText(/vor 30 Min/)).toBeInTheDocument();
  });
});

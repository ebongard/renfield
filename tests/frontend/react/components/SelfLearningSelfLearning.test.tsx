/**
 * v2.10 self-learning admin console — focused unit tests for the
 * three small DRY components shared by the new pages.
 */
import { describe, it, expect, vi } from 'vitest';
import { fireEvent, screen } from '@testing-library/react';

import { renderWithProviders } from '../test-utils';
import NavBadge from '../../../../src/frontend/src/components/NavBadge';
import StatusBadge from '../../../../src/frontend/src/components/StatusBadge';
import SkillCard from '../../../../src/frontend/src/components/SkillCard';
import type { Skill } from '../../../../src/frontend/src/api/resources/skills';

const sampleSkill: Skill = {
  id: 42,
  title: 'Wohnzimmerlicht einschalten',
  body_md: '- Step 1\n- Step 2',
  trigger_examples: ['Licht an', 'Mach das Licht an'],
  tool_sequence: ['mcp.ha.turn_on', 'mcp.ha.get_state'],
  source: 'auto_extracted',
  status: 'draft',
  version: 1,
  success_count: 4,
  failure_count: 1,
  last_used_at: null,
  pinned: false,
  circle_tier: 0,
  atom_id: 'atom-x',
  merged_into_id: null,
  user_id: 9,
  created_at: '2026-05-26T10:00:00Z',
  updated_at: '2026-05-26T10:05:00Z',
  is_owner: false,
};

describe('NavBadge', () => {
  it('renders the count when > 0', () => {
    renderWithProviders(<NavBadge count={3} />);
    expect(screen.getByTestId('nav-badge')).toHaveTextContent('3');
  });

  it('renders 99+ when over 99', () => {
    renderWithProviders(<NavBadge count={250} />);
    expect(screen.getByTestId('nav-badge')).toHaveTextContent('99+');
  });

  it('renders nothing when count is 0', () => {
    renderWithProviders(<NavBadge count={0} />);
    expect(screen.queryByTestId('nav-badge')).toBeNull();
  });
});

describe('StatusBadge', () => {
  it.each(['draft', 'approved', 'rejected', 'archived'] as const)(
    'carries a colour-independent symbol for %s status',
    (status) => {
      renderWithProviders(<StatusBadge status={status} />);
      const el = screen.getByTestId(`status-badge-${status}`);
      // Per DESIGN.md, every status must include a glyph + label, not
      // just colour, so the badge remains readable in grayscale.
      expect(el.textContent?.trim().length ?? 0).toBeGreaterThan(0);
    },
  );
});

describe('SkillCard', () => {
  it('renders title, status, source, tool sequence', () => {
    renderWithProviders(<SkillCard skill={sampleSkill} />);
    expect(screen.getByText('Wohnzimmerlicht einschalten')).toBeInTheDocument();
    expect(screen.getByTestId('status-badge-draft')).toBeInTheDocument();
    expect(screen.getByTestId('tool-sequence')).toHaveTextContent(
      'mcp.ha.turn_on',
    );
  });

  it('fires approve/reject callbacks when admin actions are shown', () => {
    const onApprove = vi.fn();
    const onReject = vi.fn();
    renderWithProviders(
      <SkillCard
        skill={sampleSkill}
        showAdminActions
        onApprove={onApprove}
        onReject={onReject}
      />,
    );
    fireEvent.click(screen.getByTestId('approve-button'));
    fireEvent.click(screen.getByTestId('reject-button'));
    expect(onApprove).toHaveBeenCalledWith(42);
    expect(onReject).toHaveBeenCalledWith(42);
  });

  it('hides admin actions when status is not draft', () => {
    const approved: Skill = { ...sampleSkill, status: 'approved' };
    renderWithProviders(<SkillCard skill={approved} showAdminActions />);
    expect(screen.queryByTestId('approve-button')).toBeNull();
    expect(screen.queryByTestId('reject-button')).toBeNull();
  });
});

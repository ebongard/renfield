/**
 * The share dialog of a knowledge base (#1462): listing, adding, changing a
 * level, the owner's delete switch, and revoking — with the rules the backend
 * enforces mirrored in the controls:
 *  - the switch is operable only for the KB owner / kb.all (`can_delete_kb`);
 *  - a share that carries the switch is locked for an admin grantee (no level
 *    change, no revoke), an unswitched one is not.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { http, HttpResponse } from 'msw';
import { screen, fireEvent, waitFor, within } from '@testing-library/react';
import { server } from '../mocks/server';
import { renderWithProviders } from '../test-utils';
import KbShareDialog from '../../../../src/frontend/src/components/knowledge/KbShareDialog';
import type { KnowledgeBaseRow, KbShare } from '../../../../src/frontend/src/api/resources/knowledge';
import { TEST_CONFIG } from '../config';

const BASE = TEST_CONFIG.API_BASE_URL;

function share(userId: number, username: string, permission: KbShare['permission'], canDelete: boolean): KbShare {
  return {
    id: userId, user_id: userId, username, permission, can_delete: canDelete,
    granted_by: 1, granted_by_username: 'alice', created_at: '2026-11-01T00:00:00',
  };
}

const OWNER_KB: KnowledgeBaseRow = {
  id: 7, name: 'Haus', owner_id: 1, permission: 'owner', can_delete: true, can_delete_kb: true,
};
const ADMIN_GRANTEE_KB: KnowledgeBaseRow = {
  ...OWNER_KB, permission: 'admin', can_delete: false, can_delete_kb: false,
};

interface Captured {
  posts: Record<string, unknown>[];
  deletes: string[];
}

function wire(shares: KbShare[], opts: { usersStatus?: number; shareStatus?: number } = {}): Captured {
  const captured: Captured = { posts: [], deletes: [] };
  server.use(
    http.get(`${BASE}/api/knowledge/bases/7/permissions`, () => HttpResponse.json(shares)),
    http.get(`${BASE}/api/users`, () =>
      opts.usersStatus
        ? HttpResponse.json({ detail: 'forbidden' }, { status: opts.usersStatus })
        : HttpResponse.json({
          users: [
            { id: 1, username: 'alice', role_id: 1, is_active: true },
            { id: 2, username: 'bob', role_id: 2, is_active: true },
            { id: 3, username: 'carol', role_id: 2, is_active: true },
            { id: 4, username: 'dave', role_id: 2, is_active: true },
          ],
        })),
    http.post(`${BASE}/api/knowledge/bases/7/share`, async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>;
      captured.posts.push(body);
      if (opts.shareStatus) {
        return HttpResponse.json(
          { detail: 'Only the owner may change a share that carries the right to delete' },
          { status: opts.shareStatus },
        );
      }
      return HttpResponse.json(share(Number(body.user_id), 'x', 'read', Boolean(body.can_delete)));
    }),
    http.delete(`${BASE}/api/knowledge/bases/7/permissions/:uid`, ({ params }) => {
      captured.deletes.push(String(params.uid));
      return HttpResponse.json({ message: 'Permission revoked' });
    }),
  );
  return captured;
}

function rowOf(name: string): HTMLElement {
  const label = screen.getByText(name, { selector: 'span' });
  const row = label.closest('li');
  if (!row) throw new Error(`no row for ${name}`);
  return row;
}

describe('KbShareDialog', () => {
  beforeEach(() => {
    server.resetHandlers();
  });

  it('lists the shares with their level and the switch for the owner', async () => {
    wire([share(2, 'bob', 'write', false), share(3, 'carol', 'read', true)]);
    renderWithProviders(<KbShareDialog kb={OWNER_KB} isOpen onClose={() => {}} />);

    await screen.findByText('bob', { selector: 'span' });
    expect(screen.getByLabelText('Stufe für bob')).toHaveValue('write');
    expect(screen.getByLabelText('Stufe für carol')).toHaveValue('read');
    const bobSwitch = within(rowOf('bob')).getByRole('checkbox', { name: 'darf löschen' });
    const carolSwitch = within(rowOf('carol')).getByRole('checkbox', { name: 'darf löschen' });
    expect(bobSwitch).not.toBeChecked();
    expect(carolSwitch).toBeChecked();
    expect(within(rowOf('carol')).getByRole('button', { name: 'Freigabe für carol entziehen' })).toBeEnabled();
  });

  it('says so when nothing is shared', async () => {
    wire([]);
    renderWithProviders(<KbShareDialog kb={OWNER_KB} isOpen onClose={() => {}} />);
    expect(await screen.findByText('Diese Wissensbasis ist mit niemandem geteilt.')).toBeInTheDocument();
  });

  it('adds a share with the switch when the owner ticks it', async () => {
    const captured = wire([share(2, 'bob', 'write', false)]);
    renderWithProviders(<KbShareDialog kb={OWNER_KB} isOpen onClose={() => {}} />);
    await screen.findByText('bob', { selector: 'span' });

    const picker = await screen.findByLabelText('Person');
    // The owner and the already shared person are not offered.
    await waitFor(() => expect(within(picker).getByRole('option', { name: 'carol' })).toBeInTheDocument());
    expect(within(picker).queryByRole('option', { name: 'alice' })).toBeNull();
    expect(within(picker).queryByRole('option', { name: 'bob' })).toBeNull();

    fireEvent.change(picker, { target: { value: '3' } });
    fireEvent.change(screen.getByLabelText('Stufe'), { target: { value: 'write' } });
    const form = screen.getByRole('button', { name: 'Freigeben' }).closest('form');
    if (!form) throw new Error('no form');
    fireEvent.click(within(form).getByRole('checkbox', { name: 'darf löschen' }));
    fireEvent.click(screen.getByRole('button', { name: 'Freigeben' }));

    await waitFor(() => expect(captured.posts).toHaveLength(1));
    expect(captured.posts[0]).toEqual({ user_id: 3, permission: 'write', can_delete: true });
  });

  it('lets the owner flip the switch and change a level', async () => {
    const captured = wire([share(2, 'bob', 'write', false)]);
    renderWithProviders(<KbShareDialog kb={OWNER_KB} isOpen onClose={() => {}} />);
    await screen.findByText('bob', { selector: 'span' });

    fireEvent.click(within(rowOf('bob')).getByRole('checkbox', { name: 'darf löschen' }));
    await waitFor(() => expect(captured.posts).toHaveLength(1));
    expect(captured.posts[0]).toEqual({ user_id: 2, permission: 'write', can_delete: true });

    fireEvent.change(screen.getByLabelText('Stufe für bob'), { target: { value: 'admin' } });
    await waitFor(() => expect(captured.posts).toHaveLength(2));
    // A level change does not touch the switch: the field is omitted.
    expect(captured.posts[1]).toEqual({ user_id: 2, permission: 'admin' });
  });

  it('revokes a share', async () => {
    const captured = wire([share(2, 'bob', 'write', false)]);
    renderWithProviders(<KbShareDialog kb={OWNER_KB} isOpen onClose={() => {}} />);
    await screen.findByText('bob', { selector: 'span' });

    fireEvent.click(screen.getByRole('button', { name: 'Freigabe für bob entziehen' }));
    await waitFor(() => expect(captured.deletes).toEqual(['2']));
  });

  it('shows an admin grantee the switch read-only and locks switched shares', async () => {
    const captured = wire([share(2, 'bob', 'write', false), share(3, 'carol', 'read', true)]);
    renderWithProviders(<KbShareDialog kb={ADMIN_GRANTEE_KB} isOpen onClose={() => {}} />);
    await screen.findByText('bob', { selector: 'span' });

    // No switch anywhere — not in the rows, not in the add form.
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0);
    // The switched share is named, but locked.
    expect(within(rowOf('carol')).getByText('darf löschen')).toBeInTheDocument();
    expect(within(rowOf('bob')).queryByText('darf löschen')).toBeNull();
    expect(screen.getByLabelText('Stufe für carol')).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Freigabe für carol entziehen' })).toBeDisabled();

    // An unswitched share stays the admin grantee's to manage.
    expect(screen.getByLabelText('Stufe für bob')).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'Freigabe für bob entziehen' }));
    await waitFor(() => expect(captured.deletes).toEqual(['2']));

    // Adding sends no switch field.
    fireEvent.change(await screen.findByLabelText('Person'), { target: { value: '4' } });
    fireEvent.click(screen.getByRole('button', { name: 'Freigeben' }));
    await waitFor(() => expect(captured.posts).toHaveLength(1));
    expect(captured.posts[0]).toEqual({ user_id: 4, permission: 'read' });
  });

  it('shows a grantee with only per-document grants read-only, revocable', async () => {
    const docGrants: KbShare = {
      ...share(4, 'dave', 'read', false), permission: null, kind: 'document_grants', documents: 3,
    };
    const captured = wire([share(2, 'bob', 'write', false), docGrants]);
    renderWithProviders(<KbShareDialog kb={OWNER_KB} isOpen onClose={() => {}} />);
    await screen.findByText('dave', { selector: 'span' });

    const row = rowOf('dave');
    expect(within(row).getByText('Zugriff auf 3 einzelne Dokumente')).toBeInTheDocument();
    // No level, no switch — even for the owner.
    expect(within(row).queryByRole('combobox')).toBeNull();
    expect(within(row).queryByRole('checkbox')).toBeNull();
    // Still offered in the picker: a KB share may be given on top.
    const picker = await screen.findByLabelText('Person');
    await waitFor(() => expect(within(picker).getByRole('option', { name: 'dave' })).toBeInTheDocument());

    fireEvent.click(within(row).getByRole('button', { name: 'Freigabe für dave entziehen' }));
    await waitFor(() => expect(captured.deletes).toEqual(['4']));
  });

  it('falls back to an id field without the user list and shows the refusal', async () => {
    const captured = wire([], { usersStatus: 403, shareStatus: 403 });
    renderWithProviders(<KbShareDialog kb={ADMIN_GRANTEE_KB} isOpen onClose={() => {}} />);
    await screen.findByText('Diese Wissensbasis ist mit niemandem geteilt.');

    const idField = await screen.findByPlaceholderText('user_id');
    expect(screen.getByText(/Benutzer-ID/)).toBeInTheDocument();
    fireEvent.change(idField, { target: { value: '3' } });
    fireEvent.click(screen.getByRole('button', { name: 'Freigeben' }));

    await waitFor(() => expect(captured.posts).toHaveLength(1));
    expect(
      await screen.findByText('Only the owner may change a share that carries the right to delete'),
    ).toBeInTheDocument();
  });
});

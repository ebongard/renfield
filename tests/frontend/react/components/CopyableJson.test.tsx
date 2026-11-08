import { describe, it, expect, vi, afterEach } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import CopyableJson from '../../../../src/frontend/src/components/CopyableJson';
import { renderWithRouter } from '../test-utils';

// The clipboard API exists only in a secure context. A plain-HTTP instance and a
// page with an untrusted self-signed certificate refuse it (2026-11-08: the
// pairing JSON could not be carried to the other side at all).
const VALUE = { nonce: 'n-1', initiator_pubkey: 'ab'.repeat(32), signature: 'sig' };
const TEXT = JSON.stringify(VALUE);

const originalClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard');

function setClipboard(value: unknown) {
  Object.defineProperty(navigator, 'clipboard', { value, configurable: true });
}

function renderIt() {
  renderWithRouter(<CopyableJson id="json" value={VALUE} copyLabel="JSON kopieren" />);
  return screen.getByRole('textbox') as HTMLTextAreaElement;
}

afterEach(() => {
  if (originalClipboard) Object.defineProperty(navigator, 'clipboard', originalClipboard);
  else Reflect.deleteProperty(navigator, 'clipboard');
});

describe('CopyableJson', () => {
  it('shows the JSON as read-only text', () => {
    setClipboard({ writeText: vi.fn().mockResolvedValue(undefined) });
    const box = renderIt();
    expect(box.value).toBe(TEXT);
    expect(box.readOnly).toBe(true);
  });

  it('copies the JSON and reports no refusal', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    setClipboard({ writeText });
    renderIt();

    fireEvent.click(screen.getByRole('button', { name: 'JSON kopieren' }));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith(TEXT));
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it.each([
    ['without a clipboard (insecure context)', undefined],
    ['when the browser rejects the write', { writeText: vi.fn().mockRejectedValue(new Error('NotAllowedError')) }],
  ])('selects the text and says so %s', async (_case, clipboard) => {
    setClipboard(clipboard);
    const box = renderIt();

    fireEvent.click(screen.getByRole('button', { name: 'JSON kopieren' }));

    expect(await screen.findByRole('status')).toHaveTextContent(/kopieren abgelehnt/i);
    expect(box.selectionStart).toBe(0);
    expect(box.selectionEnd).toBe(TEXT.length);
  });
});

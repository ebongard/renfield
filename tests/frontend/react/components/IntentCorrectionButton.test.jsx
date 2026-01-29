import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import IntentCorrectionButton from '../../../../src/frontend/src/components/IntentCorrectionButton';
import { renderWithRouter } from '../test-utils.jsx';

describe('IntentCorrectionButton', () => {
  const defaultProps = {
    messageText: 'Was passierte 1989?',
    detectedIntent: 'knowledge.ask',
    feedbackType: 'intent',
    onCorrect: vi.fn(),
    proactive: false,
  };

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders the button', () => {
    renderWithRouter(
      <IntentCorrectionButton {...defaultProps} />
    );

    expect(screen.getByText('Falsch erkannt?')).toBeInTheDocument();
  });

  it('opens dropdown on click', () => {
    renderWithRouter(
      <IntentCorrectionButton {...defaultProps} />
    );

    fireEvent.click(screen.getByText('Falsch erkannt?'));

    expect(screen.getByText('Richtigen Intent wählen:')).toBeInTheDocument();
  });

  it('shows intent options excluding detected intent', () => {
    renderWithRouter(
      <IntentCorrectionButton {...defaultProps} detectedIntent="knowledge.ask" />
    );

    fireEvent.click(screen.getByText('Falsch erkannt?'));

    // Should show all options except knowledge.ask
    expect(screen.getByText('Allgemeine Konversation')).toBeInTheDocument();
    expect(screen.getByText('Smart Home')).toBeInTheDocument();
    expect(screen.getByText('Websuche')).toBeInTheDocument();
    expect(screen.getByText('Wetter')).toBeInTheDocument();
    expect(screen.getByText('Nachrichten')).toBeInTheDocument();

    // knowledge.ask should NOT be in the options (it's the detected intent)
    expect(screen.queryByText('Wissensdatenbank')).not.toBeInTheDocument();
  });

  it('calls onCorrect when option is selected', async () => {
    const onCorrect = vi.fn().mockResolvedValue(undefined);

    renderWithRouter(
      <IntentCorrectionButton {...defaultProps} onCorrect={onCorrect} />
    );

    fireEvent.click(screen.getByText('Falsch erkannt?'));
    fireEvent.click(screen.getByText('Allgemeine Konversation'));

    await waitFor(() => {
      expect(onCorrect).toHaveBeenCalledWith(
        'Was passierte 1989?',
        'intent',
        'knowledge.ask',
        'general.conversation'
      );
    });
  });

  it('shows success state after selection', async () => {
    const onCorrect = vi.fn().mockResolvedValue(undefined);

    renderWithRouter(
      <IntentCorrectionButton {...defaultProps} onCorrect={onCorrect} />
    );

    fireEvent.click(screen.getByText('Falsch erkannt?'));
    fireEvent.click(screen.getByText('Allgemeine Konversation'));

    await waitFor(() => {
      expect(screen.getByText('Korrektur gespeichert! Renfield lernt daraus.')).toBeInTheDocument();
    });

    // Button should no longer be visible
    expect(screen.queryByText('Falsch erkannt?')).not.toBeInTheDocument();
  });

  it('closes dropdown on toggle', () => {
    renderWithRouter(
      <IntentCorrectionButton {...defaultProps} />
    );

    // Open
    fireEvent.click(screen.getByText('Falsch erkannt?'));
    expect(screen.getByText('Richtigen Intent wählen:')).toBeInTheDocument();

    // Close
    fireEvent.click(screen.getByText('Falsch erkannt?'));
    expect(screen.queryByText('Richtigen Intent wählen:')).not.toBeInTheDocument();
  });

  it('shows complexity options for feedbackType=complexity', () => {
    renderWithRouter(
      <IntentCorrectionButton
        {...defaultProps}
        feedbackType="complexity"
        detectedIntent="simple"
      />
    );

    fireEvent.click(screen.getByText('Falsche Einstufung?'));

    expect(screen.getByText('Richtige Einstufung wählen:')).toBeInTheDocument();
    expect(screen.getByText('Hätte Agent verwenden sollen')).toBeInTheDocument();
    expect(screen.getByText('War zu komplex eingestuft')).toBeInTheDocument();
  });

  it('opens automatically when proactive is true', () => {
    renderWithRouter(
      <IntentCorrectionButton {...defaultProps} proactive={true} />
    );

    // Dropdown should be open immediately
    expect(screen.getByText('Richtigen Intent wählen:')).toBeInTheDocument();
  });

  it('shows proactive question text when proactive and closed', () => {
    renderWithRouter(
      <IntentCorrectionButton {...defaultProps} proactive={true} />
    );

    // Close the dropdown first
    fireEvent.click(screen.getByText('Falsch erkannt?'));

    expect(screen.getByText('War das die richtige Zuordnung?')).toBeInTheDocument();
  });
});

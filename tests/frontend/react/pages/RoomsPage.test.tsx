import { createElement, Fragment } from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { BASE_URL } from '../mocks/handlers';
import RoomsPage from '../../../../src/frontend/src/pages/RoomsPage';
import { renderWithProviders } from '../test-utils';
import type { ModalProps } from '../../../../src/frontend/src/components/Modal';
import type { UseConfirmDialogResult } from '../../../../src/frontend/src/components/ConfirmDialog';
import type { RoomOutputSettingsProps } from '../../../../src/frontend/src/components/RoomOutputSettings';
import type { Room, HAArea, CreateRoomInput } from '../../../../src/frontend/src/api/resources/rooms';

// Mock data
const mockRooms: Room[] = [
  {
    id: 1,
    name: 'Wohnzimmer',
    alias: 'wohnzimmer',
    icon: 'mdi:sofa',
    source: 'homeassistant',
    ha_area_id: 'area_1',
    device_count: 2,
    online_count: 1,
    devices: [
      { device_id: 'sat-1', device_name: 'Satellite', device_type: 'satellite', is_online: true },
      { device_id: 'web-1', device_name: 'Tablet', device_type: 'web_tablet', is_online: false },
    ],
  },
  {
    id: 2,
    name: 'Kueche',
    alias: 'kueche',
    icon: null,
    source: 'renfield',
    ha_area_id: null,
    device_count: 0,
    online_count: 0,
    devices: [],
  },
];

const mockHAAreas: HAArea[] = [
  { area_id: 'area_1', name: 'Living Room', is_linked: true, linked_room_name: 'Wohnzimmer' },
  { area_id: 'area_2', name: 'Kitchen', is_linked: false },
  { area_id: 'area_3', name: 'Bedroom', is_linked: false },
];

// Mock ConfirmDialog
vi.mock('../../../../src/frontend/src/components/ConfirmDialog', () => {
  const result: UseConfirmDialogResult = {
    confirm: () => Promise.resolve(true),
    ConfirmDialogComponent: createElement(Fragment),
  };
  return {
    useConfirmDialog: (): UseConfirmDialogResult => result,
  };
});

// Mock Modal component
vi.mock('../../../../src/frontend/src/components/Modal', () => ({
  default: ({ isOpen, onClose, title, children }: ModalProps) => {
    if (!isOpen) return null;
    return (
      <div data-testid="modal">
        <h2>{title}</h2>
        <button onClick={onClose}>Schließen</button>
        {children}
      </div>
    );
  },
}));

// Mock RoomOutputSettings
vi.mock('../../../../src/frontend/src/components/RoomOutputSettings', () => ({
  default: ({ roomId, roomName, outputType = 'audio' }: RoomOutputSettingsProps) => (
    <div data-testid={`room-output-settings-${roomId}-${outputType}`}>
      Output settings for {roomName}
    </div>
  ),
}));

describe('RoomsPage', () => {
  beforeEach(() => {
    server.resetHandlers();
    server.use(
      http.get(`${BASE_URL}/api/rooms`, () => {
        return HttpResponse.json(mockRooms);
      }),
      http.get(`${BASE_URL}/api/rooms/ha/areas`, () => {
        return HttpResponse.json(mockHAAreas);
      }),
    );
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  describe('Rendering', () => {
    it('renders the page title', async () => {
      renderWithProviders(<RoomsPage />);

      expect(screen.getByText('Raumverwaltung')).toBeInTheDocument();
      expect(screen.getByText('Verwalte Räume und synchronisiere mit Home Assistant')).toBeInTheDocument();
    });

    it('shows loading state initially', () => {
      renderWithProviders(<RoomsPage />);

      expect(screen.getByText('Lade Räume...')).toBeInTheDocument();
    });

    it('displays rooms after loading', async () => {
      renderWithProviders(<RoomsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wohnzimmer')).toBeInTheDocument();
      });

      expect(screen.getByText('Kueche')).toBeInTheDocument();
    });

    it('shows room count in heading', async () => {
      renderWithProviders(<RoomsPage />);

      await waitFor(() => {
        expect(screen.getByText(/Räume \(2\)/)).toBeInTheDocument();
      });
    });

    it('shows HA badge for rooms imported from Home Assistant', async () => {
      renderWithProviders(<RoomsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wohnzimmer')).toBeInTheDocument();
      });

      expect(screen.getByText('HA')).toBeInTheDocument();
    });

    it('shows Renfield badge for locally created rooms', async () => {
      renderWithProviders(<RoomsPage />);

      await waitFor(() => {
        expect(screen.getByText('Kueche')).toBeInTheDocument();
      });

      expect(screen.getByText('Renfield')).toBeInTheDocument();
    });

    it('shows device count for rooms', async () => {
      renderWithProviders(<RoomsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wohnzimmer')).toBeInTheDocument();
      });

      expect(screen.getByText(/\(1 online\)/)).toBeInTheDocument();
    });

    it('shows devices in room', async () => {
      renderWithProviders(<RoomsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wohnzimmer')).toBeInTheDocument();
      });

      const deviceNames = screen.getAllByText(/Satellite|Tablet/);
      expect(deviceNames.length).toBeGreaterThan(0);
    });

    it('shows HA link status', async () => {
      renderWithProviders(<RoomsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wohnzimmer')).toBeInTheDocument();
      });

      expect(screen.getByText('Verknüpft')).toBeInTheDocument();
      expect(screen.getByText('Nicht verknüpft')).toBeInTheDocument();
    });
  });

  describe('Empty State', () => {
    it('shows empty state when no rooms exist', async () => {
      server.use(
        http.get(`${BASE_URL}/api/rooms`, () => {
          return HttpResponse.json([]);
        }),
      );

      renderWithProviders(<RoomsPage />);

      await waitFor(() => {
        expect(screen.getByText('Noch keine Räume vorhanden')).toBeInTheDocument();
      });

      expect(screen.getByText('Ersten Raum anlegen')).toBeInTheDocument();
    });
  });

  describe('Create Room', () => {
    it('opens create modal when clicking new room button', async () => {
      const user = userEvent.setup();
      renderWithProviders(<RoomsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wohnzimmer')).toBeInTheDocument();
      });

      await user.click(screen.getByText('Neuer Raum'));

      expect(screen.getByText('Neuen Raum anlegen')).toBeInTheDocument();
    });

    it('creates room with form data', async () => {
      const user = userEvent.setup();
      let createdRoom: Partial<CreateRoomInput> | null = null;

      server.use(
        http.post(`${BASE_URL}/api/rooms`, async ({ request }) => {
          createdRoom = (await request.json()) as Partial<CreateRoomInput>;
          return HttpResponse.json(
            {
              id: 3,
              ...createdRoom,
              alias: 'schlafzimmer',
              source: 'renfield',
              ha_area_id: null,
              device_count: 0,
              online_count: 0,
              devices: [],
            },
            { status: 201 },
          );
        }),
      );

      renderWithProviders(<RoomsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wohnzimmer')).toBeInTheDocument();
      });

      await user.click(screen.getByText('Neuer Raum'));

      await user.type(screen.getByPlaceholderText('Wohnzimmer'), 'Schlafzimmer');
      await user.type(screen.getByPlaceholderText('mdi:sofa'), 'mdi:bed');

      await user.click(screen.getByText('Erstellen'));

      await waitFor(() => {
        expect(createdRoom).not.toBeNull();
      });

      expect(createdRoom!.name).toBe('Schlafzimmer');
      expect(createdRoom!.icon).toBe('mdi:bed');
    });
  });

  describe('Delete Room', () => {
    it('deletes room when clicking delete button', async () => {
      const user = userEvent.setup();
      let deletedId: string | readonly string[] | null = null;

      server.use(
        http.delete(`${BASE_URL}/api/rooms/:id`, ({ params }) => {
          deletedId = params.id ?? null;
          return HttpResponse.json({ message: 'Room deleted' });
        }),
      );

      renderWithProviders(<RoomsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wohnzimmer')).toBeInTheDocument();
      });

      const deleteButtons = screen.getAllByLabelText(/löschen/i);
      await user.click(deleteButtons[0]);

      await waitFor(() => {
        expect(deletedId).not.toBeNull();
      });
    });
  });

  describe('HA Sync', () => {
    it('opens sync panel when clicking HA Sync button', async () => {
      const user = userEvent.setup();
      renderWithProviders(<RoomsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wohnzimmer')).toBeInTheDocument();
      });

      await user.click(screen.getByText('HA Sync'));

      await waitFor(() => {
        expect(screen.getByText('Home Assistant Synchronisation')).toBeInTheDocument();
      });
    });

    it('shows HA areas in sync panel', async () => {
      const user = userEvent.setup();
      renderWithProviders(<RoomsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wohnzimmer')).toBeInTheDocument();
      });

      await user.click(screen.getByText('HA Sync'));

      await waitFor(() => {
        expect(screen.getByText('Living Room')).toBeInTheDocument();
      });

      expect(screen.getByText('Kitchen')).toBeInTheDocument();
      expect(screen.getByText('Bedroom')).toBeInTheDocument();
    });

    it('shows Import, Export and Sync buttons', async () => {
      const user = userEvent.setup();
      renderWithProviders(<RoomsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wohnzimmer')).toBeInTheDocument();
      });

      await user.click(screen.getByText('HA Sync'));

      await waitFor(() => {
        expect(screen.getByText('Import')).toBeInTheDocument();
      });

      expect(screen.getByText('Export')).toBeInTheDocument();
      expect(screen.getByText('Sync')).toBeInTheDocument();
    });
  });

  describe('Error Handling', () => {
    it('shows error when loading fails', async () => {
      server.use(
        http.get(`${BASE_URL}/api/rooms`, () => {
          return new HttpResponse(null, { status: 500 });
        }),
      );

      renderWithProviders(<RoomsPage />);

      await waitFor(() => {
        expect(screen.getByText(/Räume konnten nicht geladen werden/i)).toBeInTheDocument();
      });
    });
  });

  describe('Room Actions', () => {
    it('shows refresh button', async () => {
      renderWithProviders(<RoomsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wohnzimmer')).toBeInTheDocument();
      });

      expect(screen.getByLabelText('Räume aktualisieren')).toBeInTheDocument();
    });

    it('shows edit button for each room', async () => {
      renderWithProviders(<RoomsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wohnzimmer')).toBeInTheDocument();
      });

      const editButtons = screen.getAllByLabelText(/bearbeiten/i);
      expect(editButtons.length).toBe(2);
    });

    it('shows link button for unlinked rooms', async () => {
      renderWithProviders(<RoomsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wohnzimmer')).toBeInTheDocument();
      });

      expect(screen.getByText('Verknüpfen')).toBeInTheDocument();
    });

    it('shows unlink button for linked rooms', async () => {
      renderWithProviders(<RoomsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wohnzimmer')).toBeInTheDocument();
      });

      expect(screen.getByText('Trennen')).toBeInTheDocument();
    });
  });

  describe('Output Settings', () => {
    it('shows output settings component for each room', async () => {
      renderWithProviders(<RoomsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wohnzimmer')).toBeInTheDocument();
      });

      expect(screen.getByTestId('room-output-settings-1-audio')).toBeInTheDocument();
      expect(screen.getByTestId('room-output-settings-2-audio')).toBeInTheDocument();
    });
  });
});

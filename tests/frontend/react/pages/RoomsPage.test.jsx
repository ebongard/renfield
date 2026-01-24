import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server.js';
import { BASE_URL } from '../mocks/handlers.js';
import RoomsPage from '../../../../src/frontend/src/pages/RoomsPage';
import { renderWithProviders } from '../test-utils.jsx';

// Mock data
const mockRooms = [
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
      { device_id: 'web-1', device_name: 'Tablet', device_type: 'web_tablet', is_online: false }
    ]
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
    devices: []
  }
];

const mockHAAreas = [
  { area_id: 'area_1', name: 'Living Room', is_linked: true, linked_room_name: 'Wohnzimmer' },
  { area_id: 'area_2', name: 'Kitchen', is_linked: false, linked_room_name: null },
  { area_id: 'area_3', name: 'Bedroom', is_linked: false, linked_room_name: null }
];

// Mock ConfirmDialog
vi.mock('../../../../src/frontend/src/components/ConfirmDialog', () => ({
  useConfirmDialog: () => ({
    confirm: vi.fn().mockResolvedValue(true),
    ConfirmDialogComponent: null
  })
}));

// Mock Modal component
vi.mock('../../../../src/frontend/src/components/Modal', () => ({
  default: ({ isOpen, onClose, title, children }) => {
    if (!isOpen) return null;
    return (
      <div data-testid="modal">
        <h2>{title}</h2>
        <button onClick={onClose}>Close</button>
        {children}
      </div>
    );
  }
}));

// Mock RoomOutputSettings
vi.mock('../../../../src/frontend/src/components/RoomOutputSettings', () => ({
  default: ({ roomId, roomName }) => (
    <div data-testid={`room-output-settings-${roomId}`}>
      Output settings for {roomName}
    </div>
  )
}));

describe('RoomsPage', () => {
  beforeEach(() => {
    server.resetHandlers();
    // Set up default handlers
    server.use(
      http.get(`${BASE_URL}/api/rooms`, () => {
        return HttpResponse.json(mockRooms);
      }),
      http.get(`${BASE_URL}/api/rooms/ha/areas`, () => {
        return HttpResponse.json(mockHAAreas);
      })
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
        expect(screen.getByText(/Raeume \(2\)/)).toBeInTheDocument();
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

      // Check that online count is displayed
      expect(screen.getByText(/\(1 online\)/)).toBeInTheDocument();
    });

    it('shows devices in room', async () => {
      renderWithProviders(<RoomsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wohnzimmer')).toBeInTheDocument();
      });

      // Find the device names within the room card
      // Note: "Satellite" label also exists as device type, so we check for the device name
      const deviceNames = screen.getAllByText(/Satellite|Tablet/);
      expect(deviceNames.length).toBeGreaterThan(0);
    });

    it('shows HA link status', async () => {
      renderWithProviders(<RoomsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wohnzimmer')).toBeInTheDocument();
      });

      expect(screen.getByText('Verknuepft')).toBeInTheDocument();
      expect(screen.getByText('Nicht verknuepft')).toBeInTheDocument();
    });
  });

  describe('Empty State', () => {
    it('shows empty state when no rooms exist', async () => {
      server.use(
        http.get(`${BASE_URL}/api/rooms`, () => {
          return HttpResponse.json([]);
        })
      );

      renderWithProviders(<RoomsPage />);

      await waitFor(() => {
        expect(screen.getByText('Noch keine Raeume vorhanden')).toBeInTheDocument();
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
      let createdRoom = null;

      server.use(
        http.post(`${BASE_URL}/api/rooms`, async ({ request }) => {
          createdRoom = await request.json();
          return HttpResponse.json({
            id: 3,
            ...createdRoom,
            alias: 'schlafzimmer',
            source: 'renfield',
            ha_area_id: null,
            device_count: 0,
            online_count: 0,
            devices: []
          }, { status: 201 });
        })
      );

      renderWithProviders(<RoomsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wohnzimmer')).toBeInTheDocument();
      });

      await user.click(screen.getByText('Neuer Raum'));

      // Fill in form
      await user.type(screen.getByPlaceholderText('Wohnzimmer'), 'Schlafzimmer');
      await user.type(screen.getByPlaceholderText('mdi:sofa'), 'mdi:bed');

      // Submit
      await user.click(screen.getByText('Erstellen'));

      await waitFor(() => {
        expect(createdRoom).not.toBeNull();
      });

      expect(createdRoom.name).toBe('Schlafzimmer');
      expect(createdRoom.icon).toBe('mdi:bed');
    });
  });

  describe('Delete Room', () => {
    it('deletes room when clicking delete button', async () => {
      const user = userEvent.setup();
      let deletedId = null;

      server.use(
        http.delete(`${BASE_URL}/api/rooms/:id`, ({ params }) => {
          deletedId = params.id;
          return HttpResponse.json({ message: 'Room deleted' });
        })
      );

      renderWithProviders(<RoomsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wohnzimmer')).toBeInTheDocument();
      });

      // Find and click delete button for first room
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
          return HttpResponse.json(
            { detail: 'Failed to load rooms' },
            { status: 500 }
          );
        })
      );

      renderWithProviders(<RoomsPage />);

      await waitFor(() => {
        expect(screen.getByText(/Raeume konnten nicht geladen werden/i)).toBeInTheDocument();
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

      // Kueche is not linked
      expect(screen.getByText('Verknuepfen')).toBeInTheDocument();
    });

    it('shows unlink button for linked rooms', async () => {
      renderWithProviders(<RoomsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wohnzimmer')).toBeInTheDocument();
      });

      // Wohnzimmer is linked
      expect(screen.getByText('Trennen')).toBeInTheDocument();
    });
  });

  describe('Output Settings', () => {
    it('shows output settings component for each room', async () => {
      renderWithProviders(<RoomsPage />);

      await waitFor(() => {
        expect(screen.getByText('Wohnzimmer')).toBeInTheDocument();
      });

      expect(screen.getByTestId('room-output-settings-1')).toBeInTheDocument();
      expect(screen.getByTestId('room-output-settings-2')).toBeInTheDocument();
    });
  });
});

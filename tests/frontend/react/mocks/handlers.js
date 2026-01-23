import { http, HttpResponse } from 'msw';
import { TEST_CONFIG } from '../config.js';

// Base URL from configuration (can be overridden via VITE_API_URL env var)
const BASE_URL = TEST_CONFIG.API_BASE_URL;

// Default mock data
const mockPlugins = [
  {
    name: 'weather',
    version: '1.0.0',
    description: 'Weather information plugin',
    author: 'Renfield',
    enabled: true,
    enabled_var: 'WEATHER_ENABLED',
    has_config: true,
    config_vars: ['WEATHER_API_KEY'],
    intents: [
      {
        name: 'weather.get_current',
        description: 'Get current weather',
        parameters: [
          { name: 'location', type: 'string', required: true, description: 'Location name' }
        ]
      }
    ],
    rate_limit: 60
  },
  {
    name: 'calendar',
    version: '1.0.0',
    description: 'Calendar integration plugin',
    author: 'Renfield',
    enabled: false,
    enabled_var: 'CALENDAR_ENABLED',
    has_config: true,
    config_vars: ['CALENDAR_URL', 'CALENDAR_API_KEY'],
    intents: [
      {
        name: 'calendar.get_events',
        description: 'Get calendar events',
        parameters: []
      }
    ],
    rate_limit: null
  }
];

const mockRoles = [
  {
    id: 1,
    name: 'Admin',
    description: 'Full access to all resources',
    permissions: ['admin', 'plugins.manage', 'kb.all', 'ha.full'],
    allowed_plugins: [],
    is_system: true,
    user_count: 1,
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z'
  },
  {
    id: 2,
    name: 'User',
    description: 'Standard user access',
    permissions: ['plugins.use', 'kb.own', 'ha.control'],
    allowed_plugins: ['weather'],
    is_system: false,
    user_count: 5,
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z'
  }
];

const mockPermissions = [
  { value: 'plugins.none', name: 'PLUGINS_NONE', description: 'No plugin access' },
  { value: 'plugins.use', name: 'PLUGINS_USE', description: 'Use plugins' },
  { value: 'plugins.manage', name: 'PLUGINS_MANAGE', description: 'Manage plugins' },
  { value: 'admin', name: 'ADMIN', description: 'Admin access' },
  { value: 'kb.all', name: 'KB_ALL', description: 'All knowledge bases' },
  { value: 'ha.full', name: 'HA_FULL', description: 'Full Home Assistant access' }
];

export const handlers = [
  // Plugins API
  http.get(`${BASE_URL}/api/plugins`, () => {
    return HttpResponse.json({
      plugins: mockPlugins,
      total: mockPlugins.length,
      plugins_enabled: true
    });
  }),

  http.get(`${BASE_URL}/api/plugins/:name`, ({ params }) => {
    const plugin = mockPlugins.find(p => p.name === params.name);
    if (!plugin) {
      return new HttpResponse(null, { status: 404 });
    }
    return HttpResponse.json(plugin);
  }),

  http.post(`${BASE_URL}/api/plugins/:name/toggle`, async ({ params, request }) => {
    const body = await request.json();
    const plugin = mockPlugins.find(p => p.name === params.name);
    if (!plugin) {
      return new HttpResponse(null, { status: 404 });
    }
    return HttpResponse.json({
      name: params.name,
      enabled: body.enabled,
      message: `Plugin ${params.name} ${body.enabled ? 'enabled' : 'disabled'}. Restart required.`,
      requires_restart: true
    });
  }),

  // Roles API
  http.get(`${BASE_URL}/api/roles`, () => {
    return HttpResponse.json(mockRoles);
  }),

  http.get(`${BASE_URL}/api/roles/:id`, ({ params }) => {
    const role = mockRoles.find(r => r.id === parseInt(params.id));
    if (!role) {
      return new HttpResponse(null, { status: 404 });
    }
    return HttpResponse.json(role);
  }),

  http.post(`${BASE_URL}/api/roles`, async ({ request }) => {
    const body = await request.json();
    const newRole = {
      id: mockRoles.length + 1,
      ...body,
      is_system: false,
      user_count: 0,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString()
    };
    return HttpResponse.json(newRole, { status: 201 });
  }),

  http.patch(`${BASE_URL}/api/roles/:id`, async ({ params, request }) => {
    const body = await request.json();
    const role = mockRoles.find(r => r.id === parseInt(params.id));
    if (!role) {
      return new HttpResponse(null, { status: 404 });
    }
    const updatedRole = {
      ...role,
      ...body,
      updated_at: new Date().toISOString()
    };
    return HttpResponse.json(updatedRole);
  }),

  http.delete(`${BASE_URL}/api/roles/:id`, ({ params }) => {
    const role = mockRoles.find(r => r.id === parseInt(params.id));
    if (!role) {
      return new HttpResponse(null, { status: 404 });
    }
    if (role.is_system) {
      return HttpResponse.json({ detail: 'Cannot delete system roles' }, { status: 403 });
    }
    return HttpResponse.json({ message: `Role '${role.name}' deleted successfully` });
  }),

  // Permissions API
  http.get(`${BASE_URL}/api/auth/permissions`, () => {
    return HttpResponse.json(mockPermissions);
  }),

  // Auth API
  http.get(`${BASE_URL}/api/auth/me`, () => {
    return HttpResponse.json({
      id: 1,
      username: 'admin',
      email: 'admin@example.com',
      role: 'Admin',
      role_id: 1,
      permissions: ['admin', 'plugins.manage', 'kb.all', 'ha.full'],
      is_active: true
    });
  }),

  http.get(`${BASE_URL}/api/auth/status`, () => {
    return HttpResponse.json({
      auth_enabled: true,
      allow_registration: false
    });
  })
];

// Export BASE_URL for use in tests that need to override handlers
export { BASE_URL, mockPlugins, mockRoles, mockPermissions };

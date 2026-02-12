import { http, HttpResponse } from 'msw';
import { TEST_CONFIG } from '../config.js';

// Base URL from configuration (can be overridden via VITE_API_URL env var)
const BASE_URL = TEST_CONFIG.API_BASE_URL;

// Mock users data
const mockUsers = [
  {
    id: 1,
    username: 'admin',
    email: 'admin@example.com',
    role_id: 1,
    role_name: 'Admin',
    is_active: true,
    speaker_id: null,
    last_login: '2024-01-15T10:30:00Z',
    created_at: '2024-01-01T00:00:00Z'
  },
  {
    id: 2,
    username: 'user1',
    email: 'user1@example.com',
    role_id: 2,
    role_name: 'User',
    is_active: true,
    speaker_id: 1,
    last_login: '2024-01-14T15:00:00Z',
    created_at: '2024-01-02T00:00:00Z'
  },
  {
    id: 3,
    username: 'inactive_user',
    email: null,
    role_id: 2,
    role_name: 'User',
    is_active: false,
    speaker_id: null,
    last_login: null,
    created_at: '2024-01-03T00:00:00Z'
  }
];

// Mock speakers data
const mockSpeakers = [
  { id: 1, name: 'Speaker 1', embedding_count: 5 },
  { id: 2, name: 'Speaker 2', embedding_count: 3 }
];

// Mock health data
const mockHealth = {
  status: 'ok',
  services: {
    ollama: 'ok',
    database: 'ok',
    redis: 'ok'
  }
};

// Mock conversations data
const mockConversations = [
  {
    session_id: 'session-today-1',
    preview: 'Wie ist das Wetter heute?',
    message_count: 4,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString()
  },
  {
    session_id: 'session-today-2',
    preview: 'Schalte das Licht an',
    message_count: 2,
    created_at: new Date().toISOString(),
    updated_at: new Date(Date.now() - 3600000).toISOString() // 1 hour ago
  },
  {
    session_id: 'session-yesterday-1',
    preview: 'Was gibt es Neues?',
    message_count: 6,
    created_at: new Date(Date.now() - 86400000).toISOString(),
    updated_at: new Date(Date.now() - 86400000).toISOString() // Yesterday
  },
  {
    session_id: 'session-old-1',
    preview: 'Ältere Konversation',
    message_count: 10,
    created_at: new Date(Date.now() - 86400000 * 10).toISOString(),
    updated_at: new Date(Date.now() - 86400000 * 10).toISOString() // 10 days ago
  }
];

// Mock conversation history
const mockConversationHistory = {
  'session-today-1': [
    { role: 'user', content: 'Wie ist das Wetter heute?', timestamp: new Date().toISOString() },
    { role: 'assistant', content: 'Das Wetter ist sonnig mit 22°C.', timestamp: new Date().toISOString() },
    { role: 'user', content: 'Danke!', timestamp: new Date().toISOString() },
    { role: 'assistant', content: 'Gerne geschehen!', timestamp: new Date().toISOString() }
  ],
  'session-today-2': [
    { role: 'user', content: 'Schalte das Licht an', timestamp: new Date().toISOString() },
    { role: 'assistant', content: 'Ich habe das Licht eingeschaltet.', timestamp: new Date().toISOString() }
  ]
};

// MCP mock data
const mockMcpStatus = {
  enabled: true,
  total_tools: 15,
  servers: [
    {
      name: 'weather',
      connected: true,
      transport: 'stdio',
      tool_count: 5,
      last_error: null
    },
    {
      name: 'homeassistant',
      connected: true,
      transport: 'streamable_http',
      tool_count: 8,
      last_error: null
    },
    {
      name: 'search',
      connected: false,
      transport: 'stdio',
      tool_count: 2,
      last_error: 'Connection timeout'
    }
  ]
};

const mockMcpTools = [
  {
    name: 'weather__get_current',
    original_name: 'get_current',
    server: 'weather',
    description: 'Get current weather for a location',
    input_schema: {
      type: 'object',
      properties: {
        location: { type: 'string', description: 'City name' }
      },
      required: ['location']
    }
  },
  {
    name: 'weather__get_forecast',
    original_name: 'get_forecast',
    server: 'weather',
    description: 'Get weather forecast',
    input_schema: {
      type: 'object',
      properties: {
        location: { type: 'string' },
        days: { type: 'integer', default: 5 }
      }
    }
  },
  {
    name: 'homeassistant__turn_on',
    original_name: 'turn_on',
    server: 'homeassistant',
    description: 'Turn on a Home Assistant entity',
    input_schema: {
      type: 'object',
      properties: {
        entity_id: { type: 'string' }
      },
      required: ['entity_id']
    }
  },
  {
    name: 'homeassistant__turn_off',
    original_name: 'turn_off',
    server: 'homeassistant',
    description: 'Turn off a Home Assistant entity',
    input_schema: {
      type: 'object',
      properties: {
        entity_id: { type: 'string' }
      },
      required: ['entity_id']
    }
  }
];

const mockRoles = [
  {
    id: 1,
    name: 'Admin',
    description: 'Full access to all resources',
    permissions: ['admin', 'kb.all', 'ha.full'],
    is_system: true,
    user_count: 1,
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z'
  },
  {
    id: 2,
    name: 'User',
    description: 'Standard user access',
    permissions: ['kb.own', 'ha.control'],
    is_system: false,
    user_count: 5,
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z'
  }
];

const mockPermissions = [
  { value: 'admin', name: 'ADMIN', description: 'Admin access' },
  { value: 'kb.all', name: 'KB_ALL', description: 'All knowledge bases' },
  { value: 'ha.full', name: 'HA_FULL', description: 'Full Home Assistant access' }
];

export const handlers = [
  // MCP API
  http.get(`${BASE_URL}/api/mcp/status`, () => {
    return HttpResponse.json(mockMcpStatus);
  }),

  http.get(`${BASE_URL}/api/mcp/tools`, () => {
    return HttpResponse.json({
      tools: mockMcpTools,
      total: mockMcpTools.length
    });
  }),

  http.post(`${BASE_URL}/api/mcp/refresh`, () => {
    return HttpResponse.json({
      message: 'MCP connections refreshed successfully',
      servers_reconnected: 1
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
      permissions: ['admin', 'kb.all', 'ha.full'],
      is_active: true
    });
  }),

  http.get(`${BASE_URL}/api/auth/status`, () => {
    return HttpResponse.json({
      auth_enabled: true,
      allow_registration: false
    });
  }),

  // Login API
  http.post(`${BASE_URL}/api/auth/login`, async ({ request }) => {
    const body = await request.json();
    if (body.username === 'admin' && body.password === 'password123') {
      return HttpResponse.json({
        access_token: 'mock-access-token',
        refresh_token: 'mock-refresh-token',
        token_type: 'bearer'
      });
    }
    return HttpResponse.json(
      { detail: 'Invalid username or password' },
      { status: 401 }
    );
  }),

  // Register API
  http.post(`${BASE_URL}/api/auth/register`, async ({ request }) => {
    const body = await request.json();
    if (body.username === 'existing_user') {
      return HttpResponse.json(
        { detail: 'Username already exists' },
        { status: 400 }
      );
    }
    return HttpResponse.json({
      id: 4,
      username: body.username,
      email: body.email,
      role_id: 2,
      role_name: 'User',
      is_active: true
    }, { status: 201 });
  }),

  // Users API
  http.get(`${BASE_URL}/api/users`, () => {
    return HttpResponse.json({
      users: mockUsers,
      total: mockUsers.length,
      page: 1,
      page_size: 20
    });
  }),

  http.get(`${BASE_URL}/api/users/:id`, ({ params }) => {
    const user = mockUsers.find(u => u.id === parseInt(params.id));
    if (!user) {
      return HttpResponse.json({ detail: 'User not found' }, { status: 404 });
    }
    return HttpResponse.json(user);
  }),

  http.post(`${BASE_URL}/api/users`, async ({ request }) => {
    const body = await request.json();
    const newUser = {
      id: mockUsers.length + 1,
      ...body,
      role_name: mockRoles.find(r => r.id === body.role_id)?.name || 'User',
      is_active: body.is_active ?? true,
      speaker_id: null,
      last_login: null,
      created_at: new Date().toISOString()
    };
    return HttpResponse.json(newUser, { status: 201 });
  }),

  http.patch(`${BASE_URL}/api/users/:id`, async ({ params, request }) => {
    const body = await request.json();
    const user = mockUsers.find(u => u.id === parseInt(params.id));
    if (!user) {
      return HttpResponse.json({ detail: 'User not found' }, { status: 404 });
    }
    return HttpResponse.json({
      ...user,
      ...body,
      role_name: body.role_id ? mockRoles.find(r => r.id === body.role_id)?.name : user.role_name
    });
  }),

  http.delete(`${BASE_URL}/api/users/:id`, ({ params }) => {
    const user = mockUsers.find(u => u.id === parseInt(params.id));
    if (!user) {
      return HttpResponse.json({ detail: 'User not found' }, { status: 404 });
    }
    return HttpResponse.json({ message: 'User deleted successfully' });
  }),

  // Speakers API
  http.get(`${BASE_URL}/api/speakers`, () => {
    return HttpResponse.json(mockSpeakers);
  }),

  // Health API
  http.get(`${BASE_URL}/health`, () => {
    return HttpResponse.json(mockHealth);
  }),

  // Chat Conversations API
  http.get(`${BASE_URL}/api/chat/conversations`, () => {
    return HttpResponse.json({
      conversations: mockConversations,
      total: mockConversations.length
    });
  }),

  http.get(`${BASE_URL}/api/chat/history/:sessionId`, ({ params }) => {
    const history = mockConversationHistory[params.sessionId];
    if (!history) {
      return HttpResponse.json({ messages: [] });
    }
    return HttpResponse.json({ messages: history });
  }),

  http.delete(`${BASE_URL}/api/chat/session/:sessionId`, ({ params }) => {
    const conv = mockConversations.find(c => c.session_id === params.sessionId);
    if (!conv) {
      return HttpResponse.json({ detail: 'Session not found' }, { status: 404 });
    }
    return HttpResponse.json({ message: 'Session deleted successfully' });
  })
];

// Export BASE_URL for use in tests that need to override handlers
export { BASE_URL, mockRoles, mockPermissions, mockUsers, mockSpeakers, mockHealth, mockConversations, mockConversationHistory, mockMcpStatus, mockMcpTools };

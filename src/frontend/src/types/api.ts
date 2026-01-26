/**
 * API response type definitions
 */

// Generic API response wrapper
export interface ApiResponse<T> {
  data: T;
  status: number;
}

// Pagination
export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

// Conversations list response
export interface ConversationsResponse {
  conversations: import('./chat').Conversation[];
  total: number;
}

// Chat history response
export interface ChatHistoryResponse {
  messages: import('./chat').ChatMessage[];
  session_id: string;
}

// Room types
export interface Room {
  id: number;
  name: string;
  ha_area_id?: string;
  created_at: string;
  updated_at: string;
  devices?: RoomDevice[];
}

export interface RoomDevice {
  id: string;
  device_id: string;
  device_type: import('./device').DeviceType;
  device_name?: string;
  room_id: number;
  is_stationary: boolean;
  ip_address?: string;
  last_seen?: string;
  is_online: boolean;
  capabilities: import('./device').DeviceCapabilities;
}

// Speaker types
export interface Speaker {
  id: number;
  name: string;
  user_id?: number;
  created_at: string;
  updated_at: string;
  embedding_count: number;
}

export interface SpeakerIdentifyResult {
  speaker_id: number | null;
  speaker_name: string | null;
  confidence: number;
  is_new: boolean;
}

// Home Assistant types
export interface HAEntity {
  entity_id: string;
  state: string;
  attributes: Record<string, unknown>;
  last_changed: string;
  last_updated: string;
  friendly_name?: string;
}

export interface HAArea {
  area_id: string;
  name: string;
  aliases?: string[];
}

// Satellite types
export interface Satellite {
  satellite_id: string;
  room: string;
  status: 'online' | 'offline';
  last_seen?: string;
  version?: string;
  update_available?: boolean;
  update_status?: 'idle' | 'downloading' | 'installing' | 'restarting' | 'failed';
  update_progress?: number;
  capabilities: {
    has_microphone: boolean;
    has_speaker: boolean;
    has_wakeword: boolean;
    has_led?: boolean;
    has_button?: boolean;
  };
  audio_levels?: {
    current: number;
    min: number;
    max: number;
    avg: number;
  };
}

// Health check response
export interface HealthResponse {
  status: 'healthy' | 'degraded' | 'unhealthy';
  version: string;
  services: {
    database: boolean;
    redis: boolean;
    ollama: boolean;
    homeassistant: boolean;
    frigate: boolean;
  };
}

// Error response
export interface ApiError {
  detail: string;
  status_code?: number;
}

// Auth types
export interface User {
  id: number;
  username: string;
  email?: string;
  is_active: boolean;
  role_id: number;
  role?: Role;
  speaker_id?: number;
  created_at: string;
  updated_at: string;
}

export interface Role {
  id: number;
  name: string;
  description?: string;
  permissions: string[];
  created_at: string;
  updated_at: string;
}

export interface LoginRequest {
  username: string;
  password: string;
}

export interface LoginResponse {
  access_token: string;
  refresh_token: string;
  token_type: 'bearer';
  user: User;
}

export interface RefreshTokenRequest {
  refresh_token: string;
}

export interface RefreshTokenResponse {
  access_token: string;
  token_type: 'bearer';
}

/**
 * Test configuration for frontend React tests
 *
 * The API base URL can be configured via environment variable:
 * - VITE_API_URL: Full URL to the backend API (default: http://localhost:8000)
 *
 * Example usage:
 *   VITE_API_URL=http://localhost:8080 npm test
 */

// Use Vite's import.meta.env for environment variables, with fallback
export const TEST_CONFIG = {
  // Base URL for API requests - configurable via VITE_API_URL environment variable
  API_BASE_URL: import.meta.env.VITE_API_URL || 'http://localhost:8000',
};

export default TEST_CONFIG;

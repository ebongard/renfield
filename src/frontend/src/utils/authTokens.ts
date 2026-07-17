// Shared localStorage keys for the JWT auth tokens.
//
// Defined here — NOT in AuthContext — so the axios request interceptor can read
// the access token without importing AuthContext (which imports apiClient, so
// that would be a circular import). Both AuthContext and utils/axios.ts import
// these constants.
export const ACCESS_TOKEN_KEY = 'renfield_access_token';
export const REFRESH_TOKEN_KEY = 'renfield_refresh_token';

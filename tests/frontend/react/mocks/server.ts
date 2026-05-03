import { setupServer, type SetupServerApi } from 'msw/node';
import { handlers } from './handlers';

// Setup mock server with default handlers.
// `setupServer` is fully typed by MSW; the explicit return-type annotation
// makes the public surface (server.listen / resetHandlers / close / use) visible
// to consumers without having to import the type from `msw/node`.
export const server: SetupServerApi = setupServer(...handlers);

// Full-page navigation to another origin (IdP authorize / end-session URL).
//
// One module so tests can replace it with `vi.mock`: jsdom's
// `window.location.assign` cannot be spied on ("Cannot redefine property").
export function leavePage(url: string): void {
  window.location.assign(url);
}

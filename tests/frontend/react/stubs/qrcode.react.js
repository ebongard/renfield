// Test-time stub for qrcode.react.
// The real library resolves React from src/frontend/node_modules, which is a
// different copy from tests/frontend/react/node_modules/react — duplicate
// React dispatchers null out each other's hooks. Stubbing here side-steps
// the whole duplication problem; tests assert on the surrounding
// role="img" aria-label container instead of the SVG payload.
export function QRCodeSVG() {
  return null;
}

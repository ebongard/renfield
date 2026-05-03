// Test-time stub for qrcode.react.
// The real library resolves React from src/frontend/node_modules, which is a
// different copy from tests/frontend/react/node_modules/react — duplicate
// React dispatchers null out each other's hooks. Stubbing here side-steps
// the whole duplication problem; tests assert on the surrounding
// role="img" aria-label container instead of the SVG payload.

// Real qrcode.react accepts ~10 props (value, size, level, bgColor, fgColor,
// includeMargin, imageSettings, …); the test only needs the surrounding
// container so we render nothing and accept anything.
export interface QRCodeSVGProps {
  value?: string;
  size?: number;
  level?: 'L' | 'M' | 'Q' | 'H';
  bgColor?: string;
  fgColor?: string;
  includeMargin?: boolean;
  title?: string;
  marginSize?: number;
}

export function QRCodeSVG(_props?: QRCodeSVGProps): null {
  return null;
}

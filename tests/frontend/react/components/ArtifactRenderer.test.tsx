/**
 * ArtifactRenderer — the chat artifacts (Lane A) dispatcher, sub-renderers,
 * fallback, streaming-state resolution, and the security-critical escape /
 * injection negative suite. German is the test default.
 *
 * a11y is asserted via semantic DOM (table headers w/ scope, list semantics,
 * region aria-label) rather than jest-axe (not a project dependency), matching
 * the existing component-test idiom (e.g. SourceChips.test.tsx).
 */
import { describe, it, expect, vi } from 'vitest';
import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ArtifactRenderer from '../../../../src/frontend/src/components/chat/artifacts/ArtifactRenderer';
import { renderWithRouter } from '../test-utils';

function renderArtifact(artifact: unknown, props: { loading?: boolean; finalized?: boolean } = {}) {
  return renderWithRouter(
    <ArtifactRenderer artifact={artifact} finalized={props.finalized ?? true} loading={props.loading} />,
  );
}

const XSS = '<img src=x onerror=alert(1)>';

describe('ArtifactRenderer — per-kind render', () => {
  it('renders a table with semantic headers (scope=col) + cells', () => {
    renderArtifact({
      id: 'a1', kind: 'table', title: 'Plan',
      data: { columns: ['Tag', 'Gericht'], rows: [['Mo', 'Pasta'], ['Di', 'Reis']] },
    });
    const table = screen.getByRole('table');
    const headers = within(table).getAllByRole('columnheader');
    expect(headers.map((h) => h.textContent)).toEqual(['Tag', 'Gericht']);
    headers.forEach((h) => expect(h).toHaveAttribute('scope', 'col'));
    expect(within(table).getByText('Pasta')).toBeInTheDocument();
    expect(within(table).getByText('Reis')).toBeInTheDocument();
  });

  it('renders an unordered list with li semantics', () => {
    renderArtifact({ id: 'a2', kind: 'list', data: { items: ['Milch', 'Brot'] } });
    const items = screen.getAllByRole('listitem');
    expect(items.map((i) => i.textContent)).toEqual(['Milch', 'Brot']);
  });

  it('renders an ordered list as <ol>', () => {
    const { container } = renderArtifact({ id: 'a2o', kind: 'list', data: { ordered: true, items: ['eins'] } });
    expect(container.querySelector('ol')).toBeInTheDocument();
    expect(container.querySelector('ul')).not.toBeInTheDocument();
  });

  it('renders keyvalue pairs as key + value', () => {
    renderArtifact({ id: 'a3', kind: 'keyvalue', data: { pairs: [{ key: 'Status', value: 'OK' }] } });
    expect(screen.getByText('Status')).toBeInTheDocument();
    expect(screen.getByText('OK')).toBeInTheDocument();
  });

  it('renders a chart as an SVG with role=img + aria summary, no overflow risk', () => {
    const { container } = renderArtifact({
      id: 'a4', kind: 'chart', title: 'Umsatz',
      data: { chartType: 'line', series: [{ label: 'A', points: [{ x: 0, y: 1 }, { x: 1, y: 3 }] }] },
    });
    const svg = container.querySelector('svg[role="img"]');
    expect(svg).toBeInTheDocument();
    expect(svg).toHaveAttribute('aria-label');
    // viewBox is finite + fixed → never overflows the bubble.
    expect(svg?.getAttribute('viewBox')).toMatch(/^0 0 \d+ \d+$/);
  });

  it('multi-series chart shows a legend AND direct end-of-line labels (CVD non-color channel)', () => {
    const { container } = renderArtifact({
      id: 'a5', kind: 'chart',
      data: { chartType: 'line', series: [
        { label: 'Nord', points: [{ x: 0, y: 1 }] },
        { label: 'Süd', points: [{ x: 0, y: 2 }] },
      ] },
    });
    // end-of-line <text> labels (non-color channel)
    const texts = Array.from(container.querySelectorAll('svg text')).map((t) => t.textContent);
    expect(texts).toContain('Nord');
    expect(texts).toContain('Süd');
    // legend entries (only when >1 series)
    expect(screen.getAllByRole('listitem').length).toBeGreaterThanOrEqual(2);
  });

  it('renders a weather widget: current temp/condition + a forecast row', () => {
    renderArtifact({
      id: 'aw', kind: 'weather', title: 'Berlin',
      data: {
        location: 'Berlin',
        current: { temp: 18.4, unit: '°C', code: 3, condition: 'Bedeckt', humidity: 72, high: 20, low: 11 },
        forecast: [
          { date: '2026-06-21', code: 1, high: 23, low: 12, condition: 'Heiter' },
          { date: '2026-06-22', code: 80, high: 19, low: 13, condition: 'Schauer', precipChance: 60 },
        ],
      },
    });
    // Current temp is rounded + carries the unit; condition shown.
    expect(screen.getByText('18°C')).toBeInTheDocument();
    expect(screen.getByText('Bedeckt')).toBeInTheDocument();
    // Forecast highs render (rounded).
    expect(screen.getByText('23°C')).toBeInTheDocument();
    // Precip chance shown for the rainy day.
    expect(screen.getByText('60%')).toBeInTheDocument();
  });

  it('weather widget renders the condition text as inert escaped text', () => {
    renderArtifact({
      id: 'awx', kind: 'weather',
      data: { location: 'X', current: { temp: 1, unit: '°C', code: 0, condition: XSS } },
    });
    expect(screen.getByText(XSS)).toBeInTheDocument();
  });

  it('renders a device_control widget: switches for light/switch + a run button for scene', async () => {
    const onDeviceAction = vi.fn().mockResolvedValue({ success: true, state: 'off' });
    renderWithRouter(
      <ArtifactRenderer
        finalized
        onDeviceAction={onDeviceAction}
        artifact={{
          id: 'adc', kind: 'device_control', title: 'Gerätesteuerung',
          data: { devices: [
            { entity_id: 'light.wz', domain: 'light', name: 'Licht WZ', state: 'on' },
            { entity_id: 'scene.abend', domain: 'scene', name: 'Abend', state: 'x' },
          ] },
        }}
      />,
    );
    // A light renders a switch reflecting its on state.
    const sw = screen.getByRole('switch', { name: 'Licht WZ' });
    expect(sw).toHaveAttribute('aria-checked', 'true');
    // A scene renders a run button.
    const runBtn = screen.getByRole('button', { name: /Szene Abend ausführen|Run scene Abend/i });
    expect(runBtn).toBeInTheDocument();

    // Clicking the switch fires a toggle action; the scene button an activate.
    await userEvent.click(sw);
    expect(onDeviceAction).toHaveBeenCalledWith('light.wz', 'toggle');
    await userEvent.click(runBtn);
    expect(onDeviceAction).toHaveBeenCalledWith('scene.abend', 'activate');
  });

  it('device_control controls are disabled without an action handler', () => {
    renderArtifact({
      id: 'adc2', kind: 'device_control',
      data: { devices: [{ entity_id: 'switch.x', domain: 'switch', name: 'Schalter', state: 'off' }] },
    });
    expect(screen.getByRole('switch', { name: 'Schalter' })).toBeDisabled();
  });

  it('region carries the generated aria-label + the "generiert" affordance', () => {
    renderArtifact({ id: 'a6', kind: 'list', title: 'Einkauf', data: { items: ['x'] } });
    expect(screen.getByRole('region', { name: /Generiertes Artefakt: Einkauf/ })).toBeInTheDocument();
    expect(screen.getByText('generiert')).toBeInTheDocument();
  });

  it('region is aria-live="off" to avoid double-announce in the live thread', () => {
    renderArtifact({ id: 'a6b', kind: 'list', data: { items: ['x'] } });
    expect(screen.getByRole('region')).toHaveAttribute('aria-live', 'off');
  });
});

describe('ArtifactRenderer — escape / injection negative suite (CRITICAL)', () => {
  it('renders HTML payloads in table cells as inert escaped text, never live elements', () => {
    const { container } = renderArtifact({
      id: 'x1', kind: 'table',
      data: { columns: [XSS, '</th><script>a</script>'], rows: [['</td><script>b</script>', XSS]] },
    });
    // No live script / img injected anywhere.
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('img')).toBeNull();
    // The payload survives as visible text (escaped).
    expect(screen.getAllByText(XSS).length).toBeGreaterThan(0);
  });

  it('renders HTML in list items as inert text', () => {
    const { container } = renderArtifact({ id: 'x2', kind: 'list', data: { items: [XSS, '<svg onload=alert(1)>'] } });
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('img')).toBeNull();
    // The `<svg onload>` payload must NOT become a live element; the list item
    // shows it as escaped text. (The only real svg in the DOM is the affordance
    // icon, which has aria-hidden — the payload is not parsed into markup.)
    expect(container.querySelector('svg[onload]')).toBeNull();
    expect(screen.getByText(XSS)).toBeInTheDocument();
    // the payload text is present verbatim (escaped), proving it's inert
    expect(screen.getByText('<svg onload=alert(1)>')).toBeInTheDocument();
  });

  it('renders HTML in keyvalue key/value as inert text', () => {
    const { container } = renderArtifact({
      id: 'x3', kind: 'keyvalue',
      data: { pairs: [{ key: XSS, value: '"><script>alert(1)</script>' }] },
    });
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('img')).toBeNull();
    expect(screen.getByText(XSS)).toBeInTheDocument();
  });

  it('renders HTML in chart series labels as inert text (no script in the SVG)', () => {
    const { container } = renderArtifact({
      id: 'x4', kind: 'chart',
      data: { chartType: 'line', series: [
        { label: XSS, points: [{ x: 0, y: 1 }] },
        { label: '<script>1</script>', points: [{ x: 0, y: 2 }] },
      ] },
    });
    expect(container.querySelector('script')).toBeNull();
    // label rendered as SVG <text> content (escaped), not parsed markup.
    const texts = Array.from(container.querySelectorAll('svg text')).map((t) => t.textContent);
    expect(texts).toContain(XSS);
  });

  it('a javascript:/data: URL in a value is rendered as text, NOT as a clickable link', () => {
    const { container } = renderArtifact({
      id: 'x5', kind: 'keyvalue',
      data: { pairs: [
        { key: 'evil', value: 'javascript:alert(1)' },
        { key: 'evil2', value: 'data:text/html,<script>1</script>' },
      ] },
    });
    // v1 renders text only — no anchors at all, so no disallowed-scheme href can exist.
    expect(container.querySelectorAll('a').length).toBe(0);
    expect(screen.getByText('javascript:alert(1)')).toBeInTheDocument();
  });
});

describe('ArtifactRenderer — fallback (fail closed)', () => {
  it('a schema-invalid payload renders the escaped code-block fallback, thread survives', () => {
    const { container } = renderArtifact({ id: 'bad', kind: 'table', data: { columns: 'not-an-array', rows: 5 } });
    expect(screen.getByText(/konnte nicht als Artefakt dargestellt werden/)).toBeInTheDocument();
    // The fallback itself is escaped (a <pre><code>), no injection through it.
    expect(container.querySelector('pre code')).toBeInTheDocument();
    expect(container.querySelector('script')).toBeNull();
  });

  it('an unknown kind falls back (zod union rejects it)', () => {
    renderArtifact({ id: 'u', kind: 'html', data: { markup: '<b>x</b>' } });
    expect(screen.getByText(/konnte nicht als Artefakt dargestellt werden/)).toBeInTheDocument();
  });

  it('the fallback escapes an injected payload inside the raw JSON', () => {
    const { container } = renderArtifact({ id: 'f', kind: 'nope', evil: '</code><script>alert(1)</script>' });
    expect(container.querySelector('script')).toBeNull();
    // the payload text is present, escaped, inside the code block
    expect(container.querySelector('pre code')?.textContent).toContain('<script>');
  });
});

describe('ArtifactRenderer — chart numeric validation (viewBox DoS)', () => {
  it('rejects NaN coordinates → fallback (schema), no SVG emitted', () => {
    const { container } = renderArtifact({
      id: 'n', kind: 'chart',
      data: { chartType: 'line', series: [{ label: 's', points: [{ x: 0, y: Number.NaN }] }] },
    });
    // No CHART svg (role=img); the only svg is the affordance icon.
    expect(container.querySelector('svg[role="img"]')).toBeNull();
    expect(screen.getByText(/konnte nicht als Artefakt dargestellt werden/)).toBeInTheDocument();
  });

  it('rejects Infinity coordinates → fallback', () => {
    const { container } = renderArtifact({
      id: 'i', kind: 'chart',
      data: { chartType: 'bar', series: [{ label: 's', points: [{ x: 0, y: Number.POSITIVE_INFINITY }] }] },
    });
    expect(container.querySelector('svg[role="img"]')).toBeNull();
    expect(screen.getByText(/konnte nicht als Artefakt dargestellt werden/)).toBeInTheDocument();
  });
});

describe('ArtifactRenderer — streaming states', () => {
  it('shows a skeleton while loading + not finalized (no fallback, no body)', () => {
    renderArtifact(
      { id: 's', kind: 'table', partial: true, data: { columns: ['A'], rows: [['1']] } },
      { loading: true, finalized: false },
    );
    expect(screen.getByLabelText(/Artefakt wird geladen/)).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    expect(screen.queryByText(/konnte nicht/)).not.toBeInTheDocument();
  });

  it('a partial that never finalized resolves to the fallback after the turn ends', () => {
    renderArtifact(
      { id: 'p', kind: 'table', partial: true, data: { columns: ['A'], rows: [['1']] } },
      { loading: false, finalized: true },
    );
    expect(screen.getByText(/konnte nicht als Artefakt dargestellt werden/)).toBeInTheDocument();
  });

  it('a finalized non-partial artifact renders normally', () => {
    renderArtifact({ id: 'd', kind: 'table', partial: false, data: { columns: ['A'], rows: [['1']] } });
    expect(screen.getByRole('table')).toBeInTheDocument();
  });
});

describe('ArtifactRenderer — empty state (distinct from fallback)', () => {
  it('0-row table → "Keine Zeilen", not the error fallback', () => {
    renderArtifact({ id: 'e1', kind: 'table', data: { columns: ['A'], rows: [] } });
    expect(screen.getByText('Keine Zeilen')).toBeInTheDocument();
    expect(screen.queryByText(/konnte nicht/)).not.toBeInTheDocument();
  });

  it('0-item list → "Keine Einträge"', () => {
    renderArtifact({ id: 'e2', kind: 'list', data: { items: [] } });
    expect(screen.getByText('Keine Einträge')).toBeInTheDocument();
  });

  it('0-point chart → "Keine Datenpunkte"', () => {
    renderArtifact({ id: 'e3', kind: 'chart', data: { chartType: 'bar', series: [] } });
    expect(screen.getByText('Keine Datenpunkte')).toBeInTheDocument();
  });
});

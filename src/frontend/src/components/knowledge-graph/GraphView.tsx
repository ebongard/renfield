/**
 * GraphView — Wissensgraph 3D, single unified scene.
 *
 * Two modes driven by the ?focus= URL param:
 *
 *   - Corpus mode (no ?focus=): renders the connected-component
 *     clusters returned by /api/wissensbasis/graph. Translucent
 *     spheres with hub entities orbiting each.
 *
 *   - Focus mode (?focus=<entity_id>): renders the entity's
 *     neighborhood. Focus entity at center, hop1 entities orbiting
 *     close, hop2 entities in an outer translucent shell. Data from
 *     /api/wissensbasis/focus.
 *
 * Search overlay (top-left) drives the camera in either mode: type a
 * name, pick a suggestion, the URL ?focus= updates and the scene
 * re-renders focused on that entity. Click a hub in the scene → same
 * URL-param change → same re-render. No page navigation, no flat-list
 * handoff.
 *
 * Replaces the prior 2D force layout AND the separate Wissensbasis
 * flat-chip A4 panel per the user's "one continuous 3D experience"
 * framing (Option 3 in D23, 2026-05-12).
 *
 * Render budget: corpus mode = ~200 entities + ~50 relations in
 * current prod; focus mode = single entity + hop1 (≤30) + hop2 (≤30).
 * Either way trivially 60fps. Scene uses StandardMaterial only on
 * hubs + cluster cores; everything else is BasicMaterial / wireframe.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useSearchParams } from 'react-router';
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

import apiClient from '../../utils/axios';
import type {
  FocusEntity,
  FocusNeighborhood,
  SearchHit,
} from '../../api/resources/wissensbasis';

interface Hub {
  entity_id: string;
  name: string;
  entity_type: string;
  mention_count: number;
}

interface Cluster {
  id: string;
  label: string;
  sub_label: string;
  entity_count: number;
  hubs: Hub[];
  color_seed: number;
  namesake_entity_id: string | null;
}

interface GraphResponse {
  clusters: Cluster[];
  total_entities: number;
  total_relations: number;
  truncated: boolean;
}

const PALETTE: number[] = [
  0xe63e54, 0x06b6d4, 0xa78bfa, 0xeab308, 0xf97316, 0x22c55e,
];
const pickColor = (seed: number) => PALETTE[seed % PALETTE.length];

// Stable debounce — keeps each keystroke from firing /search.
function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setV(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return v;
}

export default function GraphView() {
  const { t } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();
  const focusId = searchParams.get('focus') || '';

  const rootRef = useRef<HTMLDivElement>(null);
  const labelsRef = useRef<HTMLDivElement>(null);

  const [corpus, setCorpus] = useState<GraphResponse | null>(null);
  const [focusData, setFocusData] = useState<FocusNeighborhood | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Mode selector. Fetches the appropriate endpoint; clears the other.
  useEffect(() => {
    let cancelled = false;
    setLoadError(null);
    if (focusId) {
      apiClient.get<FocusNeighborhood>('/api/wissensbasis/focus', {
        params: { entity_id: focusId, hops: 2 },
      })
        .then(res => { if (!cancelled) { setFocusData(res.data); setCorpus(null); } })
        .catch(err => { if (!cancelled) setLoadError(err?.message || String(err)); });
    } else {
      apiClient.get<GraphResponse>('/api/wissensbasis/graph')
        .then(res => { if (!cancelled) { setCorpus(res.data); setFocusData(null); } })
        .catch(err => { if (!cancelled) setLoadError(err?.message || String(err)); });
    }
    return () => { cancelled = true; };
  }, [focusId]);

  // Three.js scene lifecycle.
  useEffect(() => {
    const root = rootRef.current;
    const labelsLayer: HTMLDivElement | null = labelsRef.current;
    if (!root || !labelsLayer) return;
    if (!corpus && !focusData) return;

    const scene = new THREE.Scene();
    scene.fog = new THREE.FogExp2(0x0a0f1c, 0.018);

    const W = () => root.clientWidth;
    const H = () => root.clientHeight;
    const camera = new THREE.PerspectiveCamera(45, W() / H(), 0.1, 1000);
    camera.position.set(0, 14, 36);

    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(window.devicePixelRatio);
    renderer.setSize(W(), H());
    renderer.setClearColor(0x0a0f1c, 1);
    root.appendChild(renderer.domElement);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.minDistance = 6;
    controls.maxDistance = 80;

    scene.add(new THREE.AmbientLight(0x6080a0, 0.55));
    const key = new THREE.DirectionalLight(0xffffff, 0.6);
    key.position.set(8, 20, 14);
    scene.add(key);
    const rim = new THREE.DirectionalLight(0xe63e54, 0.18);
    rim.position.set(-12, -8, -16);
    scene.add(rim);

    const plane = new THREE.Mesh(
      new THREE.PlaneGeometry(80, 80, 24, 24),
      new THREE.MeshBasicMaterial({ color: 0x1a2540, wireframe: true, transparent: true, opacity: 0.18 }),
    );
    plane.rotation.x = -Math.PI / 2;
    plane.position.y = -10;
    scene.add(plane);

    // Track all labelable things (cluster spheres OR focus entities)
    // for the 2D label overlay + raycaster targets that drive
    // click-to-refocus.
    //
    // `tier` controls label styling: 'primary' (cluster center / focus
    // entity) shows full size; 'secondary' (hubs / hop1 / hop2) is
    // smaller, fades with distance so the center label stays readable.
    // `object` is read each frame for the world position so orbiting
    // hop1 nodes carry their labels.
    const labeled: Array<{
      object: THREE.Object3D;
      name: string;
      sub?: string;
      yOffset: number;
      tier: 'primary' | 'secondary';
      entityId?: string;
    }> = [];
    const clickable: Array<{ mesh: THREE.Object3D; entityId: string }> = [];

    if (corpus) {
      buildCorpusScene(scene, corpus, labeled, clickable);
    } else if (focusData) {
      buildFocusScene(scene, focusData, labeled, clickable);
    }

    const tmpWorld = new THREE.Vector3();
    function updateLabels() {
      if (!labelsLayer) return;
      while (labelsLayer.firstChild) labelsLayer.removeChild(labelsLayer.firstChild);
      const camPos = camera.position;
      for (const item of labeled) {
        item.object.getWorldPosition(tmpWorld);
        const distance = tmpWorld.distanceTo(camPos);
        tmpWorld.y += item.yOffset;
        const v = tmpWorld.clone().project(camera);
        if (v.z > 1) continue;
        const x = (v.x + 1) * 0.5 * W();
        const y = (1 - (v.y + 1) * 0.5) * H();

        // Secondary labels fade gently with distance (orbit-controls
        // maxDistance is 80) so far-back nodes don't shout louder than
        // close ones, but everything stays readable in the default
        // camera frame. Primary labels stay fully opaque.
        let opacity = 1;
        if (item.tier === 'secondary') {
          opacity = Math.max(0.35, Math.min(1, 1.15 - (distance - 18) / 50));
        }

        const div = document.createElement('div');
        // Labels that point to an entity are clickable (the user
        // naturally aims at the caption, not the small sphere).
        // Labels with no entityId stay click-transparent so they
        // don't block scene drag/zoom.
        const clickableLabel = !!item.entityId;
        div.className = clickableLabel
          ? 'absolute whitespace-nowrap cursor-pointer select-none'
          : 'pointer-events-none absolute whitespace-nowrap';
        div.style.left = `${x}px`;
        div.style.top = `${y}px`;
        div.style.transform = 'translate(-50%, -100%)';
        div.style.textShadow = '0 1px 2px rgba(0,0,0,0.95), 0 0 4px rgba(0,0,0,0.85)';
        div.style.opacity = String(opacity);
        if (item.tier === 'secondary') {
          // Pill background so labels stay readable when they overlap.
          div.style.padding = '1px 5px';
          div.style.background = 'rgba(10,15,28,0.55)';
          div.style.borderRadius = '3px';
        }
        if (clickableLabel) {
          const eid = item.entityId!;
          div.addEventListener('click', (e) => {
            e.stopPropagation();
            setSearchParams((prev) => {
              const next = new URLSearchParams(prev);
              next.set('focus', eid);
              return next;
            });
          });
        }

        const name = document.createElement('div');
        if (item.tier === 'primary') {
          name.className = 'font-semibold text-white text-sm';
          name.style.fontFamily = 'Cormorant, Georgia, serif';
        } else {
          name.className = 'font-medium text-white/90 text-[11px]';
        }
        // Truncate very long names so neighboring labels don't overlap.
        const maxLen = item.tier === 'primary' ? 48 : 24;
        name.textContent = item.name.length > maxLen
          ? item.name.slice(0, maxLen - 1) + '…'
          : item.name;
        div.appendChild(name);

        if (item.sub) {
          const sub = document.createElement('div');
          sub.className = item.tier === 'primary'
            ? 'text-[10px] font-normal text-gray-400'
            : 'text-[9px] font-normal text-gray-400/80';
          sub.textContent = item.sub;
          div.appendChild(sub);
        }
        labelsLayer.appendChild(div);
      }
    }

    // Hover + click raycasting.
    const raycaster = new THREE.Raycaster();
    const pointer = new THREE.Vector2();
    function onMove(e: MouseEvent) {
      const rect = renderer.domElement.getBoundingClientRect();
      pointer.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
      pointer.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
    }
    function onClick() {
      raycaster.setFromCamera(pointer, camera);
      const meshes = clickable.map(c => c.mesh);
      const hits = raycaster.intersectObjects(meshes, true);
      for (const h of hits) {
        // Walk up to find an object with .userData.entityId (hubs nest inside groups).
        let obj: THREE.Object3D | null = h.object;
        while (obj && !(obj.userData as { entityId?: string }).entityId) {
          obj = obj.parent;
        }
        const eid = (obj?.userData as { entityId?: string })?.entityId;
        if (eid) {
          // Stay in scene. Just bump the URL param; the mode effect
          // refetches and the scene rebuilds on the next pass.
          setSearchParams((prev) => {
            const next = new URLSearchParams(prev);
            next.set('focus', eid);
            return next;
          });
          return;
        }
      }
    }
    renderer.domElement.addEventListener('mousemove', onMove);
    renderer.domElement.addEventListener('click', onClick);

    function onResize() {
      camera.aspect = W() / H();
      camera.updateProjectionMatrix();
      renderer.setSize(W(), H());
    }
    window.addEventListener('resize', onResize);

    let rafId = 0;
    let theta = 0;
    function loop() {
      controls.update();
      theta += 0.003;
      // Orbit any group whose userData asks for it (focus-mode hop1 rings).
      scene.traverse(obj => {
        const ud = obj.userData as { orbit?: boolean };
        if (ud.orbit) obj.rotation.y = theta;
      });
      renderer.render(scene, camera);
      updateLabels();
      rafId = requestAnimationFrame(loop);
    }
    loop();

    return () => {
      cancelAnimationFrame(rafId);
      window.removeEventListener('resize', onResize);
      renderer.domElement.removeEventListener('mousemove', onMove);
      renderer.domElement.removeEventListener('click', onClick);
      controls.dispose();
      renderer.dispose();
      if (root.contains(renderer.domElement)) root.removeChild(renderer.domElement);
    };
  }, [corpus, focusData, setSearchParams]);

  function setFocus(entityId: string | null) {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      if (entityId) next.set('focus', entityId);
      else next.delete('focus');
      return next;
    });
  }

  if (loadError) {
    return (
      <div role="alert" className="text-xs text-red-700 dark:text-red-300 px-3 py-2 bg-red-50 dark:bg-red-900/20 rounded">
        {t('knowledgeGraph.graph.loadError', 'Could not load graph: {{err}}', { err: loadError })}
      </div>
    );
  }

  return (
    <div className="relative w-full" style={{ height: '70vh', minHeight: 480 }}>
      <div ref={rootRef} className="absolute inset-0 rounded-lg overflow-hidden bg-[#0a0f1c]" />
      <div ref={labelsRef} className="pointer-events-none absolute inset-0" />

      <SearchOverlay onPick={(eid) => setFocus(eid)} />

      {focusId && (
        <button
          type="button"
          onClick={() => setFocus(null)}
          className="absolute top-3 right-3 text-[11px] px-2 py-1 rounded
            bg-black/50 text-white/80 hover:bg-black/70 hover:text-white"
          title={t('knowledgeGraph.graph.backToCorpus', 'Back to corpus view')}
        >
          {t('knowledgeGraph.graph.backToCorpus', '← Corpus')}
        </button>
      )}

      <div className="pointer-events-none absolute left-3 bottom-3 text-[10px] text-gray-500 bg-black/40 px-2 py-1 rounded">
        {t('knowledgeGraph.graph.hint', 'Drag to orbit · scroll to zoom · click a hub to focus')}
      </div>

      {corpus && !focusId && (
        <div className="pointer-events-none absolute right-3 bottom-3 text-[10px] text-gray-500 bg-black/40 px-2 py-1 rounded">
          {corpus.total_entities} entities · {corpus.clusters.length} clusters
          {corpus.truncated && ' · truncated'}
        </div>
      )}
      {focusData && (
        <div className="pointer-events-none absolute right-3 bottom-3 text-[10px] text-gray-500 bg-black/40 px-2 py-1 rounded">
          {focusData.focus.display_name} · {focusData.hop1.length} hop1 · {focusData.hop2.length} hop2
        </div>
      )}
    </div>
  );
}

// =========================================================================
// Scene builders
// =========================================================================

type Labeled = {
  object: THREE.Object3D;
  name: string;
  sub?: string;
  yOffset: number;
  tier: 'primary' | 'secondary';
  entityId?: string;
};

function buildCorpusScene(
  scene: THREE.Scene,
  data: GraphResponse,
  labeled: Labeled[],
  clickable: Array<{ mesh: THREE.Object3D; entityId: string }>,
) {
  const N = data.clusters.length;
  const ringRadius = N <= 1 ? 0 : Math.min(14, 6 + N * 1.5);

  data.clusters.forEach((c, i) => {
    const angle = (i / Math.max(N, 1)) * Math.PI * 2;
    const pos = new THREE.Vector3(
      Math.cos(angle) * ringRadius,
      (i % 2 === 0 ? 1 : -1) * (i % 3) * 0.8,
      Math.sin(angle) * ringRadius,
    );
    const color = pickColor(c.color_seed);
    const radius = Math.max(2.0, Math.min(5.0, 1.4 + Math.sqrt(c.entity_count)));

    const group = new THREE.Group();
    group.position.copy(pos);
    group.userData = { cluster: c };

    // Translucent body — keep clickable so users can grab the whole
    // cluster from anywhere on the sphere, not just the small core.
    const body = new THREE.Mesh(
      new THREE.SphereGeometry(radius, 32, 32),
      new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.10, depthWrite: false }),
    );
    group.add(body);
    group.add(new THREE.Mesh(
      new THREE.SphereGeometry(radius * 1.02, 24, 16),
      new THREE.MeshBasicMaterial({ color, wireframe: true, transparent: true, opacity: 0.32 }),
    ));
    const core = new THREE.Mesh(
      new THREE.SphereGeometry(0.5, 16, 16),
      new THREE.MeshStandardMaterial({ color, emissive: color, emissiveIntensity: 0.45, roughness: 0.4 }),
    );
    group.add(core);

    // Cluster centre is clickable when we have a namesake. Click
    // anywhere on the body or core → focus on the cluster's namesake
    // entity. Loose-ends has no namesake so the cluster stays
    // non-clickable (its individual hubs remain clickable instead).
    if (c.namesake_entity_id) {
      const eid = c.namesake_entity_id;
      body.userData = { entityId: eid };
      core.userData = { entityId: eid };
      clickable.push({ mesh: body, entityId: eid });
      clickable.push({ mesh: core, entityId: eid });
    }

    c.hubs.forEach((hub, hi) => {
      const theta = (hi / Math.max(c.hubs.length, 1)) * Math.PI * 2;
      const phi = Math.PI / 2 + ((hi % 2 === 0 ? 1 : -1) * 0.3);
      const r = radius * 0.75;
      const hubMesh = new THREE.Mesh(
        new THREE.SphereGeometry(0.35 + (hi === 0 ? 0.18 : 0), 12, 12),
        new THREE.MeshStandardMaterial({ color, emissive: color, emissiveIntensity: 0.5, roughness: 0.35 }),
      );
      hubMesh.position.set(
        r * Math.sin(phi) * Math.cos(theta),
        r * Math.cos(phi),
        r * Math.sin(phi) * Math.sin(theta),
      );
      hubMesh.userData = { entityId: hub.entity_id, hub };
      group.add(hubMesh);
      clickable.push({ mesh: hubMesh, entityId: hub.entity_id });
      labeled.push({
        object: hubMesh,
        name: hub.name,
        sub: hub.entity_type,
        yOffset: 0.7,
        tier: 'secondary',
        entityId: hub.entity_id,
      });
    });

    scene.add(group);
    labeled.push({
      object: group,
      name: c.label,
      sub: c.sub_label,
      yOffset: radius * 1.1,
      tier: 'primary',
      entityId: c.namesake_entity_id || undefined,
    });
  });
}

function buildFocusScene(
  scene: THREE.Scene,
  data: FocusNeighborhood,
  labeled: Labeled[],
  clickable: Array<{ mesh: THREE.Object3D; entityId: string }>,
) {
  const focusColor = 0xe63e54;
  const hop1Color = 0x06b6d4;
  const hop2Color = 0xa78bfa;

  // Focus entity — large central emissive sphere.
  const focusMesh = new THREE.Mesh(
    new THREE.SphereGeometry(1.4, 32, 32),
    new THREE.MeshStandardMaterial({
      color: focusColor, emissive: focusColor, emissiveIntensity: 0.6, roughness: 0.3,
    }),
  );
  focusMesh.userData = { entityId: data.focus.entity_id, focusEntity: data.focus };
  scene.add(focusMesh);
  // Atmospheric shell.
  scene.add(new THREE.Mesh(
    new THREE.SphereGeometry(2.5, 32, 32),
    new THREE.MeshBasicMaterial({
      color: focusColor, transparent: true, opacity: 0.08, depthWrite: false,
    }),
  ));
  labeled.push({
    object: focusMesh,
    name: data.focus.display_name,
    sub: data.focus.entity_type,
    yOffset: 3.2,
    tier: 'primary',
    // The focus label is the entity you're already on — no need to
    // re-focus on click; leaving entityId unset keeps it
    // click-transparent so the user can drag-orbit through the centre.
  });

  // hop1 ring — orbiting hubs at radius ~7. Group so we can lazy-orbit.
  const hop1Group = new THREE.Group();
  hop1Group.userData = { orbit: true };
  scene.add(hop1Group);
  data.hop1.forEach((e, i) => {
    const theta = (i / Math.max(data.hop1.length, 1)) * Math.PI * 2;
    const r = 7;
    const mesh = makeHubMesh(hop1Color, 0.42);
    mesh.position.set(Math.cos(theta) * r, Math.sin(theta * 2) * 0.6, Math.sin(theta) * r);
    mesh.userData = { entityId: e.entity_id, entity: e };
    hop1Group.add(mesh);
    clickable.push({ mesh, entityId: e.entity_id });
    labeled.push({
      object: mesh,
      name: e.display_name,
      sub: e.entity_type,
      yOffset: 0.8,
      tier: 'secondary',
      entityId: e.entity_id,
    });

    // Edge from focus to hop1 (faint line).
    const lineGeom = new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(0, 0, 0), mesh.position.clone(),
    ]);
    scene.add(new THREE.Line(
      lineGeom,
      new THREE.LineBasicMaterial({ color: hop1Color, transparent: true, opacity: 0.35 }),
    ));
  });

  // hop2 outer shell — distributed on a larger sphere surface.
  data.hop2.forEach((e, i) => {
    const n = Math.max(data.hop2.length, 1);
    // Fibonacci sphere distribution for even coverage.
    const golden = Math.PI * (3 - Math.sqrt(5));
    const y = 1 - (i / Math.max(n - 1, 1)) * 2;
    const r = Math.sqrt(1 - y * y);
    const theta = golden * i;
    const R = 13;
    const mesh = makeHubMesh(hop2Color, 0.3);
    mesh.position.set(Math.cos(theta) * r * R, y * R, Math.sin(theta) * r * R);
    mesh.userData = { entityId: e.entity_id, entity: e };
    scene.add(mesh);
    clickable.push({ mesh, entityId: e.entity_id });
    labeled.push({
      object: mesh,
      name: e.display_name,
      sub: e.entity_type,
      yOffset: 0.6,
      tier: 'secondary',
      entityId: e.entity_id,
    });
  });

  // Wireframe outer-shell hint (the "2 HOPS" boundary from the A4 mock).
  scene.add(new THREE.Mesh(
    new THREE.SphereGeometry(13, 32, 16),
    new THREE.MeshBasicMaterial({
      color: 0x475569, wireframe: true, transparent: true, opacity: 0.10,
    }),
  ));
}

function makeHubMesh(color: number, radius: number): THREE.Mesh {
  return new THREE.Mesh(
    new THREE.SphereGeometry(radius, 12, 12),
    new THREE.MeshStandardMaterial({
      color, emissive: color, emissiveIntensity: 0.5, roughness: 0.35,
    }),
  );
}

// =========================================================================
// Search overlay
// =========================================================================

function SearchOverlay({ onPick }: { onPick: (entityId: string) => void }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState('');
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [activeIndex, setActiveIndex] = useState(0);
  const debouncedQ = useDebounced(q, 180);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  useEffect(() => {
    let cancelled = false;
    if (!debouncedQ.trim()) {
      setHits([]);
      return;
    }
    apiClient.get<{ items: SearchHit[] }>('/api/wissensbasis/search', {
      params: { q: debouncedQ },
    })
      .then(res => { if (!cancelled) setHits(res.data.items || []); })
      .catch(() => { if (!cancelled) setHits([]); });
    return () => { cancelled = true; };
  }, [debouncedQ]);

  useEffect(() => { setActiveIndex(0); }, [hits.length]);

  function onKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'ArrowDown' && hits.length > 0) {
      e.preventDefault();
      setActiveIndex(i => Math.min(i + 1, hits.length - 1));
    } else if (e.key === 'ArrowUp' && hits.length > 0) {
      e.preventDefault();
      setActiveIndex(i => Math.max(i - 1, 0));
    } else if (e.key === 'Enter' && hits.length > 0) {
      e.preventDefault();
      onPick(hits[activeIndex].entity_id);
      setOpen(false);
      setQ('');
    } else if (e.key === 'Escape') {
      setOpen(false);
      setQ('');
    }
  }

  return (
    <div className="absolute top-3 left-3 z-10">
      {open ? (
        <div className="w-72 bg-black/70 backdrop-blur-sm rounded-lg shadow-lg p-2">
          <input
            ref={inputRef}
            type="text"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder={t('knowledgeGraph.graph.searchPlaceholder', 'Find entity…')}
            className="w-full bg-transparent text-white text-sm outline-none px-2 py-1
              border-b border-white/20 focus:border-white/50"
            autoComplete="off"
            spellCheck={false}
          />
          {hits.length > 0 && (
            <ul role="listbox" className="mt-1.5 max-h-72 overflow-y-auto">
              {hits.map((hit, i) => (
                <li key={hit.entity_id}>
                  <button
                    type="button"
                    onClick={() => { onPick(hit.entity_id); setOpen(false); setQ(''); }}
                    onMouseEnter={() => setActiveIndex(i)}
                    className={`w-full text-left rounded px-2 py-1.5 text-xs transition-colors
                      ${i === activeIndex ? 'bg-white/15' : 'hover:bg-white/10'}`}
                  >
                    <p className="text-white truncate">{hit.display_name}</p>
                    <p className="text-[10px] text-gray-400 mt-0.5">
                      {hit.entity_type}
                      {hit.mention_count > 0 && ` · ${hit.mention_count} mentions`}
                    </p>
                  </button>
                </li>
              ))}
            </ul>
          )}
          {debouncedQ && hits.length === 0 && (
            <p className="text-[11px] text-gray-400 italic px-2 py-1.5">
              {t('knowledgeGraph.graph.searchNoResults', 'Nothing found')}
            </p>
          )}
        </div>
      ) : (
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="bg-black/50 hover:bg-black/70 text-white/80 hover:text-white
            px-3 py-1.5 rounded text-xs flex items-center gap-1.5"
          title={t('knowledgeGraph.graph.searchTitle', 'Search an entity')}
        >
          <span>🔍</span>
          <span>{t('knowledgeGraph.graph.searchButton', 'Find entity')}</span>
        </button>
      )}
    </div>
  );
}

// FocusEntity import marker — keeps TS from complaining about the unused import
// when builders are scoped helpers. The type is referenced via FocusNeighborhood.
const _: FocusEntity | undefined = undefined;
void _;

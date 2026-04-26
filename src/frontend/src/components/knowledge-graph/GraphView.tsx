import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import ForceGraph2D, {
  ForceGraphMethods,
  LinkObject,
  NodeObject,
} from 'react-force-graph-2d';

import apiClient from '../../utils/axios';

export type EntityType = 'person' | 'place' | 'organization' | 'thing' | 'event' | 'concept';

interface ApiEntity {
  id: string | number;
  name: string;
  entity_type?: EntityType;
  type?: EntityType;
  mention_count?: number;
}

interface ApiRelation {
  subject_id?: string | number;
  object_id?: string | number;
  subject?: { id: string | number };
  object?: { id: string | number };
  predicate?: string;
  confidence?: number;
}

interface EntitiesResponse {
  entities?: ApiEntity[];
}

interface RelationsResponse {
  relations?: ApiRelation[];
}

interface GraphNode {
  id: string | number;
  name: string;
  type?: EntityType;
  mentionCount: number;
  val: number;
  x?: number;
  y?: number;
  _isNew?: boolean;
}

interface GraphLink {
  source: string | number | GraphNode;
  target: string | number | GraphNode;
  label?: string;
  confidence?: number;
}

interface GraphData {
  nodes: GraphNode[];
  links: GraphLink[];
}

interface GraphViewProps {
  onEntityClick?: (id: GraphNode['id']) => void;
  onSwitchToEntities?: () => void;
  isDark?: boolean;
}

// Library-wrapped node/link shapes (NodeObject adds runtime fields like x, y, vx).
type FGNode = NodeObject<GraphNode>;
type FGLink = LinkObject<GraphNode, GraphLink>;
type FGRef = ForceGraphMethods<FGNode, FGLink>;

const TYPE_COLORS: Record<EntityType, string> = {
  person: '#3b82f6',
  place: '#22c55e',
  organization: '#a855f7',
  thing: '#f59e0b',
  event: '#ec4899',
  concept: '#14b8a6',
};

const TYPE_COLORS_DARK: Record<EntityType, string> = {
  person: '#60a5fa',
  place: '#4ade80',
  organization: '#c084fc',
  thing: '#fbbf24',
  event: '#f472b6',
  concept: '#2dd4bf',
};

const TYPE_LABELS: Record<EntityType, string> = {
  person: 'Person',
  place: 'Ort',
  organization: 'Organisation',
  thing: 'Objekt',
  event: 'Ereignis',
  concept: 'Konzept',
};

const MAX_NODES = 200;
const LABEL_ZOOM_THRESHOLD = 1.2;

function entityToNode(e: ApiEntity, typeOverride?: EntityType): GraphNode {
  return {
    id: e.id,
    name: e.name,
    type: typeOverride ?? e.entity_type ?? e.type,
    mentionCount: e.mention_count ?? 1,
    val: Math.max(2, Math.min(10, e.mention_count ?? 1)),
  };
}

export default function GraphView({ onEntityClick, onSwitchToEntities, isDark = false }: GraphViewProps) {
  const { t } = useTranslation();
  const graphRef = useRef<FGRef | undefined>(undefined);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [graphData, setGraphData] = useState<GraphData>({ nodes: [], links: [] });
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [dimensions, setDimensions] = useState<{ width: number; height: number }>({ width: 800, height: 600 });
  const [hoveredNode, setHoveredNode] = useState<GraphNode | null>(null);
  const [wsConnected, setWsConnected] = useState<boolean>(false);
  const wsRef = useRef<WebSocket | null>(null);
  const entityMapRef = useRef<Map<GraphNode['id'], GraphNode>>(new Map());

  const colors: Record<EntityType, string> = isDark ? TYPE_COLORS_DARK : TYPE_COLORS;

  // Measure container size — also re-measure when loading finishes
  useEffect(() => {
    if (!containerRef.current) return;

    const measure = (): void => {
      if (!containerRef.current) return;
      const { width, height } = containerRef.current.getBoundingClientRect();
      if (width > 0 && height > 0) {
        setDimensions({ width: Math.floor(width), height: Math.floor(height) });
      }
    };

    measure();

    const observer = new ResizeObserver(() => measure());
    observer.observe(containerRef.current);
    return () => observer.disconnect();
  }, [loading]);

  // Fetch initial data
  useEffect(() => {
    const fetchData = async (): Promise<void> => {
      try {
        setError(null);
        const [entitiesRes, relationsRes] = await Promise.all([
          apiClient.get<EntitiesResponse>('/api/knowledge-graph/entities', { params: { size: MAX_NODES } }),
          apiClient.get<RelationsResponse>('/api/knowledge-graph/relations', { params: { size: 200 } }),
        ]);

        const entities = entitiesRes.data.entities ?? [];
        const relations = relationsRes.data.relations ?? [];

        const entityMap = new Map<GraphNode['id'], GraphNode>();
        const nodes: GraphNode[] = entities.map((e) => {
          const node = entityToNode(e);
          entityMap.set(e.id, node);
          return node;
        });

        const links: GraphLink[] = relations
          .filter((r) => {
            const sId = r.subject_id ?? r.subject?.id;
            const oId = r.object_id ?? r.object?.id;
            return sId != null && oId != null && entityMap.has(sId) && entityMap.has(oId);
          })
          .map((r) => ({
            source: (r.subject_id ?? r.subject?.id) as GraphNode['id'],
            target: (r.object_id ?? r.object?.id) as GraphNode['id'],
            label: r.predicate,
            confidence: r.confidence,
          }));

        entityMapRef.current = entityMap;
        setGraphData({ nodes, links });

        // Center graph after data loads
        setTimeout(() => {
          graphRef.current?.zoomToFit(400, 40);
        }, 500);
      } catch (err) {
        console.error('Failed to load KG graph data:', err);
        setError(t('knowledgeGraph.graphError', 'Graph konnte nicht geladen werden.'));
      } finally {
        setLoading(false);
      }
    };

    fetchData();
  }, [t]);

  // WebSocket for live updates
  useEffect(() => {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws/knowledge-graph`;
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined;

    const connect = (): void => {
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.onopen = () => setWsConnected(true);

      ws.onmessage = (event: MessageEvent<string>): void => {
        try {
          const payload = JSON.parse(event.data) as {
            type?: string;
            entities?: ApiEntity[];
            relations?: ApiRelation[];
          };
          if (payload.type === 'kg_update') {
            handleKgUpdate(payload.entities ?? [], payload.relations ?? []);
          }
        } catch (err) {
          console.error('KG WS message parse error:', err);
        }
      };

      ws.onclose = () => {
        wsRef.current = null;
        setWsConnected(false);
        reconnectTimer = setTimeout(connect, 5000);
      };

      ws.onerror = () => {
        ws.close();
      };
    };

    connect();

    return () => {
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleKgUpdate = useCallback((newEntities: ApiEntity[], newRelations: ApiRelation[]): void => {
    setGraphData((prev) => {
      const entityMap = entityMapRef.current;
      const nodes = [...prev.nodes];
      const links = [...prev.links];

      for (const e of newEntities) {
        if (!entityMap.has(e.id)) {
          const node: GraphNode = {
            ...entityToNode(e),
            _isNew: true,
          };
          entityMap.set(e.id, node);
          nodes.push(node);
        } else {
          const existing = entityMap.get(e.id);
          if (existing && e.mention_count) {
            existing.mentionCount = e.mention_count;
            existing.val = Math.max(2, Math.min(10, e.mention_count));
          }
        }
      }

      for (const r of newRelations) {
        const sId = r.subject_id;
        const oId = r.object_id;
        if (sId != null && oId != null && entityMap.has(sId) && entityMap.has(oId)) {
          links.push({
            source: sId,
            target: oId,
            label: r.predicate,
            confidence: r.confidence,
          });
        }
      }

      // FIFO eviction if over max
      if (nodes.length > MAX_NODES) {
        const toRemove = nodes.splice(0, nodes.length - MAX_NODES);
        const removeIds = new Set<GraphNode['id']>(toRemove.map((n) => n.id));
        for (const id of removeIds) {
          entityMap.delete(id);
        }
        const filtered = links.filter((l) => {
          const sourceId = typeof l.source === 'object' ? l.source.id : l.source;
          const targetId = typeof l.target === 'object' ? l.target.id : l.target;
          return !removeIds.has(sourceId) && !removeIds.has(targetId);
        });
        return { nodes, links: filtered };
      }

      return { nodes, links };
    });
  }, []);

  const nodeCanvasObject = useCallback(
    (node: GraphNode, ctx: CanvasRenderingContext2D, globalScale: number): void => {
      const radius = Math.max(4, node.val * 1.5);
      const color = (node.type && colors[node.type]) || colors.thing;
      const isHovered = hoveredNode?.id === node.id;
      if (node.x == null || node.y == null) return;

      // Node circle
      ctx.beginPath();
      ctx.arc(node.x, node.y, isHovered ? radius * 1.3 : radius, 0, 2 * Math.PI);
      ctx.fillStyle = color;
      ctx.fill();

      // Glow for new or hovered nodes
      if (node._isNew || isHovered) {
        ctx.shadowColor = color;
        ctx.shadowBlur = isHovered ? 20 : 15;
        ctx.beginPath();
        ctx.arc(node.x, node.y, radius, 0, 2 * Math.PI);
        ctx.fill();
        ctx.shadowBlur = 0;
        if (node._isNew) {
          setTimeout(() => { node._isNew = false; }, 3000);
        }
      }

      // Label: show only when zoomed in enough, or when hovered
      if (globalScale > LABEL_ZOOM_THRESHOLD || isHovered) {
        const fontSize = isHovered ? Math.max(12, 14 / globalScale) : Math.max(9, 11 / globalScale);
        ctx.font = `${isHovered ? 'bold ' : ''}${fontSize}px sans-serif`;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillStyle = isDark ? '#e5e7eb' : '#1f2937';
        ctx.fillText(node.name, node.x, node.y + radius + fontSize);
      }
    },
    [colors, isDark, hoveredNode],
  );

  const linkCanvasObject = useCallback(
    (link: GraphLink, ctx: CanvasRenderingContext2D, globalScale: number): void => {
      const start = typeof link.source === 'object' ? link.source : null;
      const end = typeof link.target === 'object' ? link.target : null;
      if (!start || !end || start.x == null || start.y == null || end.x == null || end.y == null) return;

      ctx.beginPath();
      ctx.moveTo(start.x, start.y);
      ctx.lineTo(end.x, end.y);
      ctx.strokeStyle = isDark ? 'rgba(148, 163, 184, 0.3)' : 'rgba(107, 114, 128, 0.3)';
      ctx.lineWidth = 0.5;
      ctx.stroke();

      if (link.label && globalScale > 1.5) {
        const midX = (start.x + end.x) / 2;
        const midY = (start.y + end.y) / 2;
        const fontSize = Math.max(8, 10 / globalScale);
        ctx.font = `${fontSize}px sans-serif`;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillStyle = isDark ? 'rgba(148, 163, 184, 0.5)' : 'rgba(107, 114, 128, 0.5)';
        ctx.fillText(link.label, midX, midY);
      }
    },
    [isDark],
  );

  const handleNodeClick = useCallback(
    (node: GraphNode): void => {
      onEntityClick?.(node.id);
    },
    [onEntityClick],
  );

  const handleRetry = (): void => {
    setLoading(true);
    setError(null);
    setGraphData({ nodes: [], links: [] });
    entityMapRef.current = new Map();
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-96">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary-500" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center h-96 gap-4">
        <p className="text-gray-500 dark:text-gray-400">{error}</p>
        <button onClick={handleRetry} className="btn-primary px-4 py-2 text-sm">
          {t('common.retry', 'Erneut versuchen')}
        </button>
      </div>
    );
  }

  if (graphData.nodes.length === 0) {
    return (
      <div className="flex items-center justify-center h-96 text-gray-500 dark:text-gray-400">
        {t('knowledgeGraph.noEntities', 'Keine Entitäten vorhanden. Starte eine Unterhaltung, um den Wissensgraph aufzubauen.')}
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      className="relative w-full h-[calc(100vh-280px)] min-h-[400px] overflow-hidden"
      aria-label={t('knowledgeGraph.graphAriaLabel', 'Wissensgraph Visualisierung')}
    >
      <ForceGraph2D
        ref={graphRef}
        graphData={graphData}
        width={dimensions.width}
        height={dimensions.height}
        nodeCanvasObject={nodeCanvasObject}
        linkCanvasObject={linkCanvasObject}
        onNodeClick={handleNodeClick}
        onNodeHover={setHoveredNode as (node: GraphNode | null) => void}
        nodeId="id"
        linkSource="source"
        linkTarget="target"
        cooldownTicks={100}
        d3AlphaDecay={0.02}
        d3VelocityDecay={0.3}
        backgroundColor={isDark ? '#0f1117' : '#f8f7f5'}
        enableNodeDrag={true}
        enableZoomInteraction={true}
      />

      {hoveredNode && (
        <div className="absolute top-3 left-3 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg px-3 py-2 shadow-lg text-sm pointer-events-none">
          <p className="font-medium text-gray-900 dark:text-white">{hoveredNode.name}</p>
          <p className="text-gray-500 dark:text-gray-400">
            {(hoveredNode.type && TYPE_LABELS[hoveredNode.type]) || hoveredNode.type} · {hoveredNode.mentionCount}x {t('knowledgeGraph.mentions', 'erwähnt')}
          </p>
        </div>
      )}

      <div className="absolute bottom-10 left-3 flex flex-wrap gap-2 text-xs">
        {(Object.entries(TYPE_LABELS) as Array<[EntityType, string]>).map(([type, label]) => (
          <span key={type} className="flex items-center gap-1 text-gray-500 dark:text-gray-400">
            <span className="w-2.5 h-2.5 rounded-full inline-block" style={{ backgroundColor: colors[type] }} />
            {label}
          </span>
        ))}
      </div>

      <div className="absolute bottom-2 right-3 flex items-center gap-2 text-xs text-gray-400 dark:text-gray-600">
        <span className={`w-1.5 h-1.5 rounded-full ${wsConnected ? 'bg-green-400' : 'bg-gray-400'}`} />
        {graphData.nodes.length} {t('knowledgeGraph.entities', 'Entitäten')} · {graphData.links.length} {t('knowledgeGraph.relations', 'Relationen')}
      </div>

      <div className="absolute bottom-2 left-3 text-xs text-gray-400 dark:text-gray-600 sm:hidden">
        {onSwitchToEntities && (
          <button onClick={onSwitchToEntities} className="underline hover:text-gray-600 dark:hover:text-gray-400">
            {t('knowledgeGraph.switchToTable', 'Tabellenansicht')}
          </button>
        )}
      </div>
    </div>
  );
}

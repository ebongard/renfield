import {
  LayoutDashboard,
  BookOpen,
  Share2,
  Brain,
  CalendarClock,
  Inbox,
  NotebookPen,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import type { AtomType } from '../../api/resources/brain';

/**
 * A lens = one view over the knowledge corpus inside the Wissen workspace.
 *
 * This is the SINGLE SOURCE OF TRUTH for the workspace (D2): the route table
 * (`App.tsx`) wraps each lens route in a `ProtectedRoute` derived from
 * `permission`, and `LensRail` filters visibility from the same `permission` +
 * `feature`. The `nav.wissen` entry shows iff at least one lens is visible.
 *
 * Distinct icons per lens deliberately resolve the old dup-`Brain` collision
 * (`/brain` + `/memory` both used Brain) — only Notizen keeps Brain now.
 */
export interface LensDef {
  /** Stable key for tests / analytics. */
  key: string;
  /** Path segment under `/wissen` (`''` = the index/Übersicht route). */
  segment: string;
  /** i18n key for the rail label. */
  labelKey: string;
  icon: LucideIcon;
  /** Any-of permissions required to see the lens (omitted = always allowed). */
  permission?: string[];
  /** Feature flag gating the lens (omitted = always on). */
  feature?: string;
  /** Atom types this lens owns — drives search-result routing + scope filter. */
  atomTypes?: AtomType[];
  /**
   * True when the lens has its own inline search that should consume the
   * workspace `?q=` (D9 full-unify): Documents runs a chunk-semantic search,
   * Graph filters its entity table. For these the omnisearch is the single
   * input and the cross-corpus overlay is suppressed at `scope=lens` — the
   * lens renders results inline. Lenses without this fall back to the overlay.
   */
  consumesQueryInline?: boolean;
}

export const LENSES: LensDef[] = [
  { key: 'uebersicht', segment: '', labelKey: 'lens.uebersicht', icon: LayoutDashboard },
  {
    key: 'dokumente',
    segment: 'dokumente',
    labelKey: 'lens.dokumente',
    icon: BookOpen,
    permission: ['kb.own', 'kb.shared', 'kb.all'],
    feature: 'knowledge',
    atomTypes: ['kb_document'],
    consumesQueryInline: true,
  },
  {
    key: 'graph',
    segment: 'graph',
    labelKey: 'lens.graph',
    icon: Share2,
    feature: 'knowledge_graph',
    atomTypes: ['kg_node', 'kg_edge'],
    consumesQueryInline: true,
  },
  {
    key: 'erinnerungen',
    segment: 'erinnerungen',
    labelKey: 'lens.erinnerungen',
    icon: Brain,
    atomTypes: ['conversation_memory'],
  },
  {
    key: 'notizen',
    segment: 'notes',
    labelKey: 'lens.notizen',
    icon: NotebookPen,
    feature: 'notes',
    atomTypes: ['note'],
  },
  {
    key: 'fristen',
    segment: 'fristen',
    labelKey: 'lens.fristen',
    icon: CalendarClock,
    atomTypes: ['document_fact'],
  },
  { key: 'pruefen', segment: 'review', labelKey: 'lens.pruefen', icon: Inbox },
];

/** Absolute path for a lens (`/wissen` for the index, `/wissen/<segment>` else). */
export function lensPath(lens: LensDef): string {
  return lens.segment ? `/wissen/${lens.segment}` : '/wissen';
}

/** The auth surface lens gating reads — a structural subset of `useAuth()`. */
export interface LensVisibilityAuth {
  isFeatureEnabled: (feature: string) => boolean;
  hasAnyPermission: (permissions: string[]) => boolean;
  authEnabled: boolean;
}

/**
 * Single gate for "may this user see this lens" (D2) — shared by `LensRail`
 * and the Übersicht's "Bereiche" nav so visibility can never drift between the
 * rail and the dashboard. Feature flag first (applies even when auth is off),
 * then the any-of permission check (skipped in single-user mode).
 */
export function isLensVisible(lens: LensDef, auth: LensVisibilityAuth): boolean {
  if (lens.feature && !auth.isFeatureEnabled(lens.feature)) return false;
  if (!auth.authEnabled) return true;
  if (!lens.permission) return true;
  return auth.hasAnyPermission(lens.permission);
}

/** The lens owning a path segment (`''` = Übersicht index), or undefined. */
export function lensForSegment(segment: string): LensDef | undefined {
  return LENSES.find((l) => l.segment === segment);
}

/** Owning lens segment per atom type, derived from `LENSES.atomTypes` — the
 *  single source the search overlay uses to route + scope results. */
export const ATOM_LENS_SEGMENT: Partial<Record<AtomType, string>> = Object.fromEntries(
  LENSES.flatMap((lens) => (lens.atomTypes ?? []).map((at) => [at, lens.segment])),
);

import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { useAuth } from '../../../context/AuthContext';
import { useFeatureFlags } from '../../../api/resources/brain';
import { PALETTE_ACTIONS, type PaletteAction, type PaletteCategory } from './paletteActions';

/** A palette action resolved for the current user: localised + concrete path. */
export interface ResolvedPaletteAction {
  id: string;
  category: PaletteCategory;
  label: string;
  icon: PaletteAction['icon'];
  to?: string;           // navigate
  toolCommand?: string;  // tool (staged into composer)
  roleId?: string;       // set-role
}

function allowed(
  action: PaletteAction,
  hasPermission: (p: string) => boolean,
  hasAnyPermission: (ps: string[]) => boolean,
): boolean {
  const perms = action.requiredPermissions;
  if (!perms || perms.length === 0) return true;
  return action.requireAny ? hasAnyPermission(perms) : perms.every(hasPermission);
}

/**
 * Filters the static registry by the current user's permissions, resolves
 * navigation paths against the wissen-workspace flag, and (optionally) filters
 * by a search query against the localised label. Display-gate only — the
 * backend enforces the real permission check on execution.
 */
export function usePaletteActions(query: string): {
  visible: ResolvedPaletteAction[];
  filtered: ResolvedPaletteAction[];
} {
  const { t } = useTranslation();
  const { hasPermission, hasAnyPermission } = useAuth();
  const { data: features } = useFeatureFlags();
  const wissen = features?.wissen_workspace_enabled ?? false;

  const visible = useMemo<ResolvedPaletteAction[]>(() => {
    return PALETTE_ACTIONS
      .filter((a) => allowed(a, hasPermission, hasAnyPermission))
      .map((a) => ({
        id: a.id,
        category: a.category,
        label: t(a.labelKey),
        icon: a.icon,
        to: a.category === 'navigate' ? (wissen && a.wissenPath ? a.wissenPath : a.navigateTo) : undefined,
        toolCommand: a.toolCommand,
        roleId: a.roleId,
      }));
  }, [t, hasPermission, hasAnyPermission, wissen]);

  const filtered = useMemo<ResolvedPaletteAction[]>(() => {
    const q = query.trim().toLowerCase();
    if (!q) return visible;
    return visible.filter((a) => a.label.toLowerCase().includes(q));
  }, [visible, query]);

  return { visible, filtered };
}

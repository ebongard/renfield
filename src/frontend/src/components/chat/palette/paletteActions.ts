import type { LucideIcon } from 'lucide-react';
import {
  BookOpen, Brain, CalendarClock, Network, Users, Home, Camera,
  DoorOpen, Bluetooth, Moon, Sun, MapPin, Search, Sparkles, Wrench, FileText,
} from 'lucide-react';

/**
 * Static command-palette action catalog (chat-ui roadmap item 4).
 *
 * Pure data — no React, no hooks. `usePaletteActions` composes this with live
 * auth/feature state. Three categories:
 *  - `navigate`: client-side route jump (no server involvement).
 *  - `tool`: stages a natural-language command into the composer (the user
 *    reviews + sends; the agent loop enforces the real permission gate).
 *  - `set-role`: stages a next-turn agent-role hint.
 *
 * `requiredPermissions` are `Permission` enum string values; `[]` = always shown.
 * `requireAny`=true → OR (the user needs any one); default AND. The display
 * filter is a UX courtesy — the backend is the real gate.
 */

export type PaletteCategory = 'navigate' | 'tool' | 'set-role';

export interface PaletteAction {
  id: string;
  category: PaletteCategory;
  labelKey: string;
  icon: LucideIcon;
  requiredPermissions?: string[];
  requireAny?: boolean;
  /** navigate: legacy flat route; `wissenPath` overrides when wissen workspace is on. */
  navigateTo?: string;
  wissenPath?: string;
  /** tool: natural-language command staged into the composer. */
  toolCommand?: string;
  /** set-role: agent role id from agent_roles.yaml. */
  roleId?: string;
}

export const PALETTE_ACTIONS: PaletteAction[] = [
  // --- Navigate ---
  { id: 'nav.knowledge', category: 'navigate', labelKey: 'chat.palette.actions.knowledge', icon: BookOpen,
    requiredPermissions: ['kb.own', 'kb.shared', 'kb.all'], requireAny: true,
    navigateTo: '/knowledge', wissenPath: '/wissen/dokumente' },
  { id: 'nav.brain', category: 'navigate', labelKey: 'chat.palette.actions.brain', icon: Brain,
    navigateTo: '/brain', wissenPath: '/wissen' },
  { id: 'nav.fristen', category: 'navigate', labelKey: 'chat.palette.actions.fristen', icon: CalendarClock,
    navigateTo: '/brain/fristen', wissenPath: '/wissen/fristen' },
  { id: 'nav.memory', category: 'navigate', labelKey: 'chat.palette.actions.memory', icon: Brain,
    navigateTo: '/memory', wissenPath: '/wissen/erinnerungen' },
  { id: 'nav.graph', category: 'navigate', labelKey: 'chat.palette.actions.graph', icon: Network,
    requiredPermissions: ['kg.view'], navigateTo: '/knowledge-graph', wissenPath: '/wissen/graph' },
  { id: 'nav.circles', category: 'navigate', labelKey: 'chat.palette.actions.circles', icon: Users,
    navigateTo: '/settings/circles' },
  { id: 'nav.homeassistant', category: 'navigate', labelKey: 'chat.palette.actions.homeassistant', icon: Home,
    requiredPermissions: ['ha.read'], requireAny: true, navigateTo: '/homeassistant' },
  { id: 'nav.cameras', category: 'navigate', labelKey: 'chat.palette.actions.cameras', icon: Camera,
    requiredPermissions: ['cam.view'], requireAny: true, navigateTo: '/camera' },
  { id: 'nav.rooms', category: 'navigate', labelKey: 'chat.palette.actions.rooms', icon: DoorOpen,
    requiredPermissions: ['rooms.read'], requireAny: true, navigateTo: '/rooms' },

  // --- Tools (staged into composer, not auto-sent) ---
  { id: 'tool.bt_scan', category: 'tool', labelKey: 'chat.palette.actions.btScan', icon: Bluetooth,
    requiredPermissions: ['ha.control'], requireAny: true, toolCommand: 'Scanne alle Bluetooth-Geräte' },
  { id: 'tool.good_night', category: 'tool', labelKey: 'chat.palette.actions.goodNight', icon: Moon,
    requiredPermissions: ['ha.control'], requireAny: true, toolCommand: 'Starte die Gute-Nacht-Routine' },
  { id: 'tool.good_morning', category: 'tool', labelKey: 'chat.palette.actions.goodMorning', icon: Sun,
    requiredPermissions: ['ha.control'], requireAny: true, toolCommand: 'Starte die Guten-Morgen-Routine' },
  { id: 'tool.presence', category: 'tool', labelKey: 'chat.palette.actions.presence', icon: MapPin,
    toolCommand: 'Wer ist gerade zuhause?' },
  { id: 'tool.search_docs', category: 'tool', labelKey: 'chat.palette.actions.searchDocs', icon: Search,
    requiredPermissions: ['kb.own', 'kb.shared', 'kb.all'], requireAny: true,
    toolCommand: 'Suche in meinen Dokumenten: ' },

  // --- Set agent role for the next turn ---
  { id: 'role.smart_home', category: 'set-role', labelKey: 'chat.palette.actions.roleSmartHome', icon: Home,
    requiredPermissions: ['ha.read'], requireAny: true, roleId: 'smart_home' },
  { id: 'role.media', category: 'set-role', labelKey: 'chat.palette.actions.roleMedia', icon: Sparkles,
    roleId: 'media' },
  { id: 'role.documents', category: 'set-role', labelKey: 'chat.palette.actions.roleDocuments', icon: FileText,
    requiredPermissions: ['kb.own', 'kb.shared', 'kb.all'], requireAny: true, roleId: 'documents' },
  { id: 'role.research', category: 'set-role', labelKey: 'chat.palette.actions.roleResearch', icon: Search,
    roleId: 'research' },
  { id: 'role.general', category: 'set-role', labelKey: 'chat.palette.actions.roleGeneral', icon: Wrench,
    roleId: 'general' },
];

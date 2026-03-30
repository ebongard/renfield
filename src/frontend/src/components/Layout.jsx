import React, { useState, useEffect, useRef } from 'react';
import { Link, useLocation, useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import {
  MessageSquare,
  CheckSquare,
  Camera,
  Lightbulb,
  Users,
  Menu,
  X,
  DoorOpen,
  Settings,
  ChevronDown,
  BookOpen,
  LogIn,
  LogOut,
  Shield,
  UserCog,
  User,
  Satellite,
  Blocks,
  Zap,
  Brain,
  MapPin,
  Wrench,
  FileSearch,
  Share2
} from 'lucide-react';
import DeviceStatus from './DeviceStatus';
import ThemeToggle from './ThemeToggle';
import LanguageSwitcher from './LanguageSwitcher';
import NotificationToast from './NotificationToast';
import { useAuth } from '../context/AuthContext';

// Navigation items with translation keys
const mainNavigationConfig = [
  { nameKey: 'nav.chat', href: '/', icon: MessageSquare },
  { nameKey: 'nav.knowledge', href: '/knowledge', icon: BookOpen, permission: ['kb.own', 'kb.shared', 'kb.all'] },
  { nameKey: 'nav.memory', href: '/memory', icon: Brain },
  { nameKey: 'nav.knowledgeGraph', href: '/knowledge-graph', icon: Share2 },
  { nameKey: 'nav.tasks', href: '/tasks', icon: CheckSquare },
  { nameKey: 'nav.cameras', href: '/camera', icon: Camera, permission: ['cam.view', 'cam.full'], feature: 'cameras' },
];

// Admin navigation with translation keys
const adminNavigationConfig = [
  { nameKey: 'nav.rooms', href: '/rooms', icon: DoorOpen, permission: ['rooms.read', 'rooms.manage'] },
  { nameKey: 'nav.speakers', href: '/speakers', icon: Users, permission: ['speakers.own', 'speakers.all'] },
  { nameKey: 'nav.smarthome', href: '/homeassistant', icon: Lightbulb, permission: ['ha.read', 'ha.control', 'ha.full'], feature: 'smart_home' },
  { nameKey: 'nav.integrations', href: '/admin/integrations', icon: Blocks, permission: ['admin', 'plugins.use', 'plugins.manage'] },
  { nameKey: 'nav.intents', href: '/admin/intents', icon: Zap, permission: ['admin'] },
  { nameKey: 'nav.users', href: '/admin/users', icon: UserCog, permission: ['admin'] },
  { nameKey: 'nav.roles', href: '/admin/roles', icon: Shield, permission: ['admin'] },
  { nameKey: 'nav.satellites', href: '/admin/satellites', icon: Satellite, permission: ['admin'], feature: 'satellites' },
  { nameKey: 'nav.presence', href: '/admin/presence', icon: MapPin, permission: ['admin'] },
  { nameKey: 'nav.paperlessAudit', href: '/admin/paperless-audit', icon: FileSearch, permission: ['admin'] },
  { nameKey: 'nav.maintenance', href: '/admin/maintenance', icon: Wrench, permission: ['admin'] },
  { nameKey: 'nav.settings', href: '/admin/settings', icon: Settings, permission: ['admin'] },
];

export default function Layout({ children }) {
  const { t } = useTranslation();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [adminExpanded, setAdminExpanded] = useState(() => {
    if (typeof window !== 'undefined') {
      return localStorage.getItem('adminExpanded') === 'true';
    }
    return false;
  });

  const location = useLocation();
  const navigate = useNavigate();
  const sidebarRef = useRef(null);
  const firstFocusableRef = useRef(null);

  // Auth context
  const { user, isAuthenticated, authEnabled, logout, hasAnyPermission, isFeatureEnabled, loading: authLoading } = useAuth();

  // Translate navigation items
  const mainNavigation = mainNavigationConfig.map(item => ({
    ...item,
    name: t(item.nameKey)
  }));

  const adminNavigation = adminNavigationConfig.map(item => ({
    ...item,
    name: t(item.nameKey)
  }));

  // Filter navigation items based on features and permissions
  const filterNavItems = (items) => {
    return items.filter(item => {
      // Feature flag check first
      if (item.feature && !isFeatureEnabled(item.feature)) return false;
      // Permission check
      if (!authEnabled) return true;
      if (!item.permission) return true;
      return hasAnyPermission(item.permission);
    });
  };

  const visibleMainNav = filterNavItems(mainNavigation);
  const visibleAdminNav = filterNavItems(adminNavigation);

  // Handle logout
  const handleLogout = () => {
    logout();
    setSidebarOpen(false);
    navigate('/login');
  };

  // Admin-Toggle mit localStorage
  const toggleAdmin = () => {
    const newState = !adminExpanded;
    setAdminExpanded(newState);
    localStorage.setItem('adminExpanded', String(newState));
  };

  // Sidebar schliessen bei Navigation
  const handleNavClick = () => {
    setSidebarOpen(false);
  };

  // Check ob aktuelle Route im Admin-Bereich ist
  const isAdminRoute = visibleAdminNav.some(item => item.href === location.pathname);

  // Admin automatisch aufklappen wenn Admin-Route aktiv
  useEffect(() => {
    if (isAdminRoute && !adminExpanded) {
      setAdminExpanded(true);
      localStorage.setItem('adminExpanded', 'true');
    }
  }, [location.pathname]);

  // Escape-Key und Click-Outside Handler
  useEffect(() => {
    const handleEscape = (e) => {
      if (e.key === 'Escape' && sidebarOpen) {
        setSidebarOpen(false);
      }
    };

    const handleClickOutside = (e) => {
      if (sidebarOpen && sidebarRef.current && !sidebarRef.current.contains(e.target)) {
        setSidebarOpen(false);
      }
    };

    document.addEventListener('keydown', handleEscape);
    document.addEventListener('mousedown', handleClickOutside);

    return () => {
      document.removeEventListener('keydown', handleEscape);
      document.removeEventListener('mousedown', handleClickOutside);
    };
  }, [sidebarOpen]);

  // Focus-Management: Focus auf erstes Element wenn Sidebar oeffnet
  useEffect(() => {
    if (sidebarOpen && firstFocusableRef.current) {
      firstFocusableRef.current.focus();
    }
  }, [sidebarOpen]);

  // Body scroll lock wenn Sidebar offen
  useEffect(() => {
    if (sidebarOpen) {
      document.body.style.overflow = 'hidden';
    } else {
      document.body.style.overflow = '';
    }
    return () => {
      document.body.style.overflow = '';
    };
  }, [sidebarOpen]);

  const NavLink = ({ item, onClick }) => {
    const Icon = item.icon;
    const isActive = location.pathname === item.href;

    return (
      <Link
        to={item.href}
        onClick={onClick}
        className={`flex items-center space-x-3 px-3 py-3 rounded-lg text-sm font-medium transition-colors relative ${
          isActive
            ? 'bg-primary-600/20 text-primary-600 dark:text-primary-400'
            : 'text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700/50 hover:text-gray-900 dark:hover:text-white'
        }`}
        aria-current={isActive ? 'page' : undefined}
      >
        {isActive && (
          <span className="absolute left-0 top-1/2 -translate-y-1/2 w-0.5 h-5 bg-accent-400 rounded-r" />
        )}
        <Icon className="w-5 h-5 shrink-0" aria-hidden="true" />
        <span className="lg:opacity-0 lg:group-hover/sidebar:opacity-100 transition-opacity duration-200 overflow-hidden whitespace-nowrap">{item.name}</span>
      </Link>
    );
  };

  return (
    <div className="min-h-screen transition-colors">
      {/* Skip Link for Accessibility */}
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:absolute focus:top-4 focus:left-4 focus:z-60 focus:px-4 focus:py-2 focus:bg-primary-600 focus:text-white focus:rounded-lg focus:outline-hidden focus:ring-2 focus:ring-primary-400"
      >
        {t('nav.skipToContent')}
      </a>

      {/* Fixed Header */}
      <header className="fixed top-0 left-0 right-0 h-16 bg-white dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700 z-40 transition-colors lg:pl-16">
        <div className="h-full px-4 flex items-center justify-between">
          {/* Left: Hamburger + Logo */}
          <div className="flex items-center space-x-2">
            <button
              onClick={() => setSidebarOpen(true)}
              className="w-11 h-11 flex items-center justify-center rounded-lg text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white hover:bg-gray-100 dark:hover:bg-gray-700 focus:outline-hidden focus:ring-2 focus:ring-primary-500 transition-colors active:scale-95 lg:hidden"
              aria-label={t('nav.openMenu')}
              aria-expanded={sidebarOpen}
              aria-controls="sidebar"
            >
              <Menu className="w-6 h-6" aria-hidden="true" />
            </button>

            <Link to="/" className="flex items-center">
              <img src="/renfield-logo-header.svg" alt="Renfield" className="h-12 w-auto" />
            </Link>
          </div>

          {/* Right: Language + Theme Toggle + Device Status + User */}
          <div className="flex items-center space-x-2 sm:space-x-3">
            <LanguageSwitcher compact />
            <ThemeToggle />
            <DeviceStatus compact />

            {/* User/Auth in Header */}
            {authEnabled && (
              isAuthenticated ? (
                <button
                  onClick={handleLogout}
                  className="flex items-center space-x-2 px-3 py-1.5 rounded-lg text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
                  title={`${t('auth.loggedInAs')} ${user?.username}`}
                >
                  <div className="w-7 h-7 rounded-full bg-primary-600/30 flex items-center justify-center">
                    <User className="w-4 h-4 text-primary-600 dark:text-primary-400" />
                  </div>
                  <span className="hidden sm:block text-sm">{user?.username}</span>
                </button>
              ) : (
                <Link
                  to="/login"
                  className="flex items-center space-x-2 px-3 py-1.5 rounded-lg text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
                >
                  <LogIn className="w-5 h-5" />
                  <span className="hidden sm:block text-sm">{t('auth.login')}</span>
                </Link>
              )
            )}
          </div>
        </div>
      </header>

      {/* Backdrop (mobile only) */}
      <div
        className={`fixed inset-0 bg-black/50 dark:bg-black/60 backdrop-blur-xs z-40 transition-opacity duration-300 lg:hidden ${
          sidebarOpen ? 'opacity-100' : 'opacity-0 pointer-events-none'
        }`}
        aria-hidden="true"
        onClick={() => setSidebarOpen(false)}
      />

      {/* Sidebar — mobile: slide overlay; desktop: persistent rail with hover expand */}
      <aside
        ref={sidebarRef}
        id="sidebar"
        className={`group/sidebar fixed top-0 left-0 h-full flex flex-col bg-white dark:bg-gray-800 border-r border-gray-200 dark:border-gray-700 z-50 transform transition-all duration-300 ease-out
          w-72 lg:w-16 lg:hover:w-72 lg:translate-x-0
          ${sidebarOpen ? 'translate-x-0' : '-translate-x-full'}
        `}
        aria-label={t('nav.mainNavigation')}
        role="dialog"
        aria-modal="true"
      >
        {/* Sidebar Header */}
        <div className="relative flex items-center h-16 px-4 border-b border-gray-200 dark:border-gray-700">
          {/* Rail state: hamburger icon centered (desktop only, hidden on hover) */}
          <div className="hidden lg:flex lg:group-hover/sidebar:hidden items-center justify-center absolute inset-0">
            <button
              onClick={() => setSidebarOpen(true)}
              className="p-2.5 rounded-lg text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors active:scale-95"
              aria-label={t('nav.openMenu')}
            >
              <Menu className="w-5 h-5" aria-hidden="true" />
            </button>
          </div>

          {/* Full logo + close — shown on mobile + desktop hover */}
          <div className="flex items-center justify-between w-full lg:opacity-0 lg:group-hover/sidebar:opacity-100 transition-opacity duration-200">
            <Link to="/" onClick={handleNavClick} className="flex items-center overflow-hidden">
              <img src="/renfield-logo-header.svg" alt="Renfield" className="h-11 w-auto" />
            </Link>
            <button
              ref={firstFocusableRef}
              onClick={() => setSidebarOpen(false)}
              className="p-2.5 rounded-lg text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white hover:bg-gray-100 dark:hover:bg-gray-700 focus:outline-hidden focus:ring-2 focus:ring-primary-500 transition-colors lg:hidden"
              aria-label={t('nav.closeMenu')}
            >
              <X className="w-5 h-5" aria-hidden="true" />
            </button>
          </div>
        </div>

        {/* Navigation */}
        <nav className="flex-1 px-3 py-4 space-y-1 overflow-y-auto overflow-x-hidden">
          {/* Main Navigation */}
          {visibleMainNav.map((item) => (
            <NavLink key={item.href} item={item} onClick={handleNavClick} />
          ))}

          {/* Divider */}
          <div className="my-4 border-t border-gray-200 dark:border-gray-700" />

          {/* Admin Section - only show if there are visible admin items */}
          {visibleAdminNav.length > 0 && (
            <>
              <button
                onClick={toggleAdmin}
                className={`w-full flex items-center justify-between px-3 py-3 rounded-lg text-sm font-medium transition-colors ${
                  isAdminRoute
                    ? 'bg-gray-100 dark:bg-gray-700/50 text-primary-600 dark:text-primary-400'
                    : 'text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700/50 hover:text-gray-900 dark:hover:text-white'
                }`}
                aria-expanded={adminExpanded}
                aria-controls="admin-menu"
              >
                <div className="flex items-center space-x-3">
                  <Settings className="w-5 h-5 shrink-0" aria-hidden="true" />
                  <span className="lg:opacity-0 lg:group-hover/sidebar:opacity-100 transition-opacity duration-200 overflow-hidden whitespace-nowrap">{t('nav.admin')}</span>
                </div>
                <ChevronDown
                  className={`w-4 h-4 transition-transform duration-200 lg:opacity-0 lg:group-hover/sidebar:opacity-100 ${
                    adminExpanded ? 'rotate-180' : ''
                  }`}
                  aria-hidden="true"
                />
              </button>

              {/* Admin Submenu — hidden in rail, shown on hover when expanded */}
              <div
                id="admin-menu"
                className={`overflow-hidden transition-all duration-200 ease-in-out ${
                  adminExpanded
                    ? 'max-h-[600px] opacity-100 lg:max-h-0 lg:opacity-0 lg:group-hover/sidebar:max-h-[600px] lg:group-hover/sidebar:opacity-100'
                    : 'max-h-0 opacity-0'
                }`}
              >
                <div className="ml-3 pl-3 border-l border-gray-200 dark:border-gray-700 space-y-1 py-1">
                  {visibleAdminNav.map((item) => (
                    <NavLink key={item.href} item={item} onClick={handleNavClick} />
                  ))}
                </div>
              </div>
            </>
          )}

          {/* Auth Section */}
          {authEnabled && (
            <>
              <div className="my-4 border-t border-gray-200 dark:border-gray-700" />

              {isAuthenticated ? (
                <div className="space-y-2">
                  {/* User Info */}
                  <div className="px-3 py-2 rounded-lg bg-gray-100 dark:bg-gray-700/30">
                    <div className="flex items-center space-x-3">
                      <div className="w-8 h-8 rounded-full bg-primary-600/30 flex items-center justify-center shrink-0">
                        <User className="w-4 h-4 text-primary-600 dark:text-primary-400" />
                      </div>
                      <div className="flex-1 min-w-0 lg:opacity-0 lg:group-hover/sidebar:opacity-100 transition-opacity duration-200">
                        <p className="text-sm font-medium text-gray-900 dark:text-white truncate">{user?.username}</p>
                        <p className="text-xs text-gray-500 dark:text-gray-400 truncate">{user?.role}</p>
                      </div>
                    </div>
                  </div>

                  {/* Logout Button */}
                  <button
                    onClick={handleLogout}
                    className="w-full flex items-center space-x-3 px-3 py-3 rounded-lg text-sm font-medium text-gray-600 dark:text-gray-300 hover:bg-red-100 dark:hover:bg-red-900/30 hover:text-red-600 dark:hover:text-red-400 transition-colors"
                  >
                    <LogOut className="w-5 h-5 shrink-0" aria-hidden="true" />
                    <span className="lg:opacity-0 lg:group-hover/sidebar:opacity-100 transition-opacity duration-200 overflow-hidden whitespace-nowrap">{t('auth.logout')}</span>
                  </button>
                </div>
              ) : (
                <Link
                  to="/login"
                  onClick={handleNavClick}
                  className="flex items-center space-x-3 px-3 py-3 rounded-lg text-sm font-medium text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700/50 hover:text-gray-900 dark:hover:text-white transition-colors"
                >
                  <LogIn className="w-5 h-5 shrink-0" aria-hidden="true" />
                  <span className="lg:opacity-0 lg:group-hover/sidebar:opacity-100 transition-opacity duration-200 overflow-hidden whitespace-nowrap">{t('auth.login')}</span>
                </Link>
              )}
            </>
          )}
        </nav>

        {/* Sidebar Footer - Device Status */}
        <div className="shrink-0 p-4 border-t border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 lg:opacity-0 lg:group-hover/sidebar:opacity-100 transition-opacity duration-200">
          <DeviceStatus />
        </div>
      </aside>

      {/* Notification Toasts */}
      <NotificationToast />

      {/* Main Content */}
      <main
        id="main-content"
        className="pt-16 min-h-screen lg:pl-16"
        tabIndex={-1}
      >
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
          <div key={location.pathname} className="animate-fade-slide-in">
            {children}
          </div>
        </div>
      </main>
    </div>
  );
}

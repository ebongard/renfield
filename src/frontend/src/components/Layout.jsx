import React, { useState, useEffect, useRef } from 'react';
import { Link, useLocation } from 'react-router-dom';
import {
  Home,
  MessageSquare,
  CheckSquare,
  Camera,
  Lightbulb,
  Users,
  Menu,
  X,
  DoorOpen,
  Settings,
  ChevronDown
} from 'lucide-react';
import DeviceStatus from './DeviceStatus';

// Hauptnavigation
const mainNavigation = [
  { name: 'Home', href: '/', icon: Home },
  { name: 'Chat', href: '/chat', icon: MessageSquare },
  { name: 'Aufgaben', href: '/tasks', icon: CheckSquare },
  { name: 'Kameras', href: '/camera', icon: Camera },
];

// Admin-Navigation (Untermenue)
const adminNavigation = [
  { name: 'Raeume', href: '/rooms', icon: DoorOpen },
  { name: 'Sprecher', href: '/speakers', icon: Users },
  { name: 'Smart Home', href: '/homeassistant', icon: Lightbulb },
];

export default function Layout({ children }) {
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [adminExpanded, setAdminExpanded] = useState(() => {
    if (typeof window !== 'undefined') {
      return localStorage.getItem('adminExpanded') === 'true';
    }
    return false;
  });

  const location = useLocation();
  const sidebarRef = useRef(null);
  const firstFocusableRef = useRef(null);

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
  const isAdminRoute = adminNavigation.some(item => item.href === location.pathname);

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
        className={`flex items-center space-x-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors ${
          isActive
            ? 'bg-primary-600/20 text-primary-400 border-l-2 border-primary-400'
            : 'text-gray-300 hover:bg-gray-700/50 hover:text-white'
        }`}
        aria-current={isActive ? 'page' : undefined}
      >
        <Icon className="w-5 h-5 flex-shrink-0" aria-hidden="true" />
        <span>{item.name}</span>
      </Link>
    );
  };

  return (
    <div className="min-h-screen bg-gray-900">
      {/* Skip Link for Accessibility */}
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:absolute focus:top-4 focus:left-4 focus:z-[60] focus:px-4 focus:py-2 focus:bg-primary-600 focus:text-white focus:rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-400"
      >
        Zum Inhalt springen
      </a>

      {/* Fixed Header */}
      <header className="fixed top-0 left-0 right-0 h-16 bg-gray-800 border-b border-gray-700 z-40">
        <div className="h-full px-4 flex items-center justify-between">
          {/* Left: Hamburger + Logo */}
          <div className="flex items-center space-x-3">
            <button
              onClick={() => setSidebarOpen(true)}
              className="p-2 rounded-lg text-gray-400 hover:text-white hover:bg-gray-700 focus:outline-none focus:ring-2 focus:ring-primary-500 transition-colors"
              aria-label="Menu oeffnen"
              aria-expanded={sidebarOpen}
              aria-controls="sidebar"
            >
              <Menu className="w-6 h-6" aria-hidden="true" />
            </button>

            <Link to="/" className="flex items-center space-x-2">
              <div className="w-8 h-8 bg-primary-600 rounded-lg flex items-center justify-center">
                <span className="text-white font-bold text-xl">R</span>
              </div>
              <span className="text-xl font-bold text-white hidden sm:block">Renfield</span>
            </Link>
          </div>

          {/* Right: Device Status */}
          <div>
            <DeviceStatus compact />
          </div>
        </div>
      </header>

      {/* Backdrop */}
      <div
        className={`fixed inset-0 bg-black/60 backdrop-blur-sm z-40 transition-opacity duration-300 ${
          sidebarOpen ? 'opacity-100' : 'opacity-0 pointer-events-none'
        }`}
        aria-hidden="true"
        onClick={() => setSidebarOpen(false)}
      />

      {/* Sidebar */}
      <aside
        ref={sidebarRef}
        id="sidebar"
        className={`fixed top-0 left-0 h-full w-72 bg-gray-800 border-r border-gray-700 z-50 transform transition-transform duration-300 ease-out ${
          sidebarOpen ? 'translate-x-0' : '-translate-x-full'
        }`}
        aria-label="Hauptnavigation"
        role="dialog"
        aria-modal="true"
      >
        {/* Sidebar Header */}
        <div className="flex items-center justify-between h-16 px-4 border-b border-gray-700">
          <Link to="/" onClick={handleNavClick} className="flex items-center space-x-2">
            <div className="w-8 h-8 bg-primary-600 rounded-lg flex items-center justify-center">
              <span className="text-white font-bold text-xl">R</span>
            </div>
            <span className="text-xl font-bold text-white">Renfield</span>
          </Link>

          <button
            ref={firstFocusableRef}
            onClick={() => setSidebarOpen(false)}
            className="p-2 rounded-lg text-gray-400 hover:text-white hover:bg-gray-700 focus:outline-none focus:ring-2 focus:ring-primary-500 transition-colors"
            aria-label="Menu schliessen"
          >
            <X className="w-5 h-5" aria-hidden="true" />
          </button>
        </div>

        {/* Navigation */}
        <nav className="flex-1 px-3 py-4 space-y-1 overflow-y-auto">
          {/* Main Navigation */}
          {mainNavigation.map((item) => (
            <NavLink key={item.href} item={item} onClick={handleNavClick} />
          ))}

          {/* Divider */}
          <div className="my-4 border-t border-gray-700" />

          {/* Admin Section */}
          <button
            onClick={toggleAdmin}
            className={`w-full flex items-center justify-between px-3 py-2.5 rounded-lg text-sm font-medium transition-colors ${
              isAdminRoute
                ? 'bg-gray-700/50 text-primary-400'
                : 'text-gray-300 hover:bg-gray-700/50 hover:text-white'
            }`}
            aria-expanded={adminExpanded}
            aria-controls="admin-menu"
          >
            <div className="flex items-center space-x-3">
              <Settings className="w-5 h-5 flex-shrink-0" aria-hidden="true" />
              <span>Admin</span>
            </div>
            <ChevronDown
              className={`w-4 h-4 transition-transform duration-200 ${
                adminExpanded ? 'rotate-180' : ''
              }`}
              aria-hidden="true"
            />
          </button>

          {/* Admin Submenu */}
          <div
            id="admin-menu"
            className={`overflow-hidden transition-all duration-200 ease-in-out ${
              adminExpanded ? 'max-h-48 opacity-100' : 'max-h-0 opacity-0'
            }`}
          >
            <div className="ml-3 pl-3 border-l border-gray-700 space-y-1 py-1">
              {adminNavigation.map((item) => (
                <NavLink key={item.href} item={item} onClick={handleNavClick} />
              ))}
            </div>
          </div>
        </nav>

        {/* Sidebar Footer - Device Status */}
        <div className="absolute bottom-0 left-0 right-0 p-4 border-t border-gray-700 bg-gray-800">
          <DeviceStatus />
        </div>
      </aside>

      {/* Main Content */}
      <main
        id="main-content"
        className="pt-16 min-h-screen"
        tabIndex={-1}
      >
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
          {children}
        </div>
      </main>
    </div>
  );
}

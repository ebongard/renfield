/**
 * ErrorBoundary Component
 *
 * Catches JavaScript errors in child components and displays
 * a fallback UI instead of crashing the whole app.
 */

import React from 'react';
import { AlertTriangle, RefreshCw } from 'lucide-react';

class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false, error: null, errorInfo: null };
  }

  static getDerivedStateFromError(error) {
    // Update state so next render shows the fallback UI
    return { hasError: true, error };
  }

  componentDidCatch(error, errorInfo) {
    // Log error to console or error reporting service
    console.error('ErrorBoundary caught an error:', error, errorInfo);
    this.setState({ errorInfo });

    // You could send to an error reporting service here
    // logErrorToService(error, errorInfo);
  }

  handleReload = () => {
    window.location.reload();
  };

  handleRetry = () => {
    this.setState({ hasError: false, error: null, errorInfo: null });
  };

  render() {
    if (this.state.hasError) {
      // Custom fallback UI
      if (this.props.fallback) {
        return this.props.fallback;
      }

      return (
        <div className="min-h-screen bg-gray-900 flex items-center justify-center p-4">
          <div className="card max-w-md w-full text-center">
            {/* Icon */}
            <div className="w-16 h-16 mx-auto mb-4 bg-red-600/20 rounded-full flex items-center justify-center">
              <AlertTriangle className="w-8 h-8 text-red-500" aria-hidden="true" />
            </div>

            {/* Title */}
            <h1 className="text-xl font-bold text-white mb-2">
              Etwas ist schiefgelaufen
            </h1>

            {/* Message */}
            <p className="text-gray-400 mb-6">
              Ein unerwarteter Fehler ist aufgetreten. Bitte versuche es erneut oder lade die Seite neu.
            </p>

            {/* Error details (only in development) */}
            {import.meta.env.DEV && this.state.error && (
              <details className="mb-6 text-left">
                <summary className="text-sm text-gray-500 cursor-pointer hover:text-gray-400">
                  Technische Details anzeigen
                </summary>
                <pre className="mt-2 p-3 bg-gray-800 rounded-lg text-xs text-red-400 overflow-auto max-h-40">
                  {this.state.error.toString()}
                  {this.state.errorInfo?.componentStack}
                </pre>
              </details>
            )}

            {/* Actions */}
            <div className="flex space-x-3">
              <button
                onClick={this.handleRetry}
                className="flex-1 btn bg-gray-700 hover:bg-gray-600 text-white flex items-center justify-center space-x-2"
              >
                <span>Erneut versuchen</span>
              </button>
              <button
                onClick={this.handleReload}
                className="flex-1 btn btn-primary flex items-center justify-center space-x-2"
              >
                <RefreshCw className="w-4 h-4" aria-hidden="true" />
                <span>Seite neu laden</span>
              </button>
            </div>
          </div>
        </div>
      );
    }

    return this.props.children;
  }
}

export default ErrorBoundary;

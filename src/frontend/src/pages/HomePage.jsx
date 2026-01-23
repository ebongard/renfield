import React, { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { MessageSquare, Mic, Camera, Lightbulb, Activity } from 'lucide-react';
import apiClient from '../utils/axios';

const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';

export default function HomePage() {
  const [health, setHealth] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    checkHealth();
  }, []);

  const checkHealth = async () => {
    try {
      const response = await apiClient.get('/health');
      setHealth(response.data);
    } catch (error) {
      console.error('Health check failed:', error);
    } finally {
      setLoading(false);
    }
  };

  const features = [
    {
      name: 'Chat',
      description: 'Unterhalte dich mit deinem KI-Assistenten',
      icon: MessageSquare,
      href: '/chat',
      color: 'bg-blue-600'
    },
    {
      name: 'Sprachsteuerung',
      description: 'Nutze Spracheingabe und -ausgabe',
      icon: Mic,
      href: '/chat',
      color: 'bg-purple-600'
    },
    {
      name: 'Kamera-Überwachung',
      description: 'Überwache deine Kameras und erhalte Benachrichtigungen',
      icon: Camera,
      href: '/camera',
      color: 'bg-green-600'
    },
    {
      name: 'Smart Home',
      description: 'Steuere dein Zuhause mit Home Assistant',
      icon: Lightbulb,
      href: '/homeassistant',
      color: 'bg-yellow-600'
    }
  ];

  return (
    <div className="space-y-8">
      {/* Hero Section */}
      <div className="text-center">
        <h1 className="text-4xl font-bold text-white mb-4">
          Willkommen bei Renfield
        </h1>
        <p className="text-xl text-gray-400 mb-8">
          Dein vollständig offline-fähiger KI-Assistent für Smart Home
        </p>
        
        {/* System Status */}
        <div className="inline-flex items-center space-x-2 px-4 py-2 bg-gray-800 rounded-lg">
          <Activity className={`w-5 h-5 ${loading ? 'text-yellow-500' : 'text-green-500'}`} />
          <span className="text-gray-300">
            {loading ? 'Prüfe System...' : 'System Online'}
          </span>
        </div>
      </div>

      {/* Features Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {features.map((feature) => {
          const Icon = feature.icon;
          return (
            <Link
              key={feature.name}
              to={feature.href}
              className="card hover:scale-105 transition-transform cursor-pointer"
            >
              <div className="flex items-start space-x-4">
                <div className={`${feature.color} p-3 rounded-lg`}>
                  <Icon className="w-6 h-6 text-white" />
                </div>
                <div className="flex-1">
                  <h3 className="text-lg font-semibold text-white mb-1">
                    {feature.name}
                  </h3>
                  <p className="text-gray-400">
                    {feature.description}
                  </p>
                </div>
              </div>
            </Link>
          );
        })}
      </div>

      {/* Quick Stats */}
      {health?.services && (
        <div className="card">
          <h2 className="text-xl font-semibold text-white mb-4">System Status</h2>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
            <div className="text-center">
              <p className="text-sm text-gray-400 mb-1">Ollama</p>
              <p className={`text-lg font-semibold ${
                health.services.ollama === 'ok' ? 'text-green-500' : 'text-red-500'
              }`}>
                {health.services.ollama === 'ok' ? '✓ Online' : '✗ Offline'}
              </p>
            </div>
            <div className="text-center">
              <p className="text-sm text-gray-400 mb-1">Datenbank</p>
              <p className={`text-lg font-semibold ${
                health.services.database === 'ok' ? 'text-green-500' : 'text-red-500'
              }`}>
                {health.services.database === 'ok' ? '✓ Online' : '✗ Offline'}
              </p>
            </div>
            <div className="text-center">
              <p className="text-sm text-gray-400 mb-1">Redis</p>
              <p className={`text-lg font-semibold ${
                health.services.redis === 'ok' ? 'text-green-500' : 'text-red-500'
              }`}>
                {health.services.redis === 'ok' ? '✓ Online' : '✗ Offline'}
              </p>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

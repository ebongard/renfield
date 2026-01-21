import axios from 'axios';

// Axios Instance mit Base URL
const apiClient = axios.create({
  baseURL: import.meta.env.VITE_API_URL || 'http://localhost:8000',
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  }
});

// Request Interceptor
apiClient.interceptors.request.use(
  (config) => {
    // Hier könnte Auth-Token hinzugefügt werden
    return config;
  },
  (error) => {
    return Promise.reject(error);
  }
);

// Response Interceptor
apiClient.interceptors.response.use(
  (response) => {
    return response;
  },
  (error) => {
    // Globale Error-Behandlung
    console.error('API Error:', error);
    return Promise.reject(error);
  }
);

export default apiClient;

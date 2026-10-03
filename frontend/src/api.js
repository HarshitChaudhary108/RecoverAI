const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';

async function apiFetch(endpoint) {
  const response = await fetch(`${API_BASE_URL}${endpoint}`);
  if (!response.ok) {
    throw new Error(`API Error: ${response.status} ${response.statusText}`);
  }
  return response.json();
}

export const fetchSummary = () => apiFetch('/api/stats/summary');
export const fetchTimeSeries = (hours = 24) => apiFetch(`/api/stats/timeseries?hours=${hours}`);
export const fetchFailureReasons = (hours = 24) => apiFetch(`/api/stats/failure-reasons?hours=${hours}`);
export const fetchByBank = (hours = 1) => apiFetch(`/api/stats/by-bank?hours=${hours}`);
export const fetchByMethod = (hours = 1) => apiFetch(`/api/stats/by-method?hours=${hours}`);
export const fetchFunnel = (hours = 24) => apiFetch(`/api/recovery/funnel?hours=${hours}`);
export const fetchAlerts = () => apiFetch('/api/alerts');

import React, { useState, useCallback } from 'react';
import { usePolling } from './usePolling';
import {
  fetchSummary,
  fetchTimeSeries,
  fetchFailureReasons,
  fetchByBank,
  fetchByMethod,
  fetchFunnel,
  fetchAlerts
} from './api';
import { formatCurrency, formatDateTime, formatPercent } from './format';

import StatCard from './components/StatCard';
import SuccessRateChart from './components/SuccessRateChart';
import FailureReasonsChart from './components/FailureReasonsChart';
import HealthTable from './components/HealthTable';
import RecoveryFunnel from './components/RecoveryFunnel';
import TreatedVsHoldout from './components/TreatedVsHoldout';
import AlertFeed from './components/AlertFeed';

export default function App() {
  const [data, setData] = useState({
    summary: null,
    timeseries: [],
    failureReasons: [],
    byBank: [],
    byMethod: [],
    funnel: null,
    alerts: [],
  });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [lastUpdated, setLastUpdated] = useState(null);

  const loadData = useCallback(async () => {
    try {
      const [summary, timeseries, failureReasons, byBank, byMethod, funnel, alerts] = await Promise.all([
        fetchSummary(),
        fetchTimeSeries(),
        fetchFailureReasons(),
        fetchByBank(),
        fetchByMethod(),
        fetchFunnel(),
        fetchAlerts(),
      ]);

      setData({
        summary,
        timeseries,
        failureReasons,
        byBank,
        byMethod,
        funnel,
        alerts,
      });
      setLastUpdated(new Date());
      setError(null);
    } catch (err) {
      console.error("Dashboard fetch error:", err);
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  usePolling(loadData, 30000);

  if (loading) {
    return (
      <div className="dashboard-container">
        <div className="loading-overlay">
          <h2>Loading Recovery Dashboard...</h2>
        </div>
      </div>
    );
  }

  return (
    <div className="dashboard-container">
      <header className="header">
        <h1>Payment Recovery Dashboard</h1>
        <div className="last-updated">
          Last Updated: {lastUpdated ? formatDateTime(lastUpdated) : 'Never'}
        </div>
      </header>

      {error && <div className="error-banner"><strong>Error:</strong> {error}</div>}

      <div className="kpi-grid">
        <StatCard
          label="Overall Success Rate"
          value={data.summary ? formatPercent(data.summary.success_rate) : 'N/A'}
        />
        <StatCard
          label="Failed Payments"
          value={data.summary ? data.summary.failed_count : 'N/A'}
        />
        <StatCard
          label="Recovered Payments"
          value={data.summary ? data.summary.recovered_count : 'N/A'}
        />
        <StatCard
          label="Revenue Recovered"
          value={data.summary ? formatCurrency(data.summary.revenue_recovered) : 'N/A'}
        />
      </div>

      <div className="grid-row">
        <SuccessRateChart data={data.timeseries} />
        <FailureReasonsChart data={data.failureReasons} />
      </div>

      <div className="grid-row">
        <HealthTable title="Payment Health by Bank" data={data.byBank} />
        <HealthTable title="Payment Health by Method" data={data.byMethod} />
      </div>

      <div className="grid-row">
        <RecoveryFunnel data={data.funnel} />
        <TreatedVsHoldout data={data.funnel} />
      </div>

      <AlertFeed data={data.alerts} />
    </div>
  );
}

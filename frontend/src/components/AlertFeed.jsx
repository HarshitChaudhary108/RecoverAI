import React from 'react';
import { formatDateTime, formatPercent } from '../format';

export default function AlertFeed({ data }) {
  if (!data || data.length === 0) return <div className="card"><div className="stat-label" style={{ textAlign: 'left', marginBottom: '1rem' }}>Health Alerts</div><div className="loading-overlay">All systems healthy</div></div>;

  return (
    <div className="card">
      <div className="stat-label" style={{ textAlign: 'left', marginBottom: '1rem' }}>Active Health Alerts</div>
      <div className="alert-list">
        <div className="alert-item" style={{ fontWeight: '600', borderBottom: '2px solid var(--border)' }}>
          <span>Scope</span>
          <span>Value</span>
          <span>Current Rate</span>
          <span>Baseline</span>
          <span>State</span>
        </div>
        {data.map((alert, i) => (
          <div key={i} className="alert-item">
            <span>{alert.scope}</span>
            <span>{alert.scope_value}</span>
            <span>{formatPercent(alert.current_success_rate)}</span>
            <span>{formatPercent(alert.baseline)}</span>
            <span>
              <span className={`status-pill ${alert.state === 'ok' ? 'status-ok' : 'status-alerting'}`}>
                {alert.state.toUpperCase()}
              </span>
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

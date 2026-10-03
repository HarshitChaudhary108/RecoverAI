import React from 'react';
import { formatPercent } from '../format';

export default function TreatedVsHoldout({ data }) {
  if (!data) return <div className="loading-overlay">No comparison data available</div>;

  return (
    <div className="card">
      <div className="stat-label" style={{ textAlign: 'left', marginBottom: '1rem' }}>Treatment vs Holdout Comparison</div>
      <div className="comparison-grid">
        <div className="comp-card">
          <div className="stat-label">Treatment Group</div>
          <div className="stat-value" style={{ color: 'var(--primary)' }}>
            {formatPercent(data.treatment_rate)}
          </div>
          <div className="last-updated">n = {data.treatment_count}</div>
        </div>
        <div className="comp-card">
          <div className="stat-label">Holdout Group</div>
          <div className="stat-value" style={{ color: 'var(--text-muted)' }}>
            {formatPercent(data.holdout_rate)}
          </div>
          <div className="last-updated">n = {data.holdout_count}</div>
        </div>
      </div>
    </div>
  );
}

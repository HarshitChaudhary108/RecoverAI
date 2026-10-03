import React from 'react';
import { formatPercent } from '../format';

export default function HealthTable({ title, data }) {
  if (!data || data.length === 0) return <div className="card"><div className="stat-label" style={{ textAlign: 'left' }}>{title}</div><div className="loading-overlay">No health data available</div></div>;

  const getRateColor = (rate) => {
    if (rate === null || rate === undefined) return 'var(--text-main)';
    if (rate < 0.8) return 'var(--danger)';
    if (rate < 0.95) return 'var(--warning)';
    return 'var(--success)';
  };

  return (
    <div className="card">
      <div className="stat-label" style={{ textAlign: 'left', marginBottom: '1rem' }}>{title}</div>
      <div className="table-container">
        <table>
          <thead>
            <tr>
              <th>Entity</th>
              <th>Attempts</th>
              <th>Captured</th>
              <th>Failed</th>
              <th>Success Rate</th>
            </tr>
          </thead>
          <tbody>
            {data.map((row, i) => (
              <tr key={i}>
                <td>{row.entity}</td>
                <td>{row.attempts}</td>
                <td>{row.captured}</td>
                <td>{row.failed}</td>
                <td style={{ fontWeight: '600', color: getRateColor(row.success_rate) }}>
                  {formatPercent(row.success_rate)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

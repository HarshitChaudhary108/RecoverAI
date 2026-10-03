import React from 'react';

export default function StatCard({ label, value, color = 'var(--text-main)' }) {
  return (
    <div className="card stat-card">
      <div className="stat-label">{label}</div>
      <div className="stat-value" style={{ color }}>{value}</div>
    </div>
  );
}

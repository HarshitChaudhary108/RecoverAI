import React from 'react';

export default function RecoveryFunnel({ data }) {
  if (!data) return <div className="loading-overlay">No funnel data available</div>;

  const steps = [
    { label: 'Total Failed', value: data.failed },
    { label: 'Eligible for Recovery', value: data.eligible },
    { label: 'Recovery Emails Sent', value: data.emails_sent },
    { label: 'Payments Recovered', value: data.recovered },
  ];

  return (
    <div className="card">
      <div className="stat-label" style={{ textAlign: 'left', marginBottom: '1rem' }}>Recovery Funnel</div>
      <div className="funnel-container">
        {steps.map((step, i) => (
          <div key={i} className="funnel-step">
            <span className="funnel-label">{step.label}</span>
            <span className="funnel-value">{step.value}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

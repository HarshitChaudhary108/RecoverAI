import React from 'react';
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts';
import { formatDateTime, formatPercent } from '../format';

export default function SuccessRateChart({ data }) {
  if (!data || data.length === 0) return <div className="loading-overlay">No time-series data available</div>;

  return (
    <div className="card">
      <div className="stat-label" style={{ textAlign: 'left', marginBottom: '1rem' }}>Success Rate Over Time</div>
      <div className="chart-container">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data}>
            <CartesianGrid strokeDasharray="3 3" vertical={false} />
            <XAxis
              dataKey="timestamp"
              tickFormatter={(val) => {
                const d = new Date(val);
                return `${d.getHours()}:${d.getMinutes().toString().padStart(2, '0')}`;
              }}
              fontSize={12}
            />
            <YAxis
              tickFormatter={(val) => `${(val * 100).toFixed(0)}%`}
              fontSize={12}
              domain={[0, 1]}
            />
            <Tooltip
              labelFormatter={formatDateTime}
              formatter={(value) => [formatPercent(value), 'Success Rate']}
            />
            <Line
              type="monotone"
              dataKey="success_rate"
              stroke="var(--primary)"
              strokeWidth={2}
              dot={false}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

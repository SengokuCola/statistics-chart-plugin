import React from "react";
import { createRoot } from "react-dom/client";
import {
  Bar,
  CartesianGrid,
  Cell,
  ComposedChart,
  Legend,
  Line,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

const theme = {
  background: "#f3e3cc",
  card: "#f3e3cc",
  border: "#0d4650",
  foreground: "#0a4550",
  muted: "rgba(13, 70, 80, 0.68)",
  grid: "rgba(13, 70, 80, 0.24)",
  chart: ["#c24d24", "#0a4550", "#c99a3e", "#386641", "#7f4f24", "#2f6f6f", "#6d597a", "#173f46"],
};

const RADIAN = Math.PI / 180;

function formatValue(value) {
  if (typeof value !== "number") {
    return value;
  }
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value);
}

function ChartTooltip({ active, payload, label }) {
  if (!active || !payload?.length) {
    return null;
  }
  return (
    <div className="chart-tooltip">
      <div className="tooltip-label">{label}</div>
      {payload.map((item) => (
        <div className="tooltip-row" key={`${item.name}-${item.dataKey}`}>
          <span className="tooltip-dot" style={{ backgroundColor: item.color }} />
          <span>{item.name}</span>
          <strong>{formatValue(item.value)}</strong>
        </div>
      ))}
    </div>
  );
}

function formatPieLabelName(name) {
  const text = String(name || "");
  if (text.length <= 14) {
    return text;
  }
  return `${text.slice(0, 11)}...`;
}

function renderPieLabel({ cx, cy, midAngle, outerRadius, name, percent, fill }) {
  const labelRadius = Number(outerRadius || 0) + 12;
  const x = Number(cx || 0) + labelRadius * Math.cos(-midAngle * RADIAN);
  const y = Number(cy || 0) + labelRadius * Math.sin(-midAngle * RADIAN);
  const textAnchor = x > Number(cx || 0) ? "start" : "end";

  return (
    <text
      className="recharts-pie-label-text"
      dominantBaseline="central"
      fill={fill || theme.foreground}
      textAnchor={textAnchor}
      x={x}
      y={y}
    >
      {`${formatPieLabelName(name)} ${(percent * 100).toFixed(1)}%`}
    </text>
  );
}

function PieGrid({ spec }) {
  const pieCount = (spec.pies || []).length;

  return (
    <main className="page">
      <section className="card">
        <header className="header">
          <div>
            <h1>{spec.title}</h1>
            {spec.description ? <p>{spec.description}</p> : null}
          </div>
        </header>
        <div className={`pie-grid pie-count-${Math.min(pieCount, 6)}`}>
          {(spec.pies || []).map((pie, pieIndex) => (
            <section className="pie-panel" key={pie.title || pieIndex}>
              <h2>{pie.title}</h2>
              <div className="pie-chart-wrap">
                <ResponsiveContainer width="100%" height="100%">
                  <PieChart margin={{ top: 24, right: 96, bottom: 34, left: 96 }}>
                    <Tooltip content={<ChartTooltip />} />
                    <Legend wrapperStyle={{ color: theme.foreground, fontSize: 18, fontWeight: 800 }} />
                    <Pie
                      data={pie.data || []}
                      dataKey="value"
                      cx={pieCount === 2 ? (pieIndex === 0 ? "60%" : "40%") : "50%"}
                      cy="46%"
                      isAnimationActive={false}
                      label={renderPieLabel}
                      labelLine={false}
                      nameKey="name"
                      outerRadius="82%"
                      stroke={theme.background}
                      strokeWidth={2}
                    >
                      {(pie.data || []).map((entry, index) => (
                        <Cell key={`${entry.name}-${index}`} fill={theme.chart[index % theme.chart.length]} />
                      ))}
                    </Pie>
                  </PieChart>
                </ResponsiveContainer>
              </div>
            </section>
          ))}
        </div>
      </section>
    </main>
  );
}

function ComposedStatsChart({ spec }) {
  const series = spec.series || [];
  return (
    <main className="page">
      <section className="card">
        <header className="header">
          <div>
            <h1>{spec.title}</h1>
            {spec.description ? <p>{spec.description}</p> : null}
          </div>
        </header>
        <div className="chart-wrap">
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={spec.data || []} margin={{ top: 16, right: 28, bottom: 24, left: 12 }}>
              <CartesianGrid strokeDasharray="4 6" stroke={theme.grid} />
              <XAxis
                dataKey={spec.xKey || "label"}
                angle={spec.xTickAngle || 0}
                height={spec.xTickAngle ? 72 : 44}
                interval="preserveStartEnd"
                stroke={theme.muted}
                textAnchor={spec.xTickAngle ? "end" : "middle"}
                tick={{ fill: theme.muted, fontSize: 12, fontWeight: 700 }}
                tickLine={{ stroke: theme.border, strokeWidth: 1.25 }}
              />
              <YAxis
                domain={spec.yDomain || undefined}
                yAxisId="left"
                stroke={theme.muted}
                tick={{ fill: theme.muted, fontSize: 12, fontWeight: 700 }}
                tickFormatter={formatValue}
                tickLine={{ stroke: theme.border, strokeWidth: 1.25 }}
              />
              {series.some((item) => item.yAxisId === "right") ? (
                <YAxis
                  yAxisId="right"
                  orientation="right"
                  stroke={theme.muted}
                  tick={{ fill: theme.muted, fontSize: 12, fontWeight: 700 }}
                  tickFormatter={formatValue}
                  tickLine={{ stroke: theme.border, strokeWidth: 1.25 }}
                />
              ) : null}
              <Tooltip content={<ChartTooltip />} />
              <Legend wrapperStyle={{ color: theme.foreground, fontSize: 15, fontWeight: 800, paddingTop: 8 }} />
              {series.map((item, index) => {
                const color = item.color || theme.chart[index % theme.chart.length];
                if (item.type === "bar") {
                  return (
                    <Bar
                      key={item.key}
                      dataKey={item.key}
                      fill={color}
                      isAnimationActive={false}
                      name={item.label || item.key}
                      opacity={item.opacity ?? 0.78}
                      radius={[0, 0, 0, 0]}
                      yAxisId={item.yAxisId || "left"}
                    />
                  );
                }
                return (
                  <Line
                    key={item.key}
                    dataKey={item.key}
                    dot={item.dot ?? false}
                    isAnimationActive={false}
                    name={item.label || item.key}
                    stroke={color}
                    strokeWidth={item.strokeWidth || 2.4}
                    type="monotone"
                    yAxisId={item.yAxisId || "left"}
                  />
                );
              })}
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      </section>
    </main>
  );
}

function SummaryCard({ spec }) {
  return (
    <main className="page">
      <section className="card">
        <header className="header">
          <div>
            <h1>{spec.title}</h1>
            {spec.description ? <p>{spec.description}</p> : null}
          </div>
        </header>
        <div className="summary-wrap">
          {(spec.sections || []).map((section, sectionIndex) => (
            <section
              className={`summary-section${section.layout === "commands" ? " summary-section-commands" : ""}`}
              key={section.title || sectionIndex}
            >
              <h2>{section.title}</h2>
              <div className="summary-rows">
                {(section.rows || []).map((row, rowIndex) => (
                  <div className="summary-row" key={`${row.label}-${rowIndex}`}>
                    <span>{row.label}</span>
                    <strong>{row.value}</strong>
                  </div>
                ))}
              </div>
            </section>
          ))}
        </div>
      </section>
    </main>
  );
}

function ChartCard({ spec }) {
  if (spec.kind === "summary-card") {
    return <SummaryCard spec={spec} />;
  }
  if (spec.kind === "pie-grid") {
    return <PieGrid spec={spec} />;
  }
  return <ComposedStatsChart spec={spec} />;
}

function App() {
  return <ChartCard spec={window.__MAIBOT_CHART_SPEC__ || {}} />;
}

createRoot(document.getElementById("root")).render(<App />);
const waitForFonts = document.fonts?.ready || Promise.resolve();
waitForFonts.then(() =>
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      window.__MAIBOT_CHART_READY__ = true;
    });
  })
);

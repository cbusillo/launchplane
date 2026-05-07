import type { ReactNode } from "react";

import type { Status } from "./types";

export function PanelHead({
  eyebrow,
  title,
  right,
}: {
  eyebrow: string;
  title: string;
  right?: ReactNode;
}) {
  return (
    <div className="panel-head">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h2>{title}</h2>
      </div>
      {right ? <div className="panel-right">{right}</div> : null}
    </div>
  );
}

export function MetricTile({
  label,
  status,
  value,
}: {
  label: string;
  status: Status | string;
  value: string;
}) {
  return (
    <div className="metric-tile" data-status={status}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

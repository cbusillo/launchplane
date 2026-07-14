import { Boxes, GitPullRequestArrow, Network, Wrench } from "lucide-react";

import { AppLink, productIndexPath } from "./router";

const ENGINEERING_SURFACES = [
  {
    icon: Network,
    title: "Work graph",
    detail: "Prioritization, dependency state, and safe-to-start evidence.",
  },
  {
    icon: GitPullRequestArrow,
    title: "Issue reconciliation",
    detail: "GitHub issue authority and project consistency checks.",
  },
  {
    icon: Boxes,
    title: "Every Code",
    detail: "Agent work requests, claims, and completion evidence.",
  },
  {
    icon: Wrench,
    title: "Merge train and platform maintenance",
    detail: "Repository delivery controls and Launchplane maintenance.",
  },
];

export function EngineeringOpsBoundary() {
  return (
    <section className="engineering-boundary">
      <div className="route-heading">
        <div>
          <p className="eyebrow">Engineering Ops</p>
          <h1 data-route-heading tabIndex={-1}>
            Platform delivery systems
          </h1>
          <p>
            Engineering work is intentionally separated from product operation so
            fleet queues and delivery controls do not obscure product health.
          </p>
        </div>
        <span className="boundary-mark" aria-hidden="true">
          <Wrench />
        </span>
      </div>

      <div className="boundary-callout">
        <strong>Workspace boundary established</strong>
        <p>
          These surfaces remain unavailable in the new shell until their supported
          controls and evidence states are rebuilt here. Product Ops does not load
          or infer engineering queue state.
        </p>
      </div>

      <ul className="engineering-surface-list">
        {ENGINEERING_SURFACES.map(({ icon: Icon, title, detail }) => (
          <li key={title}>
            <Icon size={18} aria-hidden="true" />
            <span>
              <strong>{title}</strong>
              <small>{detail}</small>
            </span>
            <em>Not yet available</em>
          </li>
        ))}
      </ul>

      <AppLink className="button" to={productIndexPath()}>
        Return to Product Ops
      </AppLink>
    </section>
  );
}

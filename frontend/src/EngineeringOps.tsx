import {
  ArrowRight,
  Boxes,
  GitPullRequestArrow,
  Network,
  ShieldCheck,
  Wrench,
} from "lucide-react";

import { EngineeringEveryCodeRoute } from "./EngineeringEveryCodeRoute";
import { EngineeringIssueInboxRoute } from "./EngineeringIssueInboxRoute";
import { EngineeringMergeTrainRoute } from "./EngineeringMergeTrainRoute";
import { EngineeringTenantAdmissionRoute } from "./EngineeringTenantAdmissionRoute";
import { EngineeringRouteFrame } from "./EngineeringRouteUi";
import { EngineeringWorkGraphRoute } from "./EngineeringWorkGraphRoute";
import {
  AppLink,
  engineeringPath,
  type EngineeringView,
} from "./router";

import type { DevFixtureMode } from "./dev-fixture-loader";

const ENGINEERING_SURFACES = [
  {
    detail:
      "Rank the current Launchplane-assembled snapshot and inspect recommendation evidence.",
    icon: Network,
    label: "Read + rank",
    title: "Work graph",
    view: "work-graph" as const,
  },
  {
    detail:
      "Inspect explicit repository inventory and Code Plans membership without browser reconciliation.",
    icon: GitPullRequestArrow,
    label: "Read only",
    title: "Issue inbox",
    view: "issue-inbox" as const,
  },
  {
    detail:
      "Review bounded work-request summaries, provenance, active claims, and completion evidence.",
    icon: Boxes,
    label: "Read only",
    title: "Every Code",
    view: "every-code" as const,
  },
  {
    detail:
      "Read policy targets, controller state, lease diagnostics, and durable train records.",
    icon: Wrench,
    label: "Status only",
    title: "Merge train",
    view: "merge-train" as const,
  },
  {
    detail:
      "Inspect exact-head tenant classification, human admission paths, mergeability, and required technical checks.",
    icon: ShieldCheck,
    label: "Read only",
    title: "Tenant admission",
    view: "tenant-admission" as const,
  },
];

export function EngineeringOpsRoute({
  fixtureMode,
  view,
}: {
  fixtureMode: DevFixtureMode;
  view: EngineeringView;
}) {
  if (view === "work-graph") {
    return <EngineeringWorkGraphRoute fixtureMode={fixtureMode} />;
  }
  if (view === "issue-inbox") {
    return <EngineeringIssueInboxRoute fixtureMode={fixtureMode} />;
  }
  if (view === "every-code") {
    return <EngineeringEveryCodeRoute fixtureMode={fixtureMode} />;
  }
  if (view === "merge-train") {
    return <EngineeringMergeTrainRoute fixtureMode={fixtureMode} />;
  }
  if (view === "tenant-admission") {
    return <EngineeringTenantAdmissionRoute fixtureMode={fixtureMode} />;
  }
  return <EngineeringOpsHub />;
}

function EngineeringOpsHub() {
  return (
    <EngineeringRouteFrame
      description="Platform delivery evidence is separated from product health so operators can investigate queues, automation, and repository control without obscuring live products."
      icon={Wrench}
      title="Platform delivery systems"
      view="hub"
    >
      <div className="engineering-hub-intro">
        <strong>Five independent evidence routes</strong>
        <p>
          Each surface owns its own request lifecycle, direct link, stale-data
          disclosure, refresh failure, and cancellation state. Browser controls
          appear only where the generated UI contract supports them.
        </p>
      </div>
      <div className="engineering-hub-grid">
        {ENGINEERING_SURFACES.map(({ detail, icon: Icon, label, title, view }) => (
          <AppLink
            className="engineering-hub-card"
            key={view}
            to={engineeringPath(view)}
          >
            <span className="engineering-hub-icon">
              <Icon size={21} aria-hidden="true" />
            </span>
            <span className="engineering-kicker">{label}</span>
            <strong>{title}</strong>
            <p>{detail}</p>
            <span className="engineering-hub-link">
              Open evidence
              <ArrowRight size={15} aria-hidden="true" />
            </span>
          </AppLink>
        ))}
      </div>
    </EngineeringRouteFrame>
  );
}

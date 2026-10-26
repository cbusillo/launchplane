import { EngineeringRuntimeSettingRemovalRoute } from "./EngineeringRuntimeSettingRemovalRoute";
import { CredentialsRoute } from "./CredentialsRoute";
import {
  ArrowRight,
  KeyRound,
  ShieldCheck,
  Wrench,
} from "lucide-react";

import { EngineeringMergeTrainRoute } from "./EngineeringMergeTrainRoute";
import { EngineeringGovernanceProjectionRoute } from "./EngineeringGovernanceProjectionRoute";
import { EngineeringPrivilegedOperationsRoute } from "./EngineeringPrivilegedOperationsRoute";
import { EngineeringTenantAdmissionRoute } from "./EngineeringTenantAdmissionRoute";
import { EngineeringRouteFrame } from "./EngineeringRouteUi";
import { EngineeringGitHubDeliveryRoute } from "./EngineeringGitHubDeliveryRoute";
import {
  AppLink,
  engineeringPath,
  type EngineeringView,
} from "./router";

import type { DevFixtureMode } from "./dev-fixture-loader";

const ENGINEERING_SURFACES = [
  { detail: "Read credential names, source coverage, collection freshness and drift.",
    icon: KeyRound, label: "Director controls", title: "Credentials", view: "credentials" as const },
  { detail: "Review exact stored plain-setting removals and approve them as the signed-in administrator.",
    icon: KeyRound, label: "Director controls", title: "Remove stored settings", view: "runtime-setting-removal" as const },
  {
    detail: "Select the service's existing Delivery App key and review obsolete service-token retirement.",
    icon: KeyRound,
    label: "Director controls",
    title: "GitHub delivery",
    view: "github-delivery" as const,
  },
  {
    detail:
      "Inspect current readiness, immutable admission, landing outcome, and GitHub observations.",
    icon: ShieldCheck,
    label: "Read only",
    title: "Governance evidence",
    view: "governance-projection" as const,
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
      "Inspect tenant classification, mergeability, and required technical checks for the current revision.",
    icon: ShieldCheck,
    label: "Read only",
    title: "Tenant admission",
    view: "tenant-admission" as const,
  },
  {
    detail:
      "Inspect typed, redacted privileged-operation plans without approval, execution, or credential custody.",
    icon: KeyRound,
    label: "Planning only",
    title: "Privileged operation plans",
    view: "privileged-operations" as const,
  },
];

export function EngineeringOpsRoute({
  admin,
  fixtureMode,
  view,
  actorId,
}: {
  admin: boolean;
  fixtureMode: DevFixtureMode;
  view: EngineeringView;
  actorId: number;
}) {
  if (view === "credentials") return <CredentialsRoute key={actorId} admin={admin} />;
  if (view === "runtime-setting-removal") return <EngineeringRuntimeSettingRemovalRoute admin={admin} actorId={actorId} />;
  if (view === "github-delivery") {
    return <EngineeringGitHubDeliveryRoute actorId={actorId} />;
  }
  if (view === "merge-train") {
    return <EngineeringMergeTrainRoute fixtureMode={fixtureMode} />;
  }
  if (view === "tenant-admission") {
    return <EngineeringTenantAdmissionRoute fixtureMode={fixtureMode} />;
  }
  if (view === "governance-projection") {
    return <EngineeringGovernanceProjectionRoute fixtureMode={fixtureMode} />;
  }
  if (view === "privileged-operations") {
    return <EngineeringPrivilegedOperationsRoute admin={admin} fixtureMode={fixtureMode} />;
  }
  return <EngineeringOpsHub />;
}

function EngineeringOpsHub() {
  return (
    <EngineeringRouteFrame
      description="Platform delivery evidence is separated from product health so admins can investigate queues, automation, and repository control without obscuring live products."
      icon={Wrench}
      title="Platform delivery systems"
      view="hub"
    >
      <div className="engineering-hub-intro">
        <strong>Delivery evidence routes</strong>
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

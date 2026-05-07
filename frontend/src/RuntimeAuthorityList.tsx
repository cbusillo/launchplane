import { KeyRound } from "lucide-react";

import { PanelHead } from "./panel-ui";
import { StateBlock, StatusPill } from "./status-ui";

import type { DriverDescriptor, LaneSummary } from "./types";

export function RuntimeAuthorityList({
  driver,
  lane,
}: {
  driver: DriverDescriptor | null;
  lane: LaneSummary | null;
}) {
  const bindingHints =
    driver?.setting_groups.flatMap((group) => {
      return group.secret_bindings.map((binding) => ({ group, binding }));
    }) ?? [];
  const actualBindings = lane?.secret_bindings ?? [];
  const runtimeRecords = lane?.runtime_environment_records ?? [];

  return (
    <section className="panel">
      <PanelHead
        eyebrow="current state"
        title="Current runtime authority"
        right={<KeyRound size={17} aria-hidden="true" />}
      />
      <div className="secret-list">
        {runtimeRecords.map((record) => {
          const envKeys = Object.keys(record.env).sort();
          return (
            <div
              className="secret-row"
              key={`${record.scope}:${record.context}:${record.instance}:${record.updated_at}`}
            >
              <span className="lane-chip lane-chip-prod">
                {record.scope === "global"
                  ? "global"
                  : record.instance || record.context}
              </span>
              <strong>{envKeys.join(", ") || "no keys"}</strong>
              <span>{record.source_label || "runtime environment"}</span>
              <StatusPill status={envKeys.length ? "pass" : "unknown"} />
            </div>
          );
        })}
      </div>
      {actualBindings.length ? (
        <div className="secret-list">
          {actualBindings.map((binding) => (
            <div className="secret-row" key={binding.binding_id}>
              <span className="lane-chip lane-chip-prod">
                {binding.instance || binding.context || "global"}
              </span>
              <strong>{binding.binding_key}</strong>
              <span>{binding.integration}</span>
              <StatusPill
                status={binding.status === "configured" ? "pass" : "blocked"}
              />
            </div>
          ))}
        </div>
      ) : (
        <div className="secret-list">
          {bindingHints.map(({ group, binding }) => (
            <div className="secret-row" key={`${group.group_id}:${binding}`}>
              <span className="lane-chip lane-chip-prod">{group.scope}</span>
              <strong>{binding}</strong>
              <span>{group.label}</span>
              <StatusPill status="unknown" />
            </div>
          ))}
          {!bindingHints.length ? (
            <StateBlock
              icon={<KeyRound size={18} />}
              title={
                runtimeRecords.length
                  ? "No secret binding metadata"
                  : "No runtime metadata"
              }
            />
          ) : null}
        </div>
      )}
    </section>
  );
}

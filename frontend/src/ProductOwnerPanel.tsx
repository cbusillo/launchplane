import { Eye, LoaderCircle, RotateCcw, Save, UserCheck, UserX } from "lucide-react";
import { useEffect, useState } from "react";

import { applyProductImageRepository, applyProductProductionUse, applyProductOwner, LaunchplaneApiError, readProductProfile } from "./api";
import type { BrowserOperationFailureCertainty } from "./browser-operation";
import { loadDevFixtures, type DevFixtureMode } from "./dev-fixture-loader";
import {
  productConfigFailureCertainty,
  productConfigOperationFailure,
} from "./product-config-operation";
import {
  InlineFormError,
  isOperationBusy,
  OperationNotice,
  ReasonField,
} from "./ProductConfigForms";
import {
  NO_PRODUCT_OWNER,
  normalizeOwnerLoginInput,
  ownerLoginInputError,
  productOwnerDraftKey,
  productOwnerFromRecord,
  productOwnerLabel,
  productOwnerPlanFromResponse,
  productOwnerPlanSummary,
  type ProductOwnerIdentity,
  type ProductOwnerPlan,
} from "./product-owner-operation";
import { useBrowserOperationController } from "./use-browser-operation";

import type { AcceptedEvidenceResponse, ApplyProductImageRepositoryData, ApplyProductProductionUseData, ApplyProductOwnerData } from "./generated/openapi.ts";

type ProductOwnerRequest = ApplyProductOwnerData["body"];

interface OwnerResource {
  error: string;
  owner: ProductOwnerIdentity;
  status: "loading" | "ready" | "error";
}

export function ProductOwnerPanel({
  fixtureMode,
  product,
}: {
  fixtureMode: DevFixtureMode;
  product: string;
}) {
  const [resource, setResource] = useState<OwnerResource>({
    error: "",
    owner: NO_PRODUCT_OWNER,
    status: "loading",
  });
  const [login, setLogin] = useState("");
  const [reason, setReason] = useState("");
  const [localError, setLocalError] = useState("");
  const [plan, setPlan] = useState<ProductOwnerPlan | null>(null);
  const [plannedDraft, setPlannedDraft] = useState<{ clear: boolean; key: string } | null>(null);
  const [saved, setSaved] = useState(false);
  const planOperation = useProductOwnerOperation(`${product}:owner:plan`, product, fixtureMode);
  const applyOperation = useProductOwnerOperation(`${product}:owner:apply`, product, fixtureMode);
  const busy = isOperationBusy(planOperation.state) || isOperationBusy(applyOperation.state);
  const locked = busy || applyOperation.state.requiresIdempotencyContinuity;
  const planMatchesDraft = Boolean(
    plan &&
      plannedDraft &&
      plannedDraft.key === productOwnerDraftKey(login, plannedDraft.clear),
  );

  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    setResource({ error: "", owner: NO_PRODUCT_OWNER, status: "loading" });
    const read = fixtureMode
      ? loadDevFixtures().then((fixtures) => fixtures.productOwnerForFixture(fixtureMode, product))
      : readProductProfile(product, controller.signal).then((payload) => payload.profile.owner);
    read
      .then((owner) => {
        if (active) {
          setResource({ error: "", owner: productOwnerFromRecord(owner), status: "ready" });
        }
      })
      .catch((error: unknown) => {
        if (!active || controller.signal.aborted) {
          return;
        }
        const denied = error instanceof LaunchplaneApiError && error.statusCode === 403;
        setResource({
          error: denied
            ? "This session cannot read the product's Client."
            : error instanceof Error
              ? error.message
              : "Launchplane could not read the product's Client.",
          owner: NO_PRODUCT_OWNER,
          status: "error",
        });
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [fixtureMode, product]);

  function clearPlan() {
    setPlan(null);
    setPlannedDraft(null);
    setSaved(false);
  }

  async function preview(clear: boolean) {
    setLocalError("");
    const loginError = clear ? "" : ownerLoginInputError(login);
    if (loginError) {
      setLocalError(loginError);
      return;
    }
    if (!reason.trim()) {
      setLocalError("Enter a reason before previewing the change.");
      return;
    }
    if (applyOperation.state.requiresIdempotencyContinuity) {
      setLocalError("Resolve the uncertain save before previewing another change.");
      return;
    }
    applyOperation.reset();
    clearPlan();
    const response = await planOperation.run(ownerRequest("dry-run", clear));
    const nextPlan = response ? productOwnerPlanFromResponse(response) : null;
    if (response && !nextPlan) {
      setLocalError("Launchplane returned a preview this page cannot read.");
      return;
    }
    if (nextPlan) {
      setPlan(nextPlan);
      setPlannedDraft({ clear, key: productOwnerDraftKey(login, clear) });
    }
  }

  async function save() {
    setLocalError("");
    if (!plan || !plannedDraft || !planMatchesDraft) {
      setLocalError("The login changed after the preview. Preview the change again.");
      return;
    }
    const response = await applyOperation.run(ownerRequest("apply", plannedDraft.clear));
    const appliedPlan = response ? productOwnerPlanFromResponse(response) : null;
    if (appliedPlan) {
      setResource({ error: "", owner: appliedPlan.after, status: "ready" });
      setPlan(appliedPlan);
      setSaved(true);
      setLogin("");
    }
  }

  function ownerRequest(mode: "dry-run" | "apply", clear: boolean): ProductOwnerRequest {
    return clear
      ? { schema_version: 1, mode, clear: true, reason: reason.trim() }
      : {
          schema_version: 1,
          mode,
          github_login: normalizeOwnerLoginInput(login),
          reason: reason.trim(),
        };
  }

  function startOver() {
    if (!planOperation.reset() || !applyOperation.reset()) {
      setLocalError("An uncertain save must be retried with its existing operation key.");
      return;
    }
    setLogin("");
    setReason("");
    setLocalError("");
    clearPlan();
  }

  const ownerSet = Boolean(resource.owner.githubId);

  return (
    <>
    <section className="product-config-panel product-owner-panel" aria-labelledby="product-owner-title">
      <header className="product-config-panel-header">
        <span aria-hidden="true">{ownerSet ? <UserCheck /> : <UserX />}</span>
        <div>
          <p className="eyebrow">Client</p>
          <h2 id="product-owner-title">
            {resource.status === "loading"
              ? "Reading the Client"
              : resource.status === "error"
                ? "Client unavailable"
                : productOwnerLabel(resource.owner)}
          </h2>
          <p>
            The Client can accept or request changes on previews. They can never merge or
            deploy.
          </p>
        </div>
      </header>
      {resource.status === "error" ? <InlineFormError message={resource.error} /> : null}
      {resource.status === "ready" ? (
        <>
          <fieldset aria-label="Change the Client" disabled={locked}>
            <div className="product-config-field">
              <label htmlFor="product-owner-login">
                GitHub login
                <input
                  autoCapitalize="none"
                  autoComplete="off"
                  id="product-owner-login"
                  onChange={(event) => {
                    setLocalError("");
                    setSaved(false);
                    setLogin(event.target.value);
                  }}
                  placeholder="octocat"
                  spellCheck={false}
                  type="text"
                  value={login}
                />
              </label>
            </div>
            <ReasonField reason={reason} onChange={setReason} />
          </fieldset>
          <OperationNotice state={planOperation.state} label="Preview" />
          <OperationNotice state={applyOperation.state} label="Save" />
          {localError ? <InlineFormError message={localError} /> : null}
          {plan && (saved || planMatchesDraft) ? (
            <p className="product-owner-plan" role="status">
              {saved
                ? plan.after.githubId
                  ? `Saved. The Client is now ${productOwnerLabel(plan.after)}.`
                  : "Saved. This product has no Client."
                : productOwnerPlanSummary(plan)}
            </p>
          ) : null}
          <div className="product-config-actions">
            <button
              className="button"
              disabled={locked || !login.trim()}
              onClick={() => void preview(false)}
              type="button"
            >
              {isOperationBusy(planOperation.state) ? <LoaderCircle className="spin" /> : <Eye />}
              Preview change
            </button>
            <button
              className="button button-primary"
              disabled={
                busy ||
                !plan ||
                !planMatchesDraft ||
                !plan.changed ||
                saved
              }
              onClick={() => void save()}
              type="button"
            >
              {isOperationBusy(applyOperation.state) ? <LoaderCircle className="spin" /> : <Save />}
              {applyOperation.state.requiresIdempotencyContinuity ? "Retry save" : "Save"}
            </button>
            {ownerSet ? (
              <button
                className="button"
                disabled={locked}
                onClick={() => void preview(true)}
                type="button"
              >
                <UserX />
                Clear
              </button>
            ) : null}
            {plan && !applyOperation.state.requiresIdempotencyContinuity ? (
              <button className="button" onClick={startOver} type="button">
                <RotateCcw />
                Start over
              </button>
            ) : null}
          </div>
        </>
      ) : null}
    </section>
    <ProductProfileFieldPanel key={`${product}:image`} product={product} fixtureMode={fixtureMode} field="image" />
    <ProductProfileFieldPanel key={`${product}:production`} product={product} fixtureMode={fixtureMode} field="production" />
    </>
  );
}

function useProductOwnerOperation(scope: string, product: string, fixtureMode: DevFixtureMode) {
  async function execute(
    payload: ProductOwnerRequest,
    options: Parameters<typeof applyProductOwner>[2],
  ): Promise<AcceptedEvidenceResponse> {
    if (fixtureMode) {
      const fixtures = await loadDevFixtures();
      options.onDispatch?.();
      return fixtures.applyProductOwnerForFixture(fixtureMode, product, payload, options.signal);
    }
    return applyProductOwner(product, payload, options);
  }
  return useBrowserOperationController({
    execute,
    failureCertainty: productConfigFailureCertainty,
    failureFor: productConfigOperationFailure,
    scope,
  });
}


type ProfileField = "image" | "production";
type ProfileFieldRequest = ApplyProductImageRepositoryData["body"] | ApplyProductProductionUseData["body"];
interface ProfileFieldPlan {
  before: string;
  after: string;
  changed: boolean;
  digest: string;
  lanes: Array<{ instance: string; artifact: string }>;
}

function profileFieldPlan(response: AcceptedEvidenceResponse, field: ProfileField): ProfileFieldPlan {
  const result = response.result;
  const prefix = field === "image" ? "image_repository" : "production_use";
  if (!result || typeof result[`${prefix}_before`] !== "string" ||
      typeof result[`${prefix}_after`] !== "string" || typeof result.changed !== "boolean" ||
      (field === "production" && typeof result.plan_sha256 !== "string")) {
    throw new Error("Launchplane returned a dry run this page cannot read.");
  }
  return {
    before: result[`${prefix}_before`] as string,
    after: result[`${prefix}_after`] as string,
    changed: result.changed,
    digest: typeof result.plan_sha256 === "string" ? result.plan_sha256 : "",
    lanes: Array.isArray(result.lanes) ? result.lanes.map((lane) => ({
      instance: String(lane.instance), artifact: String(lane.current_artifact_id || "No recorded artifact"),
    })) : [],
  };
}

interface ReviewedProfileField {
  plan: ProfileFieldPlan;
  key: string;
  request: ProfileFieldRequest;
}

function recoverProfileFieldDraft(storageKey: string): ReviewedProfileField | null {
  try {
    const raw = sessionStorage.getItem(storageKey);
    if (!raw) return null;
    const draft = JSON.parse(raw) as ReviewedProfileField;
    if (draft.request.mode !== "apply" || typeof draft.request.reason !== "string" ||
        typeof draft.plan.after !== "string" || typeof draft.plan.before !== "string" ||
        typeof draft.key !== "string" || !Array.isArray(draft.plan.lanes)) return null;
    return draft;
  } catch { return null; }
}

function ProductProfileFieldPanel({ product, fixtureMode, field }: {
  product: string; fixtureMode: DevFixtureMode; field: ProfileField;
}) {
  const title = field === "image" ? "Image repository" : "Production use";
  const storageKey = `launchplane:product-profile-draft:${product}:${field}`;
  const [reviewed, setReviewed] = useState<ReviewedProfileField | null>(() => recoverProfileFieldDraft(storageKey));
  const [current, setCurrent] = useState<string | null>(null);
  const [value, setValue] = useState(reviewed?.plan.after ?? "");
  const [reason, setReason] = useState(reviewed?.request.reason ?? "");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const draftKey = JSON.stringify([field === "image" ? value.trim().replace(/\/+$/, "") : value.trim(), reason.trim()]);

  async function readValue(signal?: AbortSignal): Promise<{ value: string; suggested: string }> {
    if (fixtureMode) {
      const fixtures = await loadDevFixtures();
      return fixtures.productProfileFieldForFixture(fixtureMode, product, field);
    }
    const { profile } = await readProductProfile(product, signal);
    return { value: field === "image" ? profile.image.repository : profile.production_use,
      suggested: field === "image" ? `ghcr.io/${profile.repository.trim().toLowerCase()}` : profile.production_use };
  }

  async function execute(payload: ProfileFieldRequest, options: Parameters<typeof applyProductOwner>[2]) {
    if (fixtureMode) {
      const fixtures = await loadDevFixtures();
      options.onDispatch?.();
      return fixtures.applyProductProfileFieldForFixture(fixtureMode, product, field, payload);
    }
    return field === "image"
      ? applyProductImageRepository(product, payload as ApplyProductImageRepositoryData["body"], options)
      : applyProductProductionUse(product, payload as ApplyProductProductionUseData["body"], options);
  }
  const operationOptions = { execute, failureCertainty: productConfigFailureCertainty, failureFor: productConfigOperationFailure };
  const previewOperation = useBrowserOperationController({ ...operationOptions, scope: `${product}:${field}:plan`, readOnly: true });
  const applyOperation = useBrowserOperationController({ ...operationOptions,
    failureCertainty: profileApplyFailureCertainty, scope: `${product}:${field}:apply` });
  const busy = isOperationBusy(previewOperation.state) || isOperationBusy(applyOperation.state);
  const locked = busy || applyOperation.state.requiresIdempotencyContinuity;
  const matches = reviewed?.key === draftKey;

  useEffect(() => {
    const controller = new AbortController();
    readValue(controller.signal).then((stored) => {
      if (!controller.signal.aborted) { setCurrent(stored.value); if (!reviewed) setValue(stored.suggested); }
    }).catch((failure: unknown) => {
      if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : "Profile read failed.");
    });
    return () => controller.abort();
  }, [product, fixtureMode, field]);

  useEffect(() => {
    try {
      if (reviewed) sessionStorage.setItem(storageKey, JSON.stringify(reviewed));
      else sessionStorage.removeItem(storageKey);
    } catch { setError("This browser cannot preserve the reviewed draft. Keep this page open until Apply is confirmed."); }
  }, [reviewed, storageKey]);

  useEffect(() => {
    let active = true;
    if (applyOperation.state.phase === "failed" && !applyOperation.state.requiresIdempotencyContinuity &&
        applyOperation.state.failure?.code === "stale") {
      setReviewed(null);
      readValue().then((stored) => { if (active) setCurrent(stored.value); }).catch((failure: unknown) => {
        if (active) setError(`Stale plan; the current profile could not be refreshed. ${failure instanceof Error ? failure.message : "Read failed."}`);
      });
    }
    return () => { active = false; };
  }, [applyOperation.state]);

  async function preview() {
    setError(""); setNotice(""); setReviewed(null);
    if (!reason.trim() || !value.trim()) { setError("Enter a value and a change reason."); return; }
    if (!applyOperation.reset()) { setError("Retry the uncertain Apply with its existing operation key."); return; }
    const payload: ProfileFieldRequest = field === "image"
      ? { mode: "dry-run", image_repository: value.trim(), reason: reason.trim() }
      : { mode: "dry-run", production_use: value as "unknown" | "prelaunch" | "live", reason: reason.trim() };
    const response = await previewOperation.run(payload);
    if (!response) return;
    try {
      const plan = profileFieldPlan(response, field);
      const request: ProfileFieldRequest = field === "image"
        ? { mode: "apply", image_repository: plan.after, expected_image_repository: plan.before, reason: reason.trim() }
        : { mode: "apply", production_use: plan.after as "unknown" | "prelaunch" | "live", reviewed_plan_sha256: plan.digest, reason: reason.trim() };
      setReviewed({ plan, key: draftKey, request });
    } catch (failure) { setError(failure instanceof Error ? failure.message : "Unreadable dry run."); }
  }

  async function apply() {
    if (!reviewed || !matches) return;
    setError(""); setNotice("");
    try {
      const draft = JSON.stringify(reviewed);
      sessionStorage.setItem(storageKey, draft);
      if (sessionStorage.getItem(storageKey) !== draft) throw new Error("Draft read-back differs.");
    } catch {
      setError("The reviewed request could not be saved in this tab. Nothing was sent. Restore session storage and retry Apply, or open this product in a new tab and dry-run again.");
      return;
    }
    const response = await applyOperation.run(reviewed.request);
    if (!response) return;
    setReviewed(null);
    try {
      const stored = await readValue();
      setCurrent(stored.value); setValue(stored.value);
      if (stored.value !== reviewed.plan.after) {
        setError("Apply returned, but read-back differs from the reviewed value. Review a new dry run.");
      } else {
        setNotice(`Applied and read back. ${title}: ${stored.value}.`);
      }
    } catch (failure) {
      setError(`Apply returned; read-back could not be confirmed. ${failure instanceof Error ? failure.message : "Read failed."}`);
    }
  }

  return (
    <section className="product-config-panel product-profile-field-panel" aria-labelledby={`product-${field}-title`}>
      <header className="product-config-panel-header"><span aria-hidden="true"><UserCheck /></span><div>
        <p className="eyebrow">Client settings</p><h2 id={`product-${field}-title`}>{title}</h2>
        <p>{field === "image"
          ? "Change where this product publishes new images. Recorded rollback artifacts remain available."
          : "Prelaunch exempts this product from release review. Unknown and live require review."}</p>
      </div></header>
      <p>Current: {current === null ? "Reading profile…" : current || "Not set"}</p>
      <fieldset disabled={locked || current === null} aria-label={`Change ${title.toLowerCase()}`}>
        <div className="product-config-field"><label htmlFor={`product-${field}-value`}>{title}</label>
          {field === "image" ? <input id={`product-${field}-value`} type="text" value={value} onChange={(event) => setValue(event.target.value)} spellCheck={false} autoCapitalize="none" />
            : <select id={`product-${field}-value`} value={value} onChange={(event) => setValue(event.target.value)}>
              <option value="unknown">Unknown</option><option value="prelaunch">Prelaunch</option><option value="live">Live</option>
            </select>}
        </div>
        <ReasonField reason={reason} onChange={setReason} />
      </fieldset>
      <OperationNotice state={previewOperation.state} label="Dry run" />
      <OperationNotice state={applyOperation.state} label="Apply" />
      {applyOperation.state.requiresIdempotencyContinuity && !reviewed ?
        <InlineFormError message="An earlier Apply is uncertain, and its reviewed draft is unavailable. Read the profile and reconcile that operation before another change." /> : null}
      {error ? <InlineFormError message={error} /> : null}
      {notice ? <p role="status">{notice}</p> : null}
      {reviewed && matches ? <div className="product-owner-plan">
        <p>{reviewed.plan.before || "Not set"} → {reviewed.plan.after || "Not set"}{reviewed.plan.changed ? "" : " (no change)"}</p>
        {reviewed.plan.lanes.length ? <ul>{reviewed.plan.lanes.map((lane) =>
          <li key={lane.instance}>{lane.instance}: {lane.artifact}</li>)}</ul> : null}
      </div> : null}
      <div className="product-config-actions">
        <button className="button" disabled={locked || current === null || !reason.trim() || !value.trim()} onClick={() => void preview()} type="button">Dry run</button>
        <button className="button button-primary" disabled={busy || !matches || !reviewed?.plan.changed} onClick={() => void apply()} type="button">
          {applyOperation.state.requiresIdempotencyContinuity ? "Retry Apply" : "Apply"}
        </button>
      </div>
    </section>
  );
}

function profileApplyFailureCertainty(error: unknown, dispatched: boolean): BrowserOperationFailureCertainty {
  // Both profile routes check the same-key DB mutation before evaluating staleness.
  // Its CAS binding prevents a late conflicting write; a fresh dry run is safe.
  // Stale can race a commit, so the panel refreshes the current value without claiming success.
  if (dispatched && error instanceof LaunchplaneApiError && error.statusCode === 409 && error.code === "stale") {
    return "settled";
  }
  return productConfigFailureCertainty(error, dispatched);
}

import { Eye, LoaderCircle, RotateCcw, Save, UserCheck, UserX } from "lucide-react";
import { useEffect, useState } from "react";

import { applyProductOwner, LaunchplaneApiError, readProductProfile } from "./api";
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

import type { AcceptedEvidenceResponse, ApplyProductOwnerData } from "./generated/openapi.ts";

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
            ? "This session cannot read the product's Owner."
            : error instanceof Error
              ? error.message
              : "Launchplane could not read the product's Owner.",
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
    <section className="product-config-panel product-owner-panel" aria-labelledby="product-owner-title">
      <header className="product-config-panel-header">
        <span aria-hidden="true">{ownerSet ? <UserCheck /> : <UserX />}</span>
        <div>
          <p className="eyebrow">Owner</p>
          <h2 id="product-owner-title">
            {resource.status === "loading"
              ? "Reading the Owner"
              : resource.status === "error"
                ? "Owner unavailable"
                : productOwnerLabel(resource.owner)}
          </h2>
          <p>
            The Owner can accept or request changes on previews. They can never merge or
            deploy.
          </p>
        </div>
      </header>
      {resource.status === "error" ? <InlineFormError message={resource.error} /> : null}
      {resource.status === "ready" ? (
        <>
          <fieldset aria-label="Change the Owner" disabled={locked}>
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
                  ? `Saved. The Owner is now ${productOwnerLabel(plan.after)}.`
                  : "Saved. This product has no Owner."
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

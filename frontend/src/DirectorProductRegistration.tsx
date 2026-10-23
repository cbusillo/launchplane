import { useState, type FormEvent } from "react";

import { applyProductOnboarding, readProductProfile } from "./api";
import { recoverBrowserOperationState, type BrowserOperationOptions } from "./browser-operation";
import { isOperationBusy, InlineFormError, OperationNotice } from "./ProductConfigForms";
import { productConfigFailureCertainty, productConfigOperationFailure, ProductConfigRecoveryStorageUnavailable } from "./product-config-operation";
import { normalizeOwnerLoginInput } from "./product-owner-operation";
import { AppLink } from "./router";
import { useBrowserOperationController } from "./use-browser-operation";
import type { AcceptedEvidenceResponse, ApplyProductOnboardingData } from "./generated/openapi.ts";

type RegistrationRequest = ApplyProductOnboardingData["body"];
type Draft = { product: string; name: string; repository: string; login: string; githubId: string };
type Review = { draft: Draft; digest: string };
const storageKey = "launchplane:director-product-registration";
const applyScope = "director-product-registration:apply";
const fields: Array<[keyof Draft, string]> = [["product", "Product key"], ["name", "Product name"], ["repository", "Repository"], ["login", "Client GitHub login"], ["githubId", "Client GitHub ID"]];
const emptyDraft: Draft = { product: "", name: "", repository: "", login: "", githubId: "" };

function recoverReview(): Review | null {
  try {
    const review = JSON.parse(sessionStorage.getItem(storageKey) ?? "null") as Review | null;
    return review && /^[a-f0-9]{64}$/.test(review.digest) && fields.every(([key]) => typeof review.draft?.[key] === "string") ? review : null;
  } catch { return null; }
}

function requestFor(review: Review, mode: "apply" | "dry_run"): RegistrationRequest {
  const draft = review.draft;
  return { product: "launchplane", mode, manifest: {
    product: draft.product.trim(), display_name: draft.name.trim(), repository: draft.repository.trim(), classification: "active_awareness",
    owner: { github_login: draft.login.trim(), github_id: draft.githubId.trim(), review_label: "" }, release_on_acceptance: "director_standing",
  }, ...(mode === "apply" ? { reviewed_plan_sha256: review.digest } : {}) };
}

function useRegistrationOperation(readOnly: boolean) {
  const scope = readOnly ? "director-product-registration:plan" : applyScope;
  return useBrowserOperationController<RegistrationRequest, AcceptedEvidenceResponse>({
    scope, readOnly, failureCertainty: productConfigFailureCertainty, failureFor: productConfigOperationFailure,
    execute: (request, options: BrowserOperationOptions) => applyProductOnboarding(request, { ...options, onDispatch: () => {
      options.onDispatch?.();
      if (!readOnly && recoverBrowserOperationState(scope).identity?.idempotencyKey !== options.idempotencyKey) {
        throw new ProductConfigRecoveryStorageUnavailable("Registration was not sent because this tab could not preserve its operation key.");
      }
    } }),
  });
}

export function DirectorProductRegistration({ onRefresh }: { onRefresh: () => void }) {
  const [recovered] = useState(recoverReview);
  const [draft, setDraft] = useState<Draft>(recovered?.draft ?? emptyDraft);
  const [review, setReview] = useState<Review | null>(recovered);
  const [confirmed, setConfirmed] = useState(Boolean(recovered));
  const [error, setError] = useState("");
  const [verified, setVerified] = useState(false);
  const [reading, setReading] = useState(false);
  const [recoveringReadback, setRecoveringReadback] = useState(Boolean(recovered));
  const plan = useRegistrationOperation(true);
  const apply = useRegistrationOperation(false);
  const busy = reading || isOperationBusy(plan.state) || isOperationBusy(apply.state);
  const readbackOnly = (recoveringReadback || apply.state.phase === "succeeded") && !apply.state.requiresIdempotencyContinuity;
  const locked = busy || verified || recoveringReadback || apply.state.requiresIdempotencyContinuity || apply.state.phase === "succeeded";
  const matches = review !== null && JSON.stringify(review.draft) === JSON.stringify(draft);

  async function preview(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (!confirmed || !apply.reset()) return;
    setError(""); setReview(null); setVerified(false);
    const response = await plan.run(requestFor({ draft, digest: "" }, "dry_run"));
    const digest = response?.result?.plan_sha256;
    if (typeof digest === "string" && /^[a-f0-9]{64}$/.test(digest)) setReview({ draft: { ...draft }, digest });
    else if (response) setError("The registration preview could not be read. Review it again before registering.");
  }

  async function readBack(saved: Review): Promise<void> {
    setReading(true); setError("");
    try {
      const profile = (await readProductProfile(saved.draft.product.trim())).profile;
      if (profile.repository.toLowerCase() !== saved.draft.repository.trim().toLowerCase() ||
          profile.owner.github_id !== saved.draft.githubId.trim() || profile.owner.github_login.toLowerCase() !== normalizeOwnerLoginInput(saved.draft.login).toLowerCase() ||
          profile.release_on_acceptance !== "director_standing" || profile.lifecycle_state !== "active" || profile.classification !== "active_awareness") {
        throw new Error("The saved product differs from this registration. Keep this record and inspect the product before proceeding.");
      }
      setVerified(true);
      sessionStorage.removeItem(storageKey);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "The saved registration could not be read.");
    }
    finally { setReading(false); }
  }

  async function register(): Promise<void> {
    if (!review || !matches || !confirmed) return;
    setError("");
    if (readbackOnly) { await readBack(review); return; }
    try {
      const serialized = JSON.stringify(review);
      sessionStorage.setItem(storageKey, serialized);
      if (sessionStorage.getItem(storageKey) !== serialized) throw new Error("Registration draft was not preserved.");
    } catch { setError("Registration was not sent. Restore this tab's session storage and try again."); return; }
    if (await apply.run(requestFor(review, "apply"))) await readBack(review);
  }

  function startAnother(): void {
    if (!apply.reset() || !plan.reset()) return;
    setDraft(emptyDraft); setReview(null); setConfirmed(false); setVerified(false); setError("");
    setRecoveringReadback(false);
  }

  function reviewAgain(): void {
    if (!readbackOnly || busy || !error || !apply.reset() || !plan.reset()) return;
    sessionStorage.removeItem(storageKey);
    setRecoveringReadback(false); setReview(null); setError("");
  }

  return <details className="product-config-panel">
    <summary>Register a Director product</summary>
    <section aria-label="Director product registration">
      <h2>Record a product Launchplane does not deploy</h2>
      <p>The Director's standing direction supplies acceptance. This registration creates no environments or deployment targets.</p>
      <form onSubmit={preview}>
        <fieldset disabled={locked}>
          {fields.map(([key, label]) => <div className="product-config-field" key={key}><label htmlFor={`registration-${key}`}>{label}</label>
            <input id={`registration-${key}`} required autoComplete="off" autoCapitalize="none" value={draft[key]} inputMode={key === "githubId" ? "numeric" : "text"}
              onChange={event => { setDraft({ ...draft, [key]: event.target.value }); setReview(null); setVerified(false); }} />
          </div>)}
          <label><input type="checkbox" checked={confirmed} onChange={event => { setConfirmed(event.target.checked); setReview(null); }} />
            The recorded Client is the Director and accepts this product under standing direction.
          </label>
        </fieldset>
        <button className="button" type="submit" disabled={locked || !confirmed}>Review registration</button>
      </form>
      {matches && review ? <p>Register {review.draft.name} from {review.draft.repository}, with {review.draft.login} as the Client under standing acceptance.</p> : null}
      {error ? <InlineFormError message={error} /> : null}
      {error && readbackOnly ? <button className="button" type="button" disabled={busy} onClick={reviewAgain}>Review registration again</button> : null}
      <OperationNotice label="Registration preview" state={plan.state} />
      {!verified ? <OperationNotice label="Registration" state={apply.state} /> : null}
      {apply.state.requiresIdempotencyContinuity && !busy ? <div>
        <p>This registration's outcome is still unknown. Keep this original tab open to retain its reviewed request and operation key. A separate registration does not cancel or settle it.</p>
        <AppLink className="button" to="/ui/products" target="_blank" rel="noopener noreferrer">Open separate registration</AppLink>
      </div> : null}
      {verified ? <div role="status"><p>Product record matches this registration review.</p><button className="button" onClick={onRefresh}>Refresh products</button><button className="button" onClick={startAnother}>Register another product</button></div> :
        <button className="button button-primary" type="button" disabled={busy || !matches || !confirmed} onClick={() => void register()}>
          {apply.state.requiresIdempotencyContinuity ? "Retry same registration" : readbackOnly ? "Read registration" : "Register product"}
        </button>}
    </section>
  </details>;
}

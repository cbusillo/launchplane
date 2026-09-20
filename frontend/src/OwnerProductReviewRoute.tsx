import { ExternalLink, LogOut, Moon, Sun } from "lucide-react";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";

import {
  LaunchplaneApiError,
  readProductReview,
  writeProductReviewDecision,
} from "./api";
import type { DevFixtureMode } from "./dev-fixture-loader";
import { loadDevFixtures } from "./dev-fixture-loader";
import { formatTime } from "./format";
import { ownerAcceptanceLookupFromSearch } from "./route-model";
import { useAppSearchParams } from "./router";
import { safeExternalUrl } from "./url";

import type {
  GitHubHumanIdentityResponse,
  ProductReviewDecisionRecord,
  ProductReviewResponse,
} from "./generated/openapi.ts";

type Theme = "dark" | "light";

export function OwnerReviewShell({
  children,
  identity,
  notice,
  onDismissNotice,
  onLogout,
  onThemeChange,
  signingOut,
  theme,
}: {
  children: ReactNode;
  identity: GitHubHumanIdentityResponse;
  notice: string;
  onDismissNotice: () => void;
  onLogout: () => void;
  onThemeChange: (theme: Theme) => void;
  signingOut: boolean;
  theme: Theme;
}) {
  useEffect(() => {
    const previousTitle = document.title;
    document.title = "Product review · Launchplane";
    document.querySelector<HTMLElement>("[data-route-heading]")?.focus({
      preventScroll: true,
    });
    return () => {
      document.title = previousTitle;
    };
  }, []);

  return (
    <div className="owner-review-shell">
      <a className="skip-link" href="#main-content">
        Skip to review
      </a>
      <header className="owner-review-header">
        <div className="owner-review-brand" aria-label="Launchplane product review">
          <img
            alt=""
            src={`${import.meta.env.BASE_URL}assets/brand/launchplane-icon.svg`}
          />
          <span>
            <strong>Launchplane</strong>
            <small>Product review</small>
          </span>
        </div>
        <div className="owner-review-session">
          <span>{identity.name || identity.login}</span>
          <button
            aria-label={`Use ${theme === "dark" ? "light" : "dark"} theme`}
            className="icon-button"
            type="button"
            onClick={() => onThemeChange(theme === "dark" ? "light" : "dark")}
          >
            {theme === "dark" ? (
              <Sun size={16} aria-hidden="true" />
            ) : (
              <Moon size={16} aria-hidden="true" />
            )}
          </button>
          <button className="button" type="button" disabled={signingOut} onClick={onLogout}>
            <LogOut size={15} aria-hidden="true" />
            {signingOut ? "Signing out…" : "Sign out"}
          </button>
        </div>
      </header>
      {notice ? (
        <div className="owner-review-notice" role="status">
          <span>{notice}</span>
          <button type="button" onClick={onDismissNotice}>
            Dismiss
          </button>
        </div>
      ) : null}
      <main id="main-content" className="owner-review-main">
        {children}
      </main>
    </div>
  );
}

export function OwnerProductReviewRoute({
  fixtureMode,
}: {
  fixtureMode: DevFixtureMode;
}) {
  const searchParams = useAppSearchParams();
  const lookup = ownerAcceptanceLookupFromSearch(searchParams.toString());
  const [review, setReview] = useState<ProductReviewResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const requestRef = useRef(0);

  const loadReview = useCallback(
    async (signal?: AbortSignal) => {
      if (!lookup.valid) return;
      const requestId = requestRef.current + 1;
      requestRef.current = requestId;
      setLoading(true);
      setError("");
      try {
        const response = fixtureMode
          ? await loadDevFixtures().then((fixtures) =>
              fixtures.productReviewForFixture(fixtureMode),
            )
          : await readProductReview(
              lookup.repository,
              Number(lookup.pullRequest),
              signal,
            );
        if (requestRef.current !== requestId || signal?.aborted) return;
        setReview(response);
      } catch (loadError) {
        if (requestRef.current !== requestId || signal?.aborted) return;
        const apiError = loadError as LaunchplaneApiError;
        setError(
          apiError.statusCode === 403
            ? "You are not this product's Owner, so this review is not available to you. If you expected to see it, ask the person who sent you the link."
            : apiError.statusCode === 401
              ? "You are not signed in. Sign in with GitHub to review this change."
              : "The review could not be loaded. Try again in a moment.",
        );
      } finally {
        if (requestRef.current === requestId && !signal?.aborted) setLoading(false);
      }
    },
    [fixtureMode, lookup.pullRequest, lookup.repository, lookup.valid],
  );

  useEffect(() => {
    setReview(null);
    setError("");
    if (!lookup.valid) return;
    const controller = new AbortController();
    void loadReview(controller.signal);
    return () => controller.abort();
  }, [loadReview, lookup.valid]);

  return (
    <section className="owner-review-page">
      <div className="owner-review-intro">
        <p className="eyebrow">Product decision</p>
        <h1 data-route-heading tabIndex={-1}>Review this change</h1>
        <p>
          Open the preview and look at the change. Then accept it, or say what
          should change. Your decision does not publish anything.
        </p>
      </div>
      {!lookup.valid ? (
        <OwnerReviewState>
          This review link is incomplete. Go back to the pull request and open
          its Launchplane review link again.
        </OwnerReviewState>
      ) : loading && !review ? (
        <OwnerReviewState>Loading the review…</OwnerReviewState>
      ) : error ? (
        <OwnerReviewState tone="error">{error}</OwnerReviewState>
      ) : review ? (
        <ProductReviewCard
          fixtureMode={fixtureMode}
          review={review}
          onDecided={setReview}
        />
      ) : null}
    </section>
  );
}

function OwnerReviewState({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "neutral" | "error";
}) {
  return (
    <p
      className="owner-review-state"
      data-tone={tone}
      role={tone === "error" ? "alert" : "status"}
    >
      {children}
    </p>
  );
}

function ProductReviewCard({
  fixtureMode,
  onDecided,
  review,
}: {
  fixtureMode: DevFixtureMode;
  onDecided: (review: ProductReviewResponse) => void;
  review: ProductReviewResponse;
}) {
  const previewUrl = safeExternalUrl(review.preview_url);
  const pullRequestUrl = safeExternalUrl(review.pull_request_url);
  return (
    <article className="owner-review-card" data-product={review.product}>
      <header>
        <p className="eyebrow">Product</p>
        <h2>{review.display_name || review.product}</h2>
      </header>
      <div className="owner-review-links">
        {previewUrl ? (
          <a
            className="button button-primary owner-review-preview"
            href={previewUrl.toString()}
            target="_blank"
            rel="noreferrer"
          >
            Open the preview <ExternalLink size={15} aria-hidden="true" />
          </a>
        ) : null}
        {pullRequestUrl ? (
          <a
            className="button"
            href={pullRequestUrl.toString()}
            target="_blank"
            rel="noreferrer"
          >
            Pull request #{review.pull_request_number}{" "}
            <ExternalLink size={15} aria-hidden="true" />
          </a>
        ) : null}
      </div>
      {previewUrl && review.head_sha ? (
        <p className="owner-review-state">
          Preview version {review.head_sha.slice(0, 7)}
        </p>
      ) : null}
      {review.latest_decision ? (
        <LatestDecision decision={review.latest_decision} />
      ) : null}
      {review.can_decide && previewUrl ? (
        <ProductReviewDecisionForm
          fixtureMode={fixtureMode}
          review={review}
          onDecided={onDecided}
        />
      ) : (
        <OwnerReviewState>{cannotDecideMessage(review)}</OwnerReviewState>
      )}
    </article>
  );
}

function cannotDecideMessage(review: ProductReviewResponse): string {
  if (!review.owner_set) {
    return "No Owner set for this product. Ask the operator to name one before this change can be reviewed.";
  }
  if (!review.viewer_is_owner) {
    return "You are not this product's Owner. You can look, but only the Owner can record a decision.";
  }
  return "No preview yet. Come back when the pull request says the preview is ready.";
}

function LatestDecision({ decision }: { decision: ProductReviewDecisionRecord }) {
  return (
    <section className="owner-review-latest" aria-label="Latest decision">
      <p>
        <strong>
          {decision.decision === "accepted" ? "Accepted" : "Changes requested"}
        </strong>{" "}
        by @{decision.owner_github_login} · {formatTime(decision.decided_at)}
      </p>
      {decision.reason ? <blockquote>{decision.reason}</blockquote> : null}
    </section>
  );
}

function ProductReviewDecisionForm({
  fixtureMode,
  onDecided,
  review,
}: {
  fixtureMode: DevFixtureMode;
  onDecided: (review: ProductReviewResponse) => void;
  review: ProductReviewResponse;
}) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState("");
  const [recorded, setRecorded] = useState(false);

  const record = async (decision: ProductReviewDecisionRecord["decision"]) => {
    setBusy(true);
    setFailure("");
    setRecorded(false);
    const decisionReason = decision === "changes_requested" ? reason.trim() : "";
    try {
      const response = fixtureMode
        ? await loadDevFixtures().then((fixtures) =>
            fixtures.productReviewForFixture(
              fixtureMode,
              fixtures.productReviewDecisionForFixture(decision, decisionReason),
            ),
          )
        : await writeProductReviewDecision({
            repository: review.repository,
            pull_request: review.pull_request_number,
            decision,
            reason: decisionReason,
          });
      setReason("");
      setRecorded(true);
      onDecided(response);
    } catch (writeError) {
      const apiError = writeError as LaunchplaneApiError;
      setFailure(
        apiError.statusCode === 403
          ? "Your decision was not recorded because you are not this product's Owner."
          : apiError.statusCode === 409
            ? apiError.message
            : "Your decision was not recorded. Try again in a moment.",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <section
      className="owner-review-action"
      aria-label={`Decision for ${review.display_name || review.product}`}
    >
      <label>
        <span>What should change? (needed only when you request changes)</span>
        <textarea
          maxLength={4000}
          value={reason}
          disabled={busy}
          onChange={(event) => {
            setRecorded(false);
            setReason(event.target.value);
          }}
        />
      </label>
      <div className="owner-review-action-buttons">
        <button
          className="button button-primary"
          type="button"
          disabled={busy}
          onClick={() => void record("accepted")}
        >
          Accept
        </button>
        <button
          className="button"
          type="button"
          disabled={busy || !reason.trim()}
          onClick={() => void record("changes_requested")}
        >
          Request changes
        </button>
      </div>
      {busy ? <p className="owner-review-state" role="status">Recording…</p> : null}
      {failure ? <p className="owner-review-alert" role="alert">{failure}</p> : null}
      {recorded ? <p className="owner-review-success" role="status">Decision recorded.</p> : null}
    </section>
  );
}

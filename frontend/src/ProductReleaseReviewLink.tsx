import { useEffect, useState } from "react";
import { readReleaseReviewAccess } from "./api";
import { loadDevFixtures, type DevFixtureMode } from "./dev-fixture-loader";
import type { ReleaseReviewAccessResponse } from "./generated/openapi.ts";
import { AppLink, ownerReviewPath } from "./router";

export function ProductReleaseReviewLink({
  product,
  admin = false,
  fixtureMode,
  showProductName = false,
}: {
  product: string;
  admin?: boolean;
  fixtureMode: DevFixtureMode;
  showProductName?: boolean;
}) {
  const [review, setReview] = useState<ReleaseReviewAccessResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    setReview(null);
    setLoading(true);
    setError(false);
    const read = fixtureMode
      ? loadDevFixtures().then(fixtures => ({ ...fixtures.releaseReviewForFixture(fixtureMode), product }))
      : readReleaseReviewAccess(product, controller.signal);
    void read.then(payload => {
      if (!controller.signal.aborted && payload.product === product) setReview(payload);
    }).catch(() => {
      if (!controller.signal.aborted) setError(true);
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [product, fixtureMode]);

  const permitted = review?.product === product && (admin || review.viewer_is_owner);
  return <div aria-busy={loading} data-release-review-access={loading ? "loading" : "settled"}>
    {showProductName ? <h1 data-route-heading tabIndex={-1}>{permitted ? review.display_name : "Product review"}</h1> : null}
    {permitted ? <AppLink className="button" to={`${ownerReviewPath()}?${new URLSearchParams({ product })}`}>Release review</AppLink> : null}
    {showProductName && loading ? <p role="status">Loading product review…</p> : null}
    {!loading && (error || (showProductName && !permitted)) ? <p role="status">This product's release review is unavailable. Refresh to try again.</p> : null}
  </div>;
}

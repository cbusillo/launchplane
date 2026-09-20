import type { AcceptedEvidenceResponse } from "./generated/openapi.ts";

export interface ProductOwnerIdentity {
  githubId: string;
  githubLogin: string;
}

export interface ProductOwnerPlan {
  after: ProductOwnerIdentity;
  applied: boolean;
  before: ProductOwnerIdentity;
  changed: boolean;
}

export const NO_PRODUCT_OWNER: ProductOwnerIdentity = { githubId: "", githubLogin: "" };

const GITHUB_LOGIN_PATTERN = /^[A-Za-z0-9][A-Za-z0-9-]{0,38}$/;

export function normalizeOwnerLoginInput(value: string): string {
  return value.trim().replace(/^@/, "");
}

export function ownerLoginInputError(value: string): string {
  const login = normalizeOwnerLoginInput(value);
  if (!login) {
    return "Enter the Owner's GitHub login.";
  }
  if (!GITHUB_LOGIN_PATTERN.test(login)) {
    return "A GitHub login has only letters, numbers, and hyphens.";
  }
  return "";
}

export function productOwnerLabel(owner: ProductOwnerIdentity): string {
  if (!owner.githubId || !owner.githubLogin) {
    return "No Owner set";
  }
  return `${owner.githubLogin} (id ${owner.githubId})`;
}

export function productOwnerDraftKey(login: string, clear: boolean): string {
  return JSON.stringify(clear ? ["clear"] : ["set", normalizeOwnerLoginInput(login).toLowerCase()]);
}

export function productOwnerFromRecord(record: unknown): ProductOwnerIdentity {
  if (!record || typeof record !== "object") {
    return NO_PRODUCT_OWNER;
  }
  const { github_id: githubId, github_login: githubLogin } = record as Record<string, unknown>;
  if (typeof githubId !== "string" || typeof githubLogin !== "string" || !githubId || !githubLogin) {
    return NO_PRODUCT_OWNER;
  }
  return { githubId, githubLogin };
}

export function productOwnerPlanFromResponse(
  response: Pick<AcceptedEvidenceResponse, "result">,
): ProductOwnerPlan | null {
  const result = response.result;
  if (!result || typeof result.changed !== "boolean" || !("owner_after" in result)) {
    return null;
  }
  return {
    after: productOwnerFromRecord(result.owner_after),
    applied: result.applied === true,
    before: productOwnerFromRecord(result.owner_before),
    changed: result.changed,
  };
}

export function productOwnerPlanSummary(plan: ProductOwnerPlan): string {
  if (!plan.changed) {
    return plan.after.githubId
      ? `No change. The Owner is already ${productOwnerLabel(plan.after)}.`
      : "No change. This product has no Owner.";
  }
  if (!plan.after.githubId) {
    return `Remove the Owner ${productOwnerLabel(plan.before)}.`;
  }
  return `Set the Owner to ${productOwnerLabel(plan.after)}.`;
}

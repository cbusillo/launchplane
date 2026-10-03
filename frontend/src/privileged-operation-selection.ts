import {
  LaunchplaneApiError,
  type PrivilegedOperationDescriptorId as ApiDescriptorId,
  type PrivilegedOperationListResponse,
} from "./api";

export type PrivilegedOperationDescriptorId = Exclude<ApiDescriptorId, undefined>;

const initialPlanTypes: PrivilegedOperationDescriptorId[] = [
  "managed-secret-reencryption",
  "managed-authz-policy-set",
  "managed-merge-train-policy-import",
];

const planNames: Record<PrivilegedOperationDescriptorId, string> = {
  "managed-secret-reencryption": "secret rotation plans",
  "managed-authz-policy-set": "access policy plans",
  "managed-merge-train-policy-import": "merge-train policy plans",
  "ordinary-agent-delivery-activation": "agent delivery plans",
};

export function selectedPlanType(value: string | null): PrivilegedOperationDescriptorId | null {
  return value !== null && Object.hasOwn(planNames, value)
    ? value as PrivilegedOperationDescriptorId
    : null;
}

export interface SelectedOperationPlans {
  descriptorId: PrivilegedOperationDescriptorId | null;
  plans: PrivilegedOperationListResponse | null;
}

export async function loadSelectedOperationPlans(
  selected: PrivilegedOperationDescriptorId | null,
  signal: AbortSignal,
  read: (descriptorId: PrivilegedOperationDescriptorId) => Promise<PrivilegedOperationListResponse>,
): Promise<SelectedOperationPlans> {
  for (const descriptorId of selected === null ? initialPlanTypes : [selected]) {
    signal.throwIfAborted();
    try {
      return { descriptorId, plans: await read(descriptorId) };
    } catch (error) {
      if (!(error instanceof LaunchplaneApiError) || error.statusCode !== 403 || error.code !== "authorization_denied") {
        throw error;
      }
      if (selected !== null) {
        throw new LaunchplaneApiError(
          `You do not have access to ${planNames[descriptorId]}.`,
          error.statusCode,
          error.traceId,
          error.code,
        );
      }
    }
  }
  return { descriptorId: null, plans: null };
}

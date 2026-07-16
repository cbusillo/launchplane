import type {
  AuthSessionResponse,
  LaunchplaneErrorResponse,
  ProductEnvironmentListResponse,
} from "./generated/openapi.ts";

export type Status =
  | "pass"
  | "fail"
  | "pending"
  | "skipped"
  | "unknown"
  | "blocked";

export type AuthSessionPayload = AuthSessionResponse;

export interface LogoutPayload {
  status: "ok";
  trace_id: string;
}

export type ApiErrorPayload = LaunchplaneErrorResponse;

export type ProductListPayload = ProductEnvironmentListResponse;

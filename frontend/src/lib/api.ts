/**
 * The one place browser → API calls go through (#50).
 *
 * Beyond trimming boilerplate, `apiFetch` centralizes the session-expiry story:
 * any 401 from any endpoint notifies the auth provider, which drops the
 * principal and lets the route guard bounce the user to the login page. Without
 * that, an expired session would surface as a random failed action somewhere in
 * the UI.
 *
 * Credentials are the httpOnly session cookie, which the browser attaches to
 * same-origin requests on its own — every deployed topology serves the app and
 * the API from one origin (see app/auth.py), so there is no token for this
 * module to hold, and an XSS foothold has nothing to steal.
 */

export type Role = "reviewer" | "admin";

/** The authenticated caller, as returned by `/api/auth/me` and `/api/auth/login`. */
export type Principal = {
  email: string;
  role: Role;
};

/** The API's error contract: every rejection carries `{error, detail}`. */
type ApiProblem = { error?: string; detail?: string };

let unauthorizedHandler: (() => void) | null = null;

/**
 * Register the callback fired whenever the API answers 401. Set once by
 * AuthProvider; a module-level slot rather than context because `apiFetch` is
 * called from plain functions and hooks that have no provider in scope.
 */
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
}

export async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const response = await fetch(path, init);
  if (response.status === 401) unauthorizedHandler?.();
  return response;
}

/**
 * The server's own explanation of a failed response, or `fallback` when it
 * didn't send one (a proxy 502, a truncated body).
 */
export async function problemDetail(response: Response, fallback: string): Promise<string> {
  const problem = (await response.json().catch(() => ({}))) as ApiProblem;
  return problem.detail ?? `${fallback} (${response.status})`;
}

/** POST /api/auth/login — resolves to the Principal, or throws with the API's message. */
export async function login(email: string, password: string): Promise<Principal> {
  // Not via apiFetch: a 401 here means "those credentials are wrong", which the
  // login form shows inline. Routing it through the session-expiry handler would
  // be a redirect to the page the user is already on.
  const response = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!response.ok) throw new Error(await problemDetail(response, "Sign-in failed"));
  return (await response.json()) as Principal;
}

/** POST /api/auth/logout — clears the session cookie server-side. */
export async function logout(): Promise<void> {
  await fetch("/api/auth/logout", { method: "POST" });
}

/** GET /api/auth/me — the session bootstrap. `null` when not signed in. */
export async function fetchPrincipal(): Promise<Principal | null> {
  // Also not via apiFetch: a 401 is the expected answer for a visitor with no
  // session, and it is this function's job to report that as `null`, not to
  // announce an expiry.
  const response = await fetch("/api/auth/me");
  if (response.status === 401) return null;
  if (!response.ok) throw new Error(await problemDetail(response, "Could not load your session"));
  return (await response.json()) as Principal;
}

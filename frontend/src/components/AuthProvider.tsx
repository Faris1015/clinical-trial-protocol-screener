"use client";

import { createContext, useCallback, useContext, useEffect, useState } from "react";
import {
  fetchPrincipal,
  login as loginRequest,
  logout as logoutRequest,
  setUnauthorizedHandler,
  type Principal,
  type Role,
} from "@/lib/api";

/**
 * Session state for the whole app (#50).
 *
 * The frontend is a static export with no request-time server, so there is no
 * server-side session to render against: the app boots as "unknown", asks
 * `/api/auth/me` once, and settles into signed-in or signed-out. Everything that
 * gates on identity — the route guard, the role-gated nav — reads this context,
 * so there is exactly one source of truth for "who is using this".
 *
 * `status` is deliberately three-valued. Collapsing "checking" into
 * "signed out" would flash the login page on every reload for an
 * already-authenticated reviewer.
 */

export type AuthStatus = "checking" | "authenticated" | "anonymous";

type AuthContextValue = {
  principal: Principal | null;
  status: AuthStatus;
  /** True when the caller's role clears `minimum` on the reviewer → admin ladder. */
  hasRole: (minimum: Role) => boolean;
  /** Resolves on success; rejects with the API's message on bad credentials. */
  signIn: (email: string, password: string) => Promise<void>;
  signOut: () => Promise<void>;
};

const ROLE_RANK: Record<Role, number> = { reviewer: 1, admin: 2 };

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [principal, setPrincipal] = useState<Principal | null>(null);
  const [status, setStatus] = useState<AuthStatus>("checking");

  // Any 401 from any endpoint means this session is gone (expired, or the server
  // restarted with an ephemeral AUTH_SECRET). Drop the principal and let the
  // route guard take over, instead of leaving a signed-out user staring at a UI
  // whose every action fails.
  useEffect(() => {
    setUnauthorizedHandler(() => {
      setPrincipal(null);
      setStatus("anonymous");
    });
    return () => setUnauthorizedHandler(null);
  }, []);

  useEffect(() => {
    let active = true;
    fetchPrincipal()
      .then((found) => {
        if (!active) return;
        setPrincipal(found);
        setStatus(found ? "authenticated" : "anonymous");
      })
      .catch(() => {
        // The bootstrap couldn't reach the API (backend still starting, proxy
        // down). Treat it as signed out: the login page is the one screen that
        // works without a session, and its own submit will report the real error.
        if (!active) return;
        setPrincipal(null);
        setStatus("anonymous");
      });
    return () => {
      active = false;
    };
  }, []);

  const signIn = useCallback(async (email: string, password: string) => {
    const found = await loginRequest(email, password);
    setPrincipal(found);
    setStatus("authenticated");
  }, []);

  const signOut = useCallback(async () => {
    // Clear local state even if the request fails — the user asked to be signed
    // out, and the cookie is httpOnly so the UI is all we control here.
    try {
      await logoutRequest();
    } finally {
      setPrincipal(null);
      setStatus("anonymous");
    }
  }, []);

  const hasRole = useCallback(
    (minimum: Role) => !!principal && ROLE_RANK[principal.role] >= ROLE_RANK[minimum],
    [principal]
  );

  return (
    <AuthContext.Provider value={{ principal, status, hasRole, signIn, signOut }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside <AuthProvider>");
  return value;
}

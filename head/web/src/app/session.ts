import type { Me } from "../api";

/**
 * The signed-in User, as a module singleton for synchronous reads (the rail's admin filter, the
 * Queue's cancel-authorization). It is populated by AuthGate before any gated page renders, and pages
 * only ever render while a session is live — so a read is never stale or empty in practice.
 *
 * For anything that must REACT to sign-out (unmount, redraw), use `useAuth()` instead; this object does
 * not notify.
 */
export const session = {
  username: "",
  isAdmin: false,
};

export function setSession(me: Me) {
  session.username = me.username;
  session.isAdmin = me.is_admin;
}

import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { changePassword, getMe, login, logout as apiLogout, type Me } from "../api";
import { clearSession, setSession } from "./session";

/**
 * The auth gate. Sign-in is required for everything — there is no anonymous surface — so this wraps the
 * whole routed app. The abyss shader lives OUTSIDE it (in the Shell), so the water is behind the
 * sign-in screen too; only the rail and the pages are gated.
 *
 * A cookie session may already be live from a previous visit, so we ask /me before showing a form the
 * user doesn't need.
 */

type Auth = { me: Me; logout: () => Promise<void> };
const AuthContext = createContext<Auth | null>(null);

/** The signed-in user, inside the gate. Throws if used above it — which is a bug, not a state. */
export function useAuth(): Auth {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth used outside AuthGate");
  return ctx;
}

export function AuthGate({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  // The password typed at sign-in, kept in memory ONLY to feed forced rotation (which needs it as
  // old_password and must never ask for it twice). Never stored, dropped the moment rotation succeeds.
  const [signInPw, setSignInPw] = useState<string | null>(null);
  const [booting, setBooting] = useState(true);

  useEffect(() => {
    getMe()
      .then(setMe)
      .catch(() => setMe(null))
      .finally(() => setBooting(false));
  }, []);

  useEffect(() => {
    if (me) setSession(me);
  }, [me]);

  const logout = async () => {
    await apiLogout();
    setSignInPw(null);
    setMe(null);
    clearSession();
  };

  if (booting) return null; // a blank frame over the shader; /me is one quick call

  if (!me || (me.must_change_password && signInPw === null)) {
    return (
      <SignIn
        onSignedIn={(u, typed) => {
          setMe(u);
          setSignInPw(typed);
        }}
      />
    );
  }

  if (me.must_change_password) {
    return (
      <ChangePassword
        current={signInPw!}
        onDone={() => {
          setMe({ ...me, must_change_password: false });
          setSignInPw(null);
        }}
      />
    );
  }

  return <AuthContext.Provider value={{ me, logout }}>{children}</AuthContext.Provider>;
}

/** A form floating on the abyss — no panel, no border, the way the rail floats. */
function Gate({ title, children }: { title: ReactNode; children: ReactNode }) {
  return (
    <div className="relative flex min-h-screen items-center justify-center px-6">
      <div className="w-full max-w-sm">
        <h1
          className="mb-8 text-ink"
          style={{ fontWeight: 300, fontSize: 40, letterSpacing: "-0.02em" }}
        >
          {title}
        </h1>
        {children}
      </div>
    </div>
  );
}

function SignIn({ onSignedIn }: { onSignedIn: (me: Me, password: string) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const go = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onSignedIn(await login(username, password), password);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Gate
      title={
        <div className="flex flex-col -space-y-10">
          <div><span className="text-7xl">C</span>uttlefish</div>
          <div><span className="text-7xl">C</span>loud</div>
          <div><span className="text-7xl">C</span>luster</div>
        </div>
      }
    >
      <form onSubmit={go} className="space-y-4">
        <label className="block">
          <span className="label mb-1.5 block">Username</span>
          <input
            className="field"
            autoFocus
            autoComplete="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
        </label>
        <label className="block">
          <span className="label mb-1.5 block">Password</span>
          <input
            className="field"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        {error && <p className="text-[13px] text-[var(--alarm)]">{error}</p>}
        <button className="btn-primary w-1/3 mx-auto block" disabled={busy || !username || !password}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </Gate>
  );
}

/**
 * Forced rotation: a freshly-provisioned account holds a temp password it must replace before anything
 * else works. Asks for new + confirm only — never the password just typed one screen ago (two boxes
 * read as "new + confirm", so the temp password would land in the wrong one and /passwd would 403).
 */
function ChangePassword({ current, onDone }: { current: string; onDone: () => void }) {
  const [newPw, setNewPw] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const mismatch = confirm.length > 0 && newPw !== confirm;

  const go = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (newPw !== confirm) return setError("The two passwords do not match.");
    try {
      await changePassword(current, newPw);
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <Gate title="Choose a new password">
      <form onSubmit={go} className="space-y-4">
        <p className="text-[14px] text-ink-soft">
          Your account was created with a temporary password. Replace it before submitting work.
        </p>
        <label className="block">
          <span className="label mb-1.5 block">New password</span>
          <input
            className="field"
            type="password"
            autoFocus
            autoComplete="new-password"
            value={newPw}
            onChange={(e) => setNewPw(e.target.value)}
          />
        </label>
        <label className="block">
          <span className="label mb-1.5 block">Confirm new password</span>
          <input
            className="field"
            type="password"
            autoComplete="new-password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
          />
        </label>
        {(error || mismatch) && (
          <p className="text-[13px] text-[var(--alarm)]">
            {error ?? "The two passwords do not match."}
          </p>
        )}
        <button className="btn-primary w-full" disabled={!newPw || newPw !== confirm}>
          Change password
        </button>
      </form>
    </Gate>
  );
}

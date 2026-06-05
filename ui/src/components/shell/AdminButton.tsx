import { useState } from "preact/hooks";
import { role, adminConfigured, unlock, lock } from "../../lib/auth";

/** Lock/unlock control in the top nav.
 *  Viewer → a 🔒 button that reveals an inline password field to enter the
 *  admin secret. Admin → an "Admin" badge + Unlock(lock) button. */
export function AdminButton() {
  const [open, setOpen] = useState(false);
  const [secret, setSecret] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(false);

  if (role.value === "admin") {
    return (
      <button
        type="button"
        class="admin-badge admin-badge--on"
        title="You have admin access — click to lock (return to viewer)"
        onClick={() => lock()}
      >
        <span class="admin-dot" aria-hidden="true" />
        Admin · Lock
      </button>
    );
  }

  // Admin not configured server-side → no way to unlock; show a hint, no field.
  if (!adminConfigured.value) {
    return (
      <span class="admin-badge admin-badge--none" title="No admin secret configured on the server">
        Viewer
      </span>
    );
  }

  async function submit(e: Event) {
    e.preventDefault();
    if (!secret.trim() || busy) return;
    setBusy(true);
    setErr(false);
    const ok = await unlock(secret);
    setBusy(false);
    if (ok) {
      setOpen(false);
      setSecret("");
    } else {
      setErr(true);
    }
  }

  if (!open) {
    return (
      <button
        type="button"
        class="admin-badge"
        title="Unlock admin to change settings"
        onClick={() => setOpen(true)}
      >
        <span class="admin-lock" aria-hidden="true">🔒</span> Admin
      </button>
    );
  }

  return (
    <form class={`admin-unlock${err ? " admin-unlock--err" : ""}`} onSubmit={submit}>
      <input
        type="password"
        class="admin-unlock-input"
        placeholder="Admin secret"
        autoFocus
        value={secret}
        disabled={busy}
        onInput={(e) => {
          setSecret((e.target as HTMLInputElement).value);
          setErr(false);
        }}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            setOpen(false);
            setSecret("");
            setErr(false);
          }
        }}
      />
      <button type="submit" class="admin-unlock-go" disabled={busy || !secret.trim()}>
        {busy ? "…" : "Unlock"}
      </button>
      {err && <span class="admin-unlock-msg" role="alert">Wrong secret</span>}
    </form>
  );
}

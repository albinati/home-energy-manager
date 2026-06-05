// Viewer/Admin role store.
//
// The UI is a passive VIEWER by default (read-only, no token) so it can be
// shared safely. An admin "unlocks" by entering the admin secret; we verify it
// against GET /whoami, persist it (lib/api.setAdminToken → localStorage) and
// flip `role` to "admin", which reveals the Settings + Journal tabs and the
// write controls. Server-side enforcement is the real boundary (ApiV1RoleAuth);
// this just drives the UI.
import { signal } from "@preact/signals";
import { getJson, getAdminToken, setAdminToken } from "./api";

export type Role = "viewer" | "admin";

export const role = signal<Role>(getAdminToken() ? "admin" : "viewer");
export const adminConfigured = signal<boolean>(true);
export const authEnforced = signal<boolean>(false);

type WhoAmI = { role: string; admin_configured?: boolean; auth_enforced?: boolean };

/** Re-check the role against the server. Called on boot: a stored admin token
 *  may have been rotated/revoked, in which case we silently drop to viewer. */
export async function refreshRole(): Promise<void> {
  try {
    const r = await getJson<WhoAmI>("/whoami");
    adminConfigured.value = r.admin_configured !== false;
    authEnforced.value = !!r.auth_enforced;
    const isAdmin = r.role === "admin";
    role.value = isAdmin ? "admin" : "viewer";
    if (!isAdmin && getAdminToken()) setAdminToken(null); // stale token → clear
  } catch {
    // /whoami unreachable — keep whatever we have (optimistic).
  }
}

/** Try to elevate to admin with `secret`. Returns true on success. */
export async function unlock(secret: string): Promise<boolean> {
  const prev = getAdminToken();
  setAdminToken(secret);
  try {
    const r = await getJson<WhoAmI>("/whoami");
    if (r.role === "admin") {
      role.value = "admin";
      return true;
    }
  } catch {
    // fall through to revert
  }
  setAdminToken(prev); // wrong secret / error → revert to prior state
  return false;
}

/** Drop back to viewer. */
export function lock(): void {
  setAdminToken(null);
  role.value = "viewer";
}

export function isAdmin(): boolean {
  return role.value === "admin";
}

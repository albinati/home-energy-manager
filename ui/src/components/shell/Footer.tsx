import { buildSha } from "../../lib/api";
import { QuotaChips } from "./QuotaChips";
import { role } from "../../lib/auth";

export function Footer() {
  const sha = buildSha();
  const shortSha = sha.length > 7 ? sha.slice(0, 7) : sha;
  const isAdmin = role.value === "admin";
  return (
    <footer class="footer">
      <div class="footer-inner">
        <span>
          HEM build <code>{shortSha}</code>
        </span>
        <span>•</span>
        <span title={isAdmin ? "Admin: write access enabled" : "Viewer: read-only"}>
          {isAdmin ? "Admin" : "Viewer"}
        </span>
        <span>•</span>
        <QuotaChips />
        <span class="grow"></span>
        <a href="https://github.com/albinati/home-energy-manager" rel="noreferrer">
          GitHub
        </a>
      </div>
    </footer>
  );
}

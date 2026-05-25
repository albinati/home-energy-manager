import { buildSha, hasBearer } from "../../lib/api";

export function Footer() {
  const sha = buildSha();
  const shortSha = sha.length > 7 ? sha.slice(0, 7) : sha;
  return (
    <footer class="footer">
      <div class="footer-inner">
        <span>
          HEM build <code>{shortSha}</code>
        </span>
        <span>•</span>
        <span title={hasBearer() ? "Authorization header attached to /api requests" : "No bearer configured"}>
          {hasBearer() ? "Authenticated" : "Unauthenticated"}
        </span>
        <span class="grow"></span>
        <a href="https://github.com/albinati/home-energy-manager" rel="noreferrer">
          GitHub
        </a>
      </div>
    </footer>
  );
}

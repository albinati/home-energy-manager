# OpenClaw skill — `home-energy-manager`

This directory tracks the OpenClaw skill that teaches OpenClaw how to interact
with HEM. The skill lives **in this repo** so it ships in lockstep with the
MCP tool surface it documents.

## Why it's here

Before 2026-05-15 the skill was a flat file at
`/root/.openclaw/skills/home-energy-manager/SKILL.md` on the production host,
edited manually whenever HEM features changed. It drifted ~17 days behind the
code (PR #284, #307, #308, #310, #311, #321, #324, #325, #331, #333 all
landed before anyone noticed the skill no longer described them).

The fix: keep the canonical skill in version control here. The skill +
the MCP tools it references move together; PRs that add or rename a tool
update both files in the same change.

## Publishing the skill to the live OpenClaw instance

OpenClaw reads from `/root/.openclaw/skills/home-energy-manager/SKILL.md` on
the prod host. To publish a new version:

```bash
# From a checkout of home-energy-manager on a machine with prod SSH access:
scp docs/openclaw-skill/SKILL.md \
    root@<hem-host>.ts.net:/root/.openclaw/skills/home-energy-manager/SKILL.md

# Optionally back up the old one first:
ssh root@<hem-host>.ts.net \
    'cp /root/.openclaw/skills/home-energy-manager/SKILL.md{,.bak-$(date +%Y-%m-%d)}'
```

OpenClaw re-reads skills on its next process start. To force immediate
re-read, restart the OpenClaw gateway (`systemctl restart openclaw-gateway`
or equivalent — depends on how OpenClaw is run; if in doubt, ask the user).

This is intentionally a manual `scp` (not auto-deploy) so the skill update is
a deliberate cutover; the live instance keeps the old skill until you push
the new one.

## Conventions

When changing this skill:

* Bump the "Recent changes (v11+, …)" section at the end with the new
  PR(s) covered. Keep the section ordered newest-last so the chronological
  story is readable.
* If a new MCP tool was added in the same PR that adds skill content for
  it, mention the tool name verbatim — OpenClaw uses these as anchors when
  matching natural-language requests to tool calls.
* If a tool was renamed or removed, update or delete references in the
  upper sections too — don't just append a "deprecated" note.
* Keep the tone scannable and tool-anchored. The skill is read by an LLM,
  not by a developer; long-form explanations should yield to "tool name +
  one-line summary" patterns where possible.

## File contents

* `SKILL.md` — the canonical OpenClaw skill, in the YAML-frontmatter +
  markdown format OpenClaw expects.

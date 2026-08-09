#!/usr/bin/env python3
"""
Import xct-agent personas into a LiteLLM/tokenhub xct-skills registry.

Every agent markdown file becomes one row in ``LiteLLM_SkillsTable``
(``source='custom'``) via the ``/v1/xct-skills`` CRUD API:

    markdown body (after frontmatter)  ->  system_prompt_template
    frontmatter.name                   ->  display_title
    frontmatter.description            ->  description
    category / emoji / vibe / slug     ->  xct_metadata

This is the data migration behind "Persona mode": once the rows exist, a chat
completion can pass ``{"skills": ["<skill_id>"], ...}`` and the proxy prepends
the persona as a system message (litellm/proxy/skill_endpoints/injection.py).

The importer is idempotent. Rows written by this script carry
``xct_metadata.source_repo = "xct-agents"`` and ``xct_metadata.xct_agent_slug``;
re-running matches on that slug and PATCHes instead of creating a duplicate.
Unchanged agents (same content fingerprint) are skipped without a write.

Requires:
  - LiteLLM proxy >= the build that exposes /v1/xct-skills (tokenhub does)
  - A proxy-admin key, read from the environment (never a CLI flag):

        export LITELLM_MASTER_KEY=sk-...     # or LITELLM_API_KEY

Usage:
    # always look first — dry-run performs read-only GETs, never writes
    python import_skills.py --litellm-base https://tokenhub.xcity.ai --dry-run

    # then import for real
    python import_skills.py --litellm-base https://tokenhub.xcity.ai

    # useful extras
    python import_skills.py --category engineering --limit 5 --dry-run
    python import_skills.py --export-map slug-to-skill-id.json

By default agents are read from the git ref ``origin/main`` (the branch that
carries all categories) rather than the checked-out worktree; pass
``--ref worktree`` to import whatever is on disk.

Note: the API assigns ``skill_id`` (a server-side uuid) — it cannot be set by
the client, so the agent slug lives in ``xct_metadata.xct_agent_slug`` and
``--export-map`` dumps the slug -> skill_id mapping for downstream consumers.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
    import yaml
except ImportError:
    print("pip install requests pyyaml", file=sys.stderr)
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Top-level directories that hold docs/tooling rather than agent personas.
NON_AGENT_TOP_DIRS = {".github", "docs", "examples", "integrations", "scripts"}

SOURCE_REPO = "xct-agents"
META_SLUG_KEY = "xct_agent_slug"
META_REPO_KEY = "source_repo"

# xct_metadata keys that must not influence the change-detection fingerprint
# (they either move on every commit or are written by the proxy itself).
VOLATILE_META_KEYS = {
    "content_sha256",
    "imported_at",
    "source_commit",
    "published",
    "published_at",
    "published_by",
}

RETRY_STATUSES = {429, 500, 502, 503, 504}


# ---------------------------------------------------------------------------
# Reading agents out of the repo
# ---------------------------------------------------------------------------


def slugify(name: str) -> str:
    """Same rule as register_agents.py — keeps agent identities aligned."""
    name = name.lower()
    name = re.sub(r"[^a-z0-9]+", "-", name)
    return name.strip("-")


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def resolve_commit(ref: str) -> str:
    if ref == "worktree":
        try:
            return git("rev-parse", "HEAD").strip() + "-worktree"
        except subprocess.CalledProcessError:
            return "worktree"
    return git("rev-parse", "--verify", ref).strip()


def list_markdown(ref: str) -> list[str]:
    """Repo-relative paths of every candidate agent markdown file."""
    if ref == "worktree":
        paths = [
            str(p.relative_to(REPO_ROOT))
            for p in sorted(REPO_ROOT.rglob("*.md"))
            if ".git" not in p.parts
        ]
    else:
        paths = git("ls-tree", "-r", "--name-only", ref).split("\n")
    return sorted(
        p
        for p in paths
        if p.endswith(".md") and "/" in p and p.split("/")[0] not in NON_AGENT_TOP_DIRS
    )


def read_source(ref: str, path: str) -> str:
    if ref == "worktree":
        return (REPO_ROOT / path).read_text(encoding="utf-8")
    return git("show", f"{ref}:{path}")


def parse_agent(text: str, path: str) -> dict | None:
    """Frontmatter + body -> agent dict, or None when this isn't an agent file."""
    if not text.startswith("---"):
        return None
    end = text.find("---", 3)
    if end == -1:
        return None
    try:
        fm = yaml.safe_load(text[3:end])
    except yaml.YAMLError:
        return None
    if not fm or not isinstance(fm, dict) or "name" not in fm:
        return None
    body = text[end + 3 :].strip()
    if not body:
        return None
    parts = path.split("/")
    return {
        "path": path,
        "name": str(fm.get("name", "")).strip(),
        "description": str(fm.get("description", "") or "").strip(),
        "emoji": str(fm.get("emoji", "") or "").strip(),
        "vibe": str(fm.get("vibe", "") or "").strip(),
        "color": str(fm.get("color", "") or "").strip(),
        "tools": fm.get("tools") or [],
        "category": parts[0],
        "subcategory": parts[1] if len(parts) > 2 else "",
        "system_prompt": body,
        "slug": slugify(str(fm.get("name", Path(path).stem))),
    }


def collect_agents(ref: str, category: str | None = None) -> list[dict]:
    agents: list[dict] = []
    for path in list_markdown(ref):
        if category and path.split("/")[0] != category:
            continue
        agent = parse_agent(read_source(ref, path), path)
        if agent:
            agents.append(agent)
    return agents


# ---------------------------------------------------------------------------
# Desired skill state
# ---------------------------------------------------------------------------


def build_metadata(agent: dict, ref: str, commit: str) -> dict:
    meta = {
        META_REPO_KEY: SOURCE_REPO,
        META_SLUG_KEY: agent["slug"],
        "category": agent["category"],
        "kind": "agent-persona",
        "license": "MIT",
        "source_path": agent["path"],
        "source_ref": ref,
        "source_commit": commit,
    }
    if agent["subcategory"]:
        meta["subcategory"] = agent["subcategory"]
    for key in ("emoji", "vibe", "color"):
        if agent[key]:
            meta[key] = agent[key]
    if agent["tools"]:
        meta["tools"] = agent["tools"]
    return meta


def build_desired(agent: dict, args: argparse.Namespace, commit: str) -> dict:
    """The full state we want the skill row to have."""
    title = f"{args.title_prefix}{agent['name']}" if args.title_prefix else agent["name"]
    return {
        "display_title": title,
        "description": agent["description"] or agent["vibe"] or agent["name"],
        "system_prompt_template": agent["system_prompt"],
        "version": args.version,
        "is_public": not args.private,
        "xct_metadata": build_metadata(agent, args.ref, commit),
    }


def fingerprint(desired: dict) -> str:
    """Stable hash of the content we manage; volatile metadata excluded."""
    meta = {
        k: v for k, v in desired["xct_metadata"].items() if k not in VOLATILE_META_KEYS
    }
    canonical = {
        "display_title": desired["display_title"],
        "description": desired["description"],
        "system_prompt_template": desired["system_prompt_template"],
        "version": desired["version"],
        "is_public": desired["is_public"],
        "xct_metadata": meta,
    }
    blob = json.dumps(canonical, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------


class SkillsAPI:
    def __init__(self, base: str, key: str, timeout: int = 60):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        )

    def _request(self, method: str, path: str, **kwargs):
        url = f"{self.base}{path}"
        last = None
        for attempt in range(3):
            try:
                resp = self.session.request(
                    method, url, timeout=self.timeout, **kwargs
                )
            except requests.RequestException as e:
                last = f"network error: {e.__class__.__name__}"
                continue
            if resp.status_code in RETRY_STATUSES and attempt < 2:
                last = f"{resp.status_code}: {resp.text[:160]}"
                continue
            return resp
        raise RuntimeError(last or "request failed")

    def list_all(self) -> list[dict]:
        """Every source='custom' skill, following the cursor pagination."""
        rows: list[dict] = []
        cursor = None
        while True:
            params: dict = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            resp = self._request("GET", "/v1/xct-skills", params=params)
            if not resp.ok:
                raise RuntimeError(
                    f"GET /v1/xct-skills failed ({resp.status_code}): {resp.text[:200]}"
                )
            body = resp.json()
            rows.extend(body.get("data") or [])
            if not body.get("has_more"):
                return rows
            cursor = body.get("next_cursor")
            if not cursor:
                return rows

    def create(self, payload: dict) -> dict:
        resp = self._request("POST", "/v1/xct-skills", json=payload)
        if not resp.ok:
            raise RuntimeError(f"{resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def patch(self, skill_id: str, payload: dict) -> dict:
        resp = self._request("PATCH", f"/v1/xct-skills/{skill_id}", json=payload)
        if not resp.ok:
            hint = ""
            if resp.status_code == 409:
                hint = " (row is published — content fields are frozen)"
            raise RuntimeError(f"{resp.status_code}{hint}: {resp.text[:200]}")
        return resp.json()


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def index_existing(rows: list[dict]) -> tuple[dict[str, dict], list[dict]]:
    """slug -> row for rows this importer owns; plus duplicate rows."""
    by_slug: dict[str, dict] = {}
    duplicates: list[dict] = []
    for row in sorted(rows, key=lambda r: str(r.get("created_at") or "")):
        meta = row.get("xct_metadata") or {}
        if meta.get(META_REPO_KEY) != SOURCE_REPO:
            continue
        slug = meta.get(META_SLUG_KEY)
        if not slug:
            continue
        if slug in by_slug:
            duplicates.append(row)
        else:
            by_slug[slug] = row
    return by_slug, duplicates


def plan_action(agent: dict, desired: dict, existing: dict | None, force: bool) -> str:
    if existing is None:
        return "create"
    stored = (existing.get("xct_metadata") or {}).get("content_sha256")
    if not force and stored == fingerprint(desired):
        return "unchanged"
    return "update"


def create_payload(desired: dict, args: argparse.Namespace) -> dict:
    meta = dict(desired["xct_metadata"])
    meta["content_sha256"] = fingerprint(desired)
    meta["imported_at"] = datetime.now(timezone.utc).isoformat()
    payload = {
        "display_title": desired["display_title"],
        "description": desired["description"],
        "system_prompt_template": desired["system_prompt_template"],
        "version": desired["version"],
        "is_public": desired["is_public"],
        "xct_metadata": meta,
    }
    if args.team_id:
        payload["team_id"] = args.team_id
    return payload


def patch_payload(desired: dict, existing: dict) -> dict:
    # PATCH overwrites xct_metadata wholesale, so merge on top of whatever the
    # proxy already stored (publish flags, operator annotations, ...).
    meta = dict(existing.get("xct_metadata") or {})
    meta.update(desired["xct_metadata"])
    meta["content_sha256"] = fingerprint(desired)
    meta["imported_at"] = datetime.now(timezone.utc).isoformat()
    return {
        "display_title": desired["display_title"],
        "description": desired["description"],
        "system_prompt_template": desired["system_prompt_template"],
        "version": desired["version"],
        "is_public": desired["is_public"],
        "xct_metadata": meta,
    }


def jinja_risky(agent: dict) -> bool:
    """Bodies with Jinja markers may be mangled by the injection renderer."""
    body = agent["system_prompt"]
    return "{{" in body or "{%" in body


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--litellm-base",
        default=os.environ.get("LITELLM_BASE_URL", "http://localhost:4000"),
        help="Proxy base URL (env: LITELLM_BASE_URL).",
    )
    parser.add_argument(
        "--ref",
        default="origin/main",
        help="Git ref to read agents from, or 'worktree' for the working tree.",
    )
    parser.add_argument("--category", help="Only import agents from this category dir.")
    parser.add_argument("--limit", type=int, help="Max agents to process (testing).")
    parser.add_argument("--version", default="1", help="Skill version string.")
    parser.add_argument("--team-id", help="Own the created rows with this team_id.")
    parser.add_argument(
        "--private",
        action="store_true",
        help="Create rows with is_public=false (default: public, so non-admin "
        "keys can discover them via GET /v1/xct-skills).",
    )
    parser.add_argument(
        "--title-prefix",
        default="",
        help="Prefix for display_title, e.g. 'XCT Agent — ' to make imported "
        "rows visually distinct in downstream catalogs.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-push every agent even when the content fingerprint matches.",
    )
    parser.add_argument(
        "--export-map",
        help="Write a JSON {slug: skill_id} map to this path after the run.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the plan without writing. Read-only GETs still run when a "
        "key is available so create/update/unchanged is accurate.",
    )
    args = parser.parse_args()

    key = os.environ.get("LITELLM_MASTER_KEY") or os.environ.get("LITELLM_API_KEY")
    if not key and not args.dry_run:
        print(
            "Set LITELLM_MASTER_KEY (or LITELLM_API_KEY) in the environment.",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        commit = resolve_commit(args.ref)
        agents = collect_agents(args.ref, args.category)
    except subprocess.CalledProcessError as e:
        print(f"git failed: {e.stderr.strip()[:200]}", file=sys.stderr)
        sys.exit(2)

    if args.limit:
        agents = agents[: args.limit]

    print(f"Source: {args.ref} @ {commit[:12]} — {len(agents)} agents")
    print(f"Target: {args.litellm_base}/v1/xct-skills")
    if args.dry_run:
        print("Mode:   DRY RUN (no writes)")

    api = SkillsAPI(args.litellm_base, key) if key else None
    by_slug: dict[str, dict] = {}
    duplicates: list[dict] = []
    other_rows = 0
    if api:
        try:
            rows = api.list_all()
        except (RuntimeError, ValueError) as e:
            print(f"Could not list existing skills: {e}", file=sys.stderr)
            sys.exit(2)
        by_slug, duplicates = index_existing(rows)
        other_rows = len(rows) - len(by_slug) - len(duplicates)
        print(
            f"Registry: {len(rows)} custom skills "
            f"({len(by_slug)} previously imported, {other_rows} other)"
        )
    else:
        print("Registry: not queried (no key in env) — every agent shown as 'create'")

    counts = {"create": 0, "update": 0, "unchanged": 0, "failed": 0}
    failures: list[tuple[str, str]] = []
    slug_to_id: dict[str, str] = {
        slug: row.get("skill_id", "") for slug, row in by_slug.items()
    }
    risky = [a["slug"] for a in agents if jinja_risky(a)]
    total = len(agents)

    for i, agent in enumerate(agents, 1):
        desired = build_desired(agent, args, commit)
        existing = by_slug.get(agent["slug"])
        action = plan_action(agent, desired, existing, args.force)
        prefix = f"[{i:>3}/{total}]"

        if action == "unchanged":
            counts["unchanged"] += 1
            print(f"{prefix} =  {agent['slug']}: unchanged")
            continue

        if args.dry_run:
            counts[action] += 1
            target = existing.get("skill_id") if existing else "(new skill_id)"
            print(
                f"{prefix} {'+' if action == 'create' else '~'}  {agent['slug']}: "
                f"would {action} -> {target} "
                f"[{len(desired['system_prompt_template'])} chars, "
                f"{agent['category']}]"
            )
            continue

        try:
            if action == "create":
                row = api.create(create_payload(desired, args))
                slug_to_id[agent["slug"]] = row.get("skill_id", "")
                print(f"{prefix} ✓  {agent['slug']}: created {row.get('skill_id')}")
            else:
                row = api.patch(existing["skill_id"], patch_payload(desired, existing))
                slug_to_id[agent["slug"]] = row.get("skill_id", "")
                print(f"{prefix} ✓  {agent['slug']}: updated {row.get('skill_id')}")
            counts[action] += 1
        except (RuntimeError, ValueError) as e:
            counts["failed"] += 1
            failures.append((agent["slug"], str(e)[:200]))
            print(f"{prefix} ✗  {agent['slug']}: {str(e)[:160]}", file=sys.stderr)

    print(
        f"\nDone — created: {counts['create']}, updated: {counts['update']}, "
        f"unchanged: {counts['unchanged']}, failed: {counts['failed']}"
    )

    if duplicates:
        print(
            f"\nWARNING: {len(duplicates)} duplicate rows share an agent slug "
            "(older imports or manual copies). They are left untouched:"
        )
        for row in duplicates[:20]:
            meta = row.get("xct_metadata") or {}
            print(f"  - {meta.get(META_SLUG_KEY)}: {row.get('skill_id')}")
        if len(duplicates) > 20:
            print(f"  ... and {len(duplicates) - 20} more")

    if risky:
        print(
            f"\nNote: {len(risky)} personas contain Jinja markers "
            "({{ }} / {% %}); the proxy's prompt renderer falls back to the raw "
            "template for those (skill_endpoints/injection.py::_render_prompt): "
            + ", ".join(risky[:8])
            + (" ..." if len(risky) > 8 else "")
        )

    if failures:
        print(f"\nFailed agents ({len(failures)}) — safe to re-run, the importer "
              "skips everything that already matches:")
        for slug, err in failures:
            print(f"  - {slug}: {err}")

    if args.export_map:
        if args.dry_run:
            print(f"\n(dry run) would write slug map to {args.export_map}")
        else:
            Path(args.export_map).write_text(
                json.dumps(dict(sorted(slug_to_id.items())), indent=2) + "\n",
                encoding="utf-8",
            )
            print(f"\nWrote slug -> skill_id map: {args.export_map}")

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()

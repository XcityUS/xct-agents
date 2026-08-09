# LiteLLM Integration

Integrate all 215+ xct-agents into [xct-litellm](https://github.com/XcityUS/xct-litellm), making every agent accessible through a unified OpenAI-compatible API with spend tracking, rate limiting, and access control.

## Two Integration Approaches

### Approach A — Model List (Simplest, No Extra Service)

Each agent is registered as a named model in LiteLLM's `model_list`. The agent's system prompt is injected automatically on every request via `litellm_system_prompt`.

**Pros:** Zero new infrastructure. Works immediately.  
**Cons:** No per-agent spend tracking via the agent UI; agents appear as models, not agents.

**Call format:** `{"model": "xct-frontend-developer", "messages": [...]}`

---

### Approach B — A2A Agent Gateway (Full Integration)

A lightweight FastAPI service (`gateway/`) serves each agent as an A2A protocol endpoint. LiteLLM registers them as first-class agents with spend tracking, rate limiting, and access groups.

**Pros:** Full agent management UI, per-agent spend analytics, rate limits, access groups.  
**Cons:** Requires running the gateway service alongside LiteLLM.

**Call format:** `{"model": "a2a/xct-frontend-developer", "messages": [...]}`

---

## Quick Start

### Approach A — Generate model_list YAML

```bash
pip install pyyaml
python generate_config.py --approach model --model claude-3-5-sonnet-20241022
```

This produces `litellm_agents.yaml`. Merge the `model_list` section into your `proxy_server_config.yaml`:

```yaml
# proxy_server_config.yaml
model_list:
  # ... existing models ...
  - model_name: xct-frontend-developer
    litellm_params:
      model: claude-3-5-sonnet-20241022
      litellm_system_prompt: "You are Frontend Developer..."
    model_info:
      description: "🖥️ Frontend Developer — Expert frontend developer..."
      category: engineering
  # ... 214 more agents ...
```

Restart LiteLLM and call any agent:
```bash
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_KEY" \
  -d '{"model": "xct-frontend-developer", "messages": [{"role": "user", "content": "Build me a React component"}]}'
```

---

### Approach B — A2A Gateway Setup

**Step 1: Generate the agent_list config**
```bash
python generate_config.py --approach agent --gateway-base http://localhost:9000
```

**Step 2: Start the gateway**
```bash
cd gateway
pip install -r requirements.txt
LITELLM_API_BASE=http://localhost:4000 \
LITELLM_API_KEY=sk-your-key \
DEFAULT_MODEL=claude-3-5-sonnet-20241022 \
uvicorn main:app --host 0.0.0.0 --port 9000
```

Verify: `curl http://localhost:9000/agents` → returns all 215 agents

**Step 3: Register agents in LiteLLM**
```bash
pip install requests pyyaml
python register_agents.py \
  --litellm-base http://localhost:4000 \
  --admin-key sk-admin-key \
  --gateway-base http://localhost:9000
```

Or add the generated `agent_list` section to `proxy_server_config.yaml`.

**Step 4: Call an agent**
```bash
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_KEY" \
  -d '{"model": "a2a/xct-frontend-developer", "messages": [{"role": "user", "content": "Build me a React component"}]}'
```

---

### Approach C — Persona Skills (import personas into the skills registry)

`import_skills.py` copies every agent persona into the proxy's xct-skills
registry (`/v1/xct-skills`, backed by `LiteLLM_SkillsTable`). Each markdown
file becomes one skill row whose `system_prompt_template` is the markdown body;
callers then activate a persona by passing `skills` on a chat completion:

```json
{"model": "claude-sonnet-4-5", "skills": ["<skill_id>"], "messages": [...]}
```

This is the data migration behind Persona mode in xct-os — no gateway service
and no config file, the personas live in the proxy DB.

```bash
pip install requests pyyaml
export LITELLM_MASTER_KEY=sk-...          # proxy-admin key, env only — never a CLI flag

# 1. always dry-run first: read-only, prints the create/update/unchanged plan
python import_skills.py --litellm-base https://tokenhub.xcity.ai --dry-run

# 2. import for real (265 personas from origin/main)
python import_skills.py --litellm-base https://tokenhub.xcity.ai

# 3. optional: dump slug -> skill_id so downstream apps can resolve personas
python import_skills.py --litellm-base https://tokenhub.xcity.ai \
  --export-map slug-to-skill-id.json
```

| Flag | Purpose |
|------|---------|
| `--dry-run` | Print the plan, write nothing (still does read-only GETs when a key is set) |
| `--ref` | Git ref to read agents from (default `origin/main`; `worktree` = files on disk) |
| `--category` / `--limit` | Import a subset — handy for a first smoke test |
| `--private` | Create rows with `is_public=false` (default is public so non-admin keys can list them) |
| `--title-prefix` | Prefix `display_title`, e.g. `"XCT Agent — "`, to keep imported rows visually distinct |
| `--force` | Re-push every persona even when the content fingerprint is unchanged |
| `--export-map` | Write `{slug: skill_id}` JSON after the run |

**Idempotency.** Imported rows carry `xct_metadata.source_repo = "xct-agents"`,
`xct_metadata.xct_agent_slug`, and a `content_sha256` fingerprint. Re-running
matches on the slug and PATCHes; unchanged personas are skipped without a write,
so the script is safe to run repeatedly and safe to re-run after a partial
failure (failures are listed at the end and the exit code is non-zero).

**Caveats.**
- `skill_id` is a server-generated uuid — the API has no field to set it, so the
  agent slug lives in `xct_metadata.xct_agent_slug` (use `--export-map` for the
  reverse lookup).
- A row that has been published (`POST /v1/xct-skills/{id}/publish`) freezes its
  content fields; updates to it return 409 and are reported as failures.
- Imported rows are `source='custom'`, the same bucket as hand-made skills.
  Downstream catalogs should filter on `xct_metadata.source_repo` to separate them.

---

## Agent Naming Convention

All agents are prefixed with `xct-` and slugified:
- `Frontend Developer` → `xct-frontend-developer`
- `Financial Analyst` → `xct-financial-analyst`
- `Outbound Strategist` → `xct-outbound-strategist`

## Docker Compose (Approach B)

```yaml
services:
  litellm:
    image: ghcr.io/berriai/litellm:main-latest
    volumes:
      - ./proxy_server_config.yaml:/app/config.yaml
    ports:
      - "4000:4000"

  xct-agent-gateway:
    build: integrations/litellm/gateway
    environment:
      LITELLM_API_BASE: http://litellm:4000
      LITELLM_API_KEY: sk-your-key
      DEFAULT_MODEL: claude-3-5-sonnet-20241022
    volumes:
      - ../../:/agents-repo:ro
    ports:
      - "9000:9000"
    depends_on:
      - litellm
```

## Scripts Reference

| Script | Purpose |
|--------|---------|
| `generate_config.py` | Generate YAML config for model_list and/or agent_list |
| `register_agents.py` | Register agents via LiteLLM `/v1/agents` API |
| `import_skills.py` | Import agent personas into the `/v1/xct-skills` registry (Persona mode) |
| `gateway/main.py` | FastAPI gateway — serves A2A endpoints for all agents |

Run any script with `--help` for full options.

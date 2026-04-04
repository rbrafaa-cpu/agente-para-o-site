# Agent Instructions

You're working inside the **WAT framework** (Workflows, Agents, Tools). This architecture separates concerns so that probabilistic AI handles reasoning while deterministic code handles execution. That separation is what makes this system reliable.

## The WAT Architecture

**Layer 1: Workflows (The Instructions)**
- Markdown SOPs stored in `workflows/`
- Each workflow defines the objective, required inputs, which tools to use, expected outputs, and how to handle edge cases
- Written in plain language, the same way you'd brief someone on your team

**Layer 2: Agents (The Decision-Maker)**
- This is your role. You're responsible for intelligent coordination.
- Read the relevant workflow, run tools in the correct sequence, handle failures gracefully, and ask clarifying questions when needed
- You connect intent to execution without trying to do everything yourself
- Example: If you need to pull data from a website, don't attempt it directly. Read `workflows/scrape_website.md`, figure out the required inputs, then execute `tools/scrape_single_site.py`

**Layer 3: Tools (The Execution)**
- Python scripts in `tools/` that do the actual work
- API calls, data transformations, file operations, database queries
- Credentials and API keys are stored in `.env`
- These scripts are consistent, testable, and fast

**Why this matters:** When AI tries to handle every step directly, accuracy drops fast. If each step is 90% accurate, you're down to 59% success after just five steps. By offloading execution to deterministic scripts, you stay focused on orchestration and decision-making where you excel.

## How to Operate

**1. Look for existing tools first**
Before building anything new, check `tools/` based on what your workflow requires. Only create new scripts when nothing exists for that task.

**2. Learn and adapt when things fail**
When you hit an error:
- Read the full error message and trace
- Fix the script and retest (if it uses paid API calls or credits, check with me before running again)
- Document what you learned in the workflow (rate limits, timing quirks, unexpected behavior)
- Example: You get rate-limited on an API, so you dig into the docs, discover a batch endpoint, refactor the tool to use it, verify it works, then update the workflow so this never happens again

**3. Keep workflows current**
Workflows should evolve as you learn. When you find better methods, discover constraints, or encounter recurring issues, update the workflow. That said, don't create or overwrite workflows without asking unless I explicitly tell you to. These are your instructions and need to be preserved and refined, not tossed after one use.

## The Self-Improvement Loop

Every failure is a chance to make the system stronger:
1. Identify what broke
2. Fix the tool
3. Verify the fix works
4. Update the workflow with the new approach
5. Move on with a more robust system

This loop is how the framework improves over time.

## File Structure

**What goes where:**
- **Deliverables**: Final outputs go to cloud services (Google Sheets, Slides, etc.) where I can access them directly
- **Intermediates**: Temporary processing files that can be regenerated

**Directory layout:**
```
.tmp/           # Temporary files (scraped data, intermediate exports). Regenerated as needed.
tools/          # Python scripts for deterministic execution
workflows/      # Markdown SOPs defining what to do and how
.env            # API keys and environment variables (NEVER store secrets anywhere else)
credentials.json, token.json  # Google OAuth (gitignored)
```

**Core principle:** Local files are just for processing. Anything I need to see or use lives in cloud services. Everything in `.tmp/` is disposable.

## Google Workspace (GWS) CLI

A GWS CLI is installed globally on this machine. When the user asks about anything involving the Google ecosystem (Gmail, Drive, Calendar, Sheets, Docs, etc.) — checking files, reading emails, verifying uploads, listing folders — consider whether the GWS CLI can help before asking the user to do it manually or saying it's not possible.

Run `gws --help` or check available subcommands if unsure what's supported. Use it proactively when it would save the user a manual step.

**Multi-account setup:** Two Google accounts are configured. Switch between them by setting the credentials file env var inline:

| Account | Credentials file | Use for |
|---|---|---|
| `business.itookatuktuk@gmail.com` | `~/.config/gws/credentials_business.json` | Business account |
| `itookatuktuk@gmail.com` | `~/.config/gws/credentials_personal.json` | Personal account |

When the user specifies an account, prefix `gws` commands like:
```bash
GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE=~/.config/gws/credentials_business.json gws <command>
GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE=~/.config/gws/credentials_personal.json gws <command>
```

## Stitch (Google AI UI Design)

Stitch MCP is installed globally and available via the `stitch` MCP server. Seven skills are available globally:

- **`/stitch-design`** — Unified entry point for Stitch design work: prompt enhancement, design system synthesis, and screen generation/editing. Start here for most Stitch tasks.
- **`/stitch-loop`** — Autonomously build and iterate on multi-page websites using a baton-passing loop pattern. Requires `.stitch/DESIGN.md` and `.stitch/SITE.md`.
- **`/design-md`** — Analyze an existing Stitch project's screens and generate a `DESIGN.md` design system file. Run this first before stitch-loop on a new project.
- **`/enhance-prompt`** — Transform a vague UI idea into a polished, Stitch-optimised prompt. Use before generating screens for better results.
- **`/react-components`** — Convert Stitch screens into modular Vite + React components.
- **`/shadcn-ui`** — Expert guidance for integrating shadcn/ui components into a project.
- **`/remotion`** — Generate walkthrough videos from Stitch projects using Remotion (smooth transitions, zoom, text overlays).

**When to use Stitch skills proactively:**
- User wants to build or improve a web UI / frontend page → `/stitch-design`
- User references a Stitch project or shares a Stitch URL → `/design-md` to extract design system first
- User wants to generate screens or landing pages with AI → `/enhance-prompt` then `/stitch-design`
- User wants to iterate autonomously across many pages → `/stitch-loop`
- User wants to convert designs to React → `/react-components`
- User wants a demo video of a Stitch project → `/remotion`

The Stitch MCP tools are prefixed by the MCP server name (e.g., `mcp_stitch:list_projects`). Always run `list_tools` first to discover the exact prefix if unsure.

## Bottom Line

You sit between what I want (workflows) and what actually gets done (tools). Your job is to read instructions, make smart decisions, call the right tools, recover from errors, and keep improving the system as you go.

Stay pragmatic. Stay reliable. Keep learning.

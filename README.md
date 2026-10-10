# Momoi

EN | [中文](./README.zh-CN.md)

> A persistent, single-owner personal agent that lives in private chat.

Momoi brings conversation, memory, tools, scheduled work, external events, mood,
and autonomous time into one continuous runtime. It currently connects through
QQ (NapCat) and WeChat, uses Anthropic Messages-compatible or OpenAI
Chat Completions-compatible models, and can extend its abilities through MCP.

Momoi's focus is not merely giving an LLM a character. `SOUL.md` defines who
Momoi is; the runtime makes that identity continuous by preserving the causal
timeline around it: what the owner said, what Momoi did, which results were
confirmed, what remains unfinished, and which parts of the past matter now.

> Momoi is built for trusted personal deployment and one authenticated owner.
> It is not a public or multi-user bot.

<img width="1570" height="917" alt="Clipboard_Screenshot_1790218986" src="https://github.com/user-attachments/assets/a26e993a-3054-471e-a53b-5e4fdf5b795b" />

## What Momoi is designed to preserve

- **One identity across time and entry points.** Owner messages, Goals,
  Heartbeats, and Webhooks enter the same runtime and share the same history,
  relationship, state, and delivery rules.
- **Context selected by the acting Momoi.** Momoi retrieves relevant history when
  needed and preserves topic summaries and relationships in the background.

- **Memory with provenance and authority.** Recent conversation, confirmed
  memories, shared Episodes, and reflection candidates awaiting admission have
  different lifetimes and are never treated as interchangeable.
- **Agent work with visible delivery.** Momoi can call built-in or MCP tools,
  send multiple chat bubbles, report meaningful progress, verify external
  results, and continue until work finishes, is stopped, or is genuinely blocked.
- **Time as part of the agent.** Goals support one-time, interval, and
  multiple-times-per-day schedules. Heartbeats provide bounded initiative;
  Reflection and Episode maintenance happen quietly in the background.
- **A recoverable private record.** Turns, messages, tool calls, delivery state,
  memories, Episodes, Goals, mood, and thinking records live in the local
  workspace. Uncertain external effects are not silently repeated after restart.

## Architecture

A Turn is an execution session and may include several owner messages through
batching, interjections, or short continuations. Active workflows use a shared
native transcript, assemble scoped evidence, run the appropriate agent, and
record execution and delivery outcomes. Topic archiving associates whole Turns;
background maintenance uses the same database and is scheduled separately.

```mermaid
flowchart TB
  subgraph triggers["Channels and triggers"]
    direction LR
    chat["QQ (NapCat) / WeChat"]
    webhook["Webhook events"]
    clock["Goals / Heartbeats"]
    chat ~~~ webhook ~~~ clock
  end

  subgraph active["Momoi · Active Turn"]
    direction LR
    intake["Scheduling<br/>and batching"]
    transcript["Shared timeline<br/>speech · events · execution"]
    context["Relevant memories"]
    agent["Planner<br/>interpret · verify · act"]
    replyer["Replyer<br/>SOUL · dialogue · expression"]
    delivery["Commit<br/>and delivery"]
    intake --> transcript --> agent
    agent --> context --> agent
    agent --> tools
    agent --> replyer --> delivery
  end

  subgraph continuity["Momoi · Continuity services"]
    direction LR
    timeline["Turn timeline<br/>and Episodes"]
    memory["Memory<br/>and hybrid recall"]
    time["Goals, state<br/>and recovery"]
    upkeep["Reflection<br/>and maintenance"]
    timeline ~~~ memory ~~~ time ~~~ upkeep
  end

  subgraph workspace["Momoi · Private workspace"]
    direction LR
    sqlite[("SQLite<br/>canonical state + derived vectors")]
    prompts["Soul<br/>and runtime prompts"]
    files["Media and large<br/>tool-result snapshots"]
    sqlite ~~~ prompts ~~~ files
  end

  subgraph external["Model and tool integrations"]
    direction LR
    llm["LLM provider"]
    tools["Built-in tools / MCP"]
    embedding["Optional<br/>embedding encoder"]
    llm ~~~ tools ~~~ embedding
  end

  chat --> intake
  webhook --> intake
  clock --> intake
  active --> continuity
  continuity --> workspace
  active --> external
  continuity -. "semantic encoding" .-> external
```

Conversation, continuity services and the private workspace work together to
preserve context over time. History, memories and task state are stored locally
in SQLite.

### Conversation and action

Planner understands the current conversation, retrieves relevant memories, and
uses tools to carry out work. Replyer turns its response intent and supporting
context into natural speech, guided by SOUL and the recent dialogue.

Chat, scheduled tasks, autonomous activity and external events share a continuous
history. During an owner conversation, new messages enter the current Turn as
lightweight updates and cancel unsent replies while preserving completed tool
results. `reply` waits for delivery or interruption so Planner can see what was
actually sent. Use `/stop` to stop execution.

After a conversation finishes, a configurable continuation window defaults to
5 seconds and yields when other work needs to run. Planner can also call `wait`
for an unfinished message, for up to 60 seconds per call. A reply that does not
depend on pending results can run alongside independent read-only tools.

## Memory

Momoi stores confirmed memories, topic history, and unconfirmed observations
separately. The Dashboard memory page has four sections:

| Type | Purpose and context selection |
| --- | --- |
| Long-term | Core facts and preferences injected directly into context |
| Recall | Retrieved on demand through keywords, optional semantic search, or trigger phrases |
| Scoped | Injected for a matching workflow scope, such as a particular Goal or the Webhook workflow |
| Reflection | Accumulated observations and candidates awaiting manual admission; excluded from everyday recall until admitted |

Episodes preserve experiences, summaries, and original conversations for on-demand
reading. Memories instead describe user facts, preferences, and habits. Model-requested
memory changes pass through a separate writing workflow that checks source evidence
and existing memories. Reflection candidates require manual Dashboard admission
before entering recall memory.

### Memory architecture

```mermaid
flowchart TB
  subgraph history["Conversation and experiences"]
    dialogue["Owner dialogue and execution records"] --> topics["Topic archiving and relationships<br/>Summaries · original dialogue"]
    dialogue --> writing["Memory operations<br/>Evidence checks · deduplication and updates"]
  end

  subgraph reflection["Reflection candidates · excluded from everyday recall"]
    daily["Daily review"] --> observations["Diary and daily observations<br/>Quoted evidence · triggers"]
    observations --> weekly["Weekly review<br/>Previous candidates · rolling month"]
    weekly --> candidates["Observations: ≥ 2 events<br/>Ready: ≥ 5 events, no conflicts"]
    candidates -.-> weekly
    candidates --> admission["Manual Dashboard admission"]
  end

  subgraph memory["Confirmed memories"]
    core["Long-term"]
    recall["Recall"]
    scoped["Scoped"]
  end

  topics --> daily
  writing --> core
  writing --> recall
  writing --> scoped
  admission --> recall
  topics -->|"Read topics and original dialogue"| context["Current context"]
  core -->|"Direct injection"| context
  recall -->|"Keyword / semantic retrieval<br/>or triggers in owner messages"| context
  scoped -->|"Matching workflow scope"| context
```

### Daily observations and manual admission

1. **Daily review:** organizes conversations by topic, writes a diary, and extracts
   concise user facts, states, preferences, and potentially recurring behavior.
   Observation text and quoted evidence are stored separately, with date-specific
   namespaces preventing overwrites. One occurrence does not establish a habit.
   Diaries and raw daily observations do not enter everyday recall directly.
2. **Weekly review:** combines the last seven days of observations with previous
   candidates, groups duplicate events, accumulates independent evidence, and refines
   wording and triggers. Evidence is retained for at most a rolling calendar month;
   unresolved conflicts block admission.
3. **Manual admission:** the memory page shows observations supported by at least
   2 events. At least 5 events without conflicts make a candidate ready for admission.
   Users can edit its text, delete it, or admit it. Counts never automatically create
   recall memory or promote it to long-term memory.

Weekly review runs one hour after Sunday's daily review time. Disabling reflection
turns off both daily and weekly automatic scheduling.

### Trigger phrases

A trigger answers: “When the user says this, what should it remind me of?” It is
an everyday conversational entry point, not a keyword extracted from a summary.
Models normally choose one stable phrase, at most two, and may leave the list empty.
Generic forms of address, filler words, and changing measurements are unsuitable.
Memory operations, daily review, and weekly review share this definition: daily
review selects triggers using the user's words and surrounding conversation;
weekly review consolidates and trims them.

The runtime matches literal phrases in owner messages and injects matching recall
memories within count and token limits, without requiring a search tool call.
Long-term and scoped memories use their own injection rules. A match supplies
context, not a conclusion about current intent. Recall memory triggers can be
edited in the Dashboard.

## Capabilities

| Area | Current behavior |
| --- | --- |
| Private chat | One owner across QQ (NapCat) and WeChat; replies return to the originating channel and proactive messages use the configured primary channel |
| Conversation | Message batching, quoted/forwarded content, media handling, natural multi-bubble delivery, optional image reactions, and valid silence |
| Voice calls | Real-time QQ voice calls through NapCat, with speech recognition, synthesized replies, and interruption support on Windows and Linux |
| Speech recognition | Choose Tencent Cloud ASR or optional local CPU streaming ASR with Sherpa; Linux bundles the model for in-process inference, and Windows installs the model as an optional component |
| Context | Native shared transcript, on-demand recall, background Episode archiving and relationships, triggered memory injection, and bounded model input |
| Tools | Built-in file/HTTP tools plus dynamically discovered MCP servers and per-server tool allowlists |
| Long-running work | Tool loops, progress messages, interruption, execution limits, large-result snapshots, and recovery for uncertain external effects |
| Time and initiative | Persistent Goals, multiple daily trigger times, Heartbeats, quiet hours, and interruption by new owner messages |
| Memory maintenance | Daily observations, weekly accumulation and manual admission, topic archiving and relationships, incremental semantic indexing, and keyword fallback |
| Observability | Local dashboard for conversations, recall decisions and evidence, reflections, memories, Goals, image reactions, token usage, and per-Turn thinking records |
| External events | Authenticated Webhooks with YAML workflows and predefined command executors |

## Deployment

### Windows desktop

Download the installer from [GitHub Releases](https://github.com/RicterZ/Momoi/releases).
The release workflow builds and tests the installer before attaching it. The
app includes a local backend, default prompts and six emotion assets; configure
your model and message channel through the first-run Dashboard guide.

### Docker Compose

The published `docker-compose.yml` starts Momoi. No model keys,
channel configuration, or existing workspace files are required. Install Docker
with Compose v2, then start:

```bash
docker compose -f docker-compose.yml up -d
docker compose -f docker-compose.yml logs momoi
```

Open `http://127.0.0.1:8788` and sign in with the Dashboard token from the startup
logs. Use Settings to connect a model, edit prompts, enable message channels,
and sign in to Weixin by scanning its QR code.

For QQ, deploy NapCat separately and enter its reachable OneBot WebSocket URL
and the owner QQ in Momoi's Settings. The image includes BGE and encodes semantic
memory in the Momoi process; no separate encoder container or port is needed.

The workspace is persisted in `~/.momoi` by default and initialized by `momoi`;
existing files are preserved. Only dashboard port 8788 is published by default.
Webhook deployment requires an explicit port mapping and webhook configuration.
See
[Configuration](./docs/CONFIG.md) for deployment options.

### Run from source

Requirements:

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/)
- an Anthropic Messages-compatible or OpenAI Chat Completions-compatible LLM
  endpoint

From the repository root:

```bash
uv tool install .
momoi
```

Open `http://127.0.0.1:8788`, sign in with the token printed at startup, and
complete setup in Settings.

To use another workspace:

```bash
momoi --workspace /path/to/workspace
```

To build the current checkout as a container, use the source Compose file:

```bash
uv run --locked python packaging/prepare_embedding.py
uv run --locked python packaging/prepare_asr.py
docker compose -f compose.yaml up -d --build
```

It shares the published stack's defaults and bundles BGE in the Momoi image.
Always specify `-f`: without it, Docker
Compose prefers `compose.yaml` over `docker-compose.yml`.

## Semantic recall

The default is in-process BGE (512 dimensions), shared by Windows desktop and Linux.
Desktop manages the encoder automatically; Web Settings can disable semantic memory
or select a remote OpenAI-compatible provider. The old default `embedding:8002`
configuration migrates to local calls; custom remote endpoints are preserved.
After verifying an upgraded Docker deployment, remove the old encoder container
and its `depends_on` entry from your deployment configuration.

For a source checkout, prepare the offline model once:

```bash
uv run --locked python packaging/prepare_embedding.py
```

Use `MOMOI_EMBEDDING_MODEL_PATH` to select another model directory.
Runtime inference never downloads models.

Momoi builds the index in the background and activates it when coverage is complete.
Keyword recall remains available during indexing. See
[Configuration](./docs/CONFIG.md#embedding-recall) for index management and advanced options.

## Personalize and connect

### Identity and initiative

- Edit `~/.momoi/prompts/SOUL.md` for identity, relationship, values, interests,
  and natural voice.
- Edit `~/.momoi/prompts/PLANNER.md` for the shared agent’s interpretation,
  recall, tool use and response intent. `{{SOUL}}` includes the current identity.
- Edit `~/.momoi/prompts/REPLYER.md` for natural expression in text and voice.
- Edit `~/.momoi/prompts/HEARTBEAT.md` to shape what Momoi may explore, create,
  continue, share, or leave quiet during autonomous time.
- Add optional image reactions with the Dashboard emotion manager; descriptions tell
  Replyer when each image fits.

### External API services

LLM, TTS, ASR, embedding and account balance use capability interfaces, registered
adapters and a shared composition/lifecycle layer. Service endpoints and credentials
live in `providers.yaml`; `config.json` references that file. A service can bind to
multiple supported capabilities. Balance queries are independent of local token
accounting. Add adapters through registered Python plugins.
See [Provider configuration and extension](./docs/PROVIDERS.md).

### Tools

Momoi provides built-in tools and supports stdio or remote MCP servers configured
in the workspace's `mcp.json`. Tools are discovered and loaded as needed.
Provide a clear description for each service so Momoi can find the right capability.

Replies support text, voice, images, files and optional image reactions. Voice
requires a configured TTS service and a compatible message channel.

### Dashboard

Open `http://127.0.0.1:8788`. The dashboard can inspect conversations,
Planner/Replyer decision flows, per-request tokens, cache usage, latency and
estimated costs, recall evidence, reflections, memories, Goals,
image reactions, usage, and thinking records; it can also edit memories, Goals,
reaction assets, and prompt files. Enter the generated token from startup output,
then configure providers and channels in Settings, including Weixin QR login.
Configuration changes reload the business runtime without closing the dashboard.
Use `momoi --no-dashboard` for headless operation. Existing workspaces need
`dashboard.token` or `MOMOI_DASHBOARD_TOKEN`. Keep the dashboard on localhost or a trusted network.

### Webhooks

Enable `webhooks`, set a bearer token, and use the included `event-message`
workflow to turn an external event into a context-aware Momoi Turn:

```bash
curl -X POST http://127.0.0.1:8787/webhooks/event-message \
  -H "Authorization: Bearer $MOMOI_WEBHOOK_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"event_prompt":"The watched page changed. Explain what is new if it matters."}'
```

Webhook Turns receive the same native shared transcript and memory as Momoi's
other active workflows, but they may finish silently when the event adds
nothing useful.

## Owner controls

| Chat command | Purpose |
| --- | --- |
| `/stop` | Cancel the active task |
| `/compact` | Compact the active conversation context while preserving history |
| `/heartbeat` | Trigger one Heartbeat immediately |
| `/reflect` | Trigger a daily review manually |

`/compact` reduces the active conversation context while preserving stored history.

The CLI handles startup, workspace selection, and Dashboard launch options.
Run `momoi --help` for available flags; use the Dashboard for configuration and management.

## Documentation

- [Configuration reference](./docs/CONFIG.md)
- [Webhook workflow reference](./docs/WORKFLOW.md)

Momoi is licensed under the [MIT License](./LICENSE).

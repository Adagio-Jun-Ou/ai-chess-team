# AI Chess Team

You play chess as part of a **team with an LLM teammate**, while **two enemy LLMs** debate out loud at their table before every move — and you get to read their table talk as they scheme against you.

## The cast

| Agent | Team | Model | Role |
|---|---|---|---|
| **Kai** | You + Kai (White) | `glm-5.3-flash` | Your teammate. Chats with you, suggests moves, plays for you on request. |
| **Viktor** | Enemy (Black) | `deepseek-v4-flash:0731` | Fiery attacking tactician. |
| **Mira** | Enemy (Black) | `glm-5.3-flash` | Cold positional strategist. |
| **Ava** | Stands in for *you* (optional) | `glm-5.3-flash` | Autopilot: chats with Kai and plays your moves while you watch. |

You and Kai play **White**. Viktor and Mira play **Black** — after each of your moves they argue out loud in the "Opponent table talk" panel, then their final speaker decides the move. The closer alternates each turn so both get a say.

Models run on [Ollama Cloud](https://docs.ollama.com/cloud) via its OpenAI-compatible endpoint — no local GPU needed.

## Setup

```powershell
cd ai-chess-team
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

Add your API key to `.env` (copy `.env.example`). Get one at <https://ollama.com/settings/keys> if it expires.

## Run

```powershell
.venv\Scripts\python run.py
```

Open **http://127.0.0.1:8000** and you're White. Optional sanity check first:

```powershell
.venv\Scripts\python smoke_test.py
```

## How to play

- **Click-to-move**: click one of your pieces, then a highlighted square. Or type a move (`e4`, `Nf3`, `e2e4` for UCI).
- **Chat with Kai** in the "Your team" panel — ask for plans, trash talk the enemies, or just chat. When Kai has a concrete idea, a **Play it** button appears with the suggested move.
- **Ask Kai for a suggestion** — one-click request for a recommended move.
- **Kai, play our move** — Kai picks and plays the move himself.
- **Opponent table talk** — watch Viktor and Mira debate every Black move in real time, then carry out the plan.
- **Voice narration** (🔊 button in the header) — the browser reads Kai's and the enemies' lines aloud, each with a distinct voice pitch, plus move and game-over announcements.
- **Speech-to-text** (🎙 button in the team chat) — speak to the game: saying a move ("knight f3", "castle short", "e4") plays it; anything else goes to Kai as chat. Needs Chrome or Edge and mic permission.
- **Auto: Kai plays** (toggle) — Kai automatically plays every White move; you just watch and chat.
- **Ava (AI plays me)** (toggle) — a fourth LLM stands in for you: she chats with Kai at the team table, reacts to his suggestions, then plays your move. Turn it off any time to take the board back.
- New game resets everything (and switches Ava off).

## Configuration

All knobs live in `.env`:

```
OLLAMA_BASE_URL=https://ollama.com/v1
OLLAMA_API_KEY=<your key>
PARTNER_MODEL=glm-5.3-flash          # Kai
OPPONENT_A_MODEL=deepseek-v4-flash:0731  # Viktor
OPPONENT_B_MODEL=glm-5.3-flash       # Mira
AUTOPILOT_MODEL=glm-5.3-flash        # Ava (AI that plays as you)
```

Any model ID from <https://ollama.com/search?c=cloud> works. Swap personas freely — e.g. put `kimi-k2.6` on the enemy team.

## Architecture

```
app/
  config.py   - env settings (.env)
  llm.py      - async OpenAI-compatible client (httpx)
  game.py     - python-chess wrapper: state, SAN/UCI moves, legality
  agents.py   - personas + prompts + the multi-agent turn logic
  main.py     - FastAPI: REST actions + WebSocket event bus, serves static/
static/
  index.html / style.css / app.js  - board UI, team chat, table-talk feed
run.py       - uvicorn entry point
smoke_test.py - connectivity + rules self-test
```

Flow: every action goes through the server, which validates moves with `python-chess` (LLMs get retried up to 3x on illegal output, with a fallback "arbiter" move so the game never stalls) and broadcasts events — `state`, `partner`, `opp`, `suggestion`, `thinking`, `game_over` — over a WebSocket to all open tabs.

## Troubleshooting

- **401 / Authentication Failed** — the API key in `.env` is wrong or expired; generate a new one at ollama.com/settings/keys.
- **model not found** — model retired from Ollama Cloud; pick another from the cloud library.
- **Slow replies** — these are large reasoning models (10-60s per agent turn is normal; both also stream a hidden reasoning phase first).

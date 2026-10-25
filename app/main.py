"""AI Chess Team - FastAPI server: serves the UI, runs the agent conversations."""

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, engine
from .agents import AutopilotAgent, OpponentDuo, PartnerAgent
from .game import ChessGame
from .llm import LLMClient, LLMError

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class Session:
    def __init__(self):
        self.game = ChessGame()
        self.partner_client = LLMClient(
            config.OLLAMA_BASE_URL, config.OLLAMA_API_KEY, config.PARTNER_MODEL, config.REQUEST_TIMEOUT
        )
        self.viktor_client = LLMClient(
            config.OLLAMA_BASE_URL, config.OLLAMA_API_KEY, config.OPPONENT_A_MODEL, config.REQUEST_TIMEOUT
        )
        self.mira_client = LLMClient(
            config.OLLAMA_BASE_URL, config.OLLAMA_API_KEY, config.OPPONENT_B_MODEL, config.REQUEST_TIMEOUT
        )
        self.ava_client = LLMClient(
            config.OLLAMA_BASE_URL, config.OLLAMA_API_KEY, config.AUTOPILOT_MODEL, config.REQUEST_TIMEOUT
        )
        self.partner = PartnerAgent(self.partner_client)
        self.duo = OpponentDuo(self.viktor_client, self.mira_client)
        self.ava = AutopilotAgent(self.ava_client)
        self.partner_log = []  # [{role: 'user'|'kai', name?: str, text}]
        self.opp_log = []  # [{name, text}]
        self.suggestion: Optional[dict] = None
        self.opponent_thinking = False
        self.partner_thinking = False
        self.autopilot = False
        self.autopilot_busy = False
        self.spectate = config.SPECTATE
        self.clients: Set[WebSocket] = set()

    def reset(self):
        self.game.reset()
        self.partner.reset()
        self.duo.reset()
        self.ava.reset()
        self.partner_log = []
        self.opp_log = []
        self.suggestion = None
        self.opponent_thinking = False
        self.partner_thinking = False
        self.autopilot = False
        self.autopilot_busy = False


session: Optional[Session] = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global session
    session = Session()
    if session.spectate:
        session.autopilot = True
        asyncio.create_task(_spectate_kickoff())
    yield
    for client in (session.partner_client, session.viktor_client, session.mira_client, session.ava_client):
        await client.aclose()


async def _spectate_kickoff() -> None:
    """In spectate mode, start the 2v2 match a moment after boot."""
    await asyncio.sleep(2)
    if session.autopilot and not session.game.state()["game_over"]:
        asyncio.create_task(run_autopilot_turn())


app = FastAPI(title="AI Chess Team", lifespan=lifespan)


async def broadcast(event: dict) -> None:
    if session is None:
        return
    dead = []
    for ws in list(session.clients):
        try:
            await ws.send_json(event)
        except Exception:
            dead.append(ws)
    for ws in dead:
        session.clients.discard(ws)


def snapshot() -> dict:
    return {
        "type": "snapshot",
        "state": session.game.state(),
        "partner_log": session.partner_log,
        "opp_log": session.opp_log,
        "suggestion": session.suggestion,
        "opponent_thinking": session.opponent_thinking,
        "partner_thinking": session.partner_thinking,
        "autopilot": session.autopilot,
    }


async def opp_emit(name: str, text: str) -> None:
    session.opp_log.append({"name": name, "text": text})
    if len(session.opp_log) > 150:
        del session.opp_log[:-150]
    await broadcast({"type": "opp", "name": name, "text": text})


async def broadcast_state_and_progress() -> None:
    """Push the new board state; chain opponent turn or announce game over."""
    state = session.game.state()
    await broadcast({"type": "state", "state": state})
    if state["game_over"]:
        await broadcast({"type": "game_over", "status": state["status"], "winner": state["winner"]})
    else:
        asyncio.create_task(run_opponent_turn())


async def run_opponent_turn() -> None:
    if session.opponent_thinking:
        return
    session.opponent_thinking = True
    await broadcast({"type": "thinking", "who": "opponents", "active": True})
    t0 = time.monotonic()
    try:
        san = await session.duo.take_turn(session.game, opp_emit)
        session.game.push(san)
        print(f"[TURN] Black played {san} in {time.monotonic() - t0:.1f}s", flush=True)
        session.suggestion = None
        await broadcast({"type": "clear_suggestion"})
        await broadcast({"type": "state", "state": session.game.state()})
        state = session.game.state()
        if state["game_over"]:
            await broadcast({"type": "game_over", "status": state["status"], "winner": state["winner"]})
        elif session.autopilot:
            asyncio.create_task(run_autopilot_turn())
    except Exception as exc:  # keep the game alive no matter what
        await broadcast({"type": "error", "text": f"Opponent team error: {exc}"})
    finally:
        session.opponent_thinking = False
        await broadcast({"type": "thinking", "who": "opponents", "active": False})


async def run_autopilot_turn() -> None:
    """Ava plays the human side: chats with Kai, then picks and plays White's move."""
    if not session.autopilot or session.autopilot_busy:
        return
    state = session.game.state()
    if state["game_over"] or state["turn"] != "white":
        return
    if session.opponent_thinking or session.partner_thinking:
        return

    session.autopilot_busy = True
    await broadcast({"type": "thinking", "who": "autopilot", "active": True})
    reply = None
    t0 = time.monotonic()
    try:
        try:
            kai_lines = [e["text"] for e in session.partner_log if e.get("role") == "kai"]
            chat_text, ava_move = await session.ava.open_turn(session.game, kai_lines)
        except LLMError as exc:
            chat_text, ava_move = "", None
            print(f"[AVA] open_turn failed: {exc}", flush=True)

        if chat_text:
            session.partner_log.append({"role": "user", "name": "Ava", "text": chat_text})
            await broadcast({"type": "partner", "role": "user", "name": "Ava", "text": chat_text})

            session.partner_thinking = True
            await broadcast({"type": "thinking", "who": "partner", "active": True})
            try:
                reply, suggestion = await session.partner.respond(
                    session.game, chat_text, mode="chat", opp_feed=session.opp_log
                )
            except LLMError as exc:
                reply, suggestion = f"(Kai lost connection: {exc})", None
            finally:
                session.partner_thinking = False
                await broadcast({"type": "thinking", "who": "partner", "active": False})

            session.partner_log.append({"role": "kai", "text": reply})
            await broadcast({"type": "partner", "role": "kai", "text": reply})
            if suggestion and not session.game.state()["game_over"]:
                session.suggestion = {"san": suggestion, "text": reply}
                await broadcast({"type": "suggestion", "san": suggestion, "text": reply})
        else:
            suggestion = None

        suggestion_san = session.suggestion["san"] if session.suggestion else None
        final = ava_move if (ava_move and session.game.is_legal(ava_move)) else None
        # only reconvene with Ava when Kai actually proposes a different move
        if suggestion_san and final and suggestion_san != final and session.game.is_legal(suggestion_san):
            try:
                final = await session.ava.decide(session.game, reply, suggestion_san) or final
            except LLMError:
                pass
        if not final or not session.game.is_legal(final):
            final = engine.best(session.game.board)
        print(f"[TURN] Ava played {final} in {time.monotonic() - t0:.1f}s", flush=True)
        session.game.push(final)
        session.suggestion = None
        await broadcast({"type": "clear_suggestion"})
        state = session.game.state()
        await broadcast({"type": "state", "state": state})
        if state["game_over"]:
            await broadcast({"type": "game_over", "status": state["status"], "winner": state["winner"]})
        else:
            asyncio.create_task(run_opponent_turn())
    except Exception as exc:  # absolute last resort: keep the game alive
        await broadcast({"type": "error", "text": f"Ava turn error: {exc}"})
        try:
            fallback = engine.best(session.game.board)
            if fallback:
                session.game.push(fallback)
                await broadcast({"type": "state", "state": session.game.state()})
                asyncio.create_task(run_opponent_turn())
        except Exception:
            session.autopilot = False
            await broadcast({"type": "autopilot", "enabled": False})
    finally:
        session.autopilot_busy = False
        await broadcast({"type": "thinking", "who": "autopilot", "active": False})


class ChatIn(BaseModel):
    text: str


class MoveIn(BaseModel):
    move: str


class AutopilotIn(BaseModel):
    enabled: bool


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/state")
async def get_state():
    return snapshot()


@app.post("/api/chat")
async def chat(inp: ChatIn):
    text = inp.text.strip()
    if not text:
        return JSONResponse(status_code=400, content={"detail": "Empty message"})
    if session.partner_thinking:
        return JSONResponse(status_code=409, content={"detail": "Kai is still typing..."})

    session.partner_log.append({"role": "user", "text": text})
    await broadcast({"type": "partner", "role": "user", "text": text})

    session.partner_thinking = True
    await broadcast({"type": "thinking", "who": "partner", "active": True})
    try:
        reply, suggestion = await session.partner.respond(
            session.game, text, mode="chat", opp_feed=session.opp_log
        )
    except LLMError as exc:
        reply, suggestion = f"(Kai lost connection: {exc})", None
    finally:
        session.partner_thinking = False
        await broadcast({"type": "thinking", "who": "partner", "active": False})

    session.partner_log.append({"role": "kai", "text": reply})
    await broadcast({"type": "partner", "role": "kai", "text": reply})
    if suggestion and not session.game.state()["game_over"]:
        session.suggestion = {"san": suggestion, "text": reply}
        await broadcast({"type": "suggestion", "san": suggestion, "text": reply})
    return {"ok": True}


@app.post("/api/suggest")
async def suggest():
    if session.partner_thinking:
        return JSONResponse(status_code=409, content={"detail": "Kai is still typing..."})
    session.partner_thinking = True
    await broadcast({"type": "thinking", "who": "partner", "active": True})
    try:
        reply, suggestion = await session.partner.respond(
            session.game, "(The human clicks 'suggest a move'.)", mode="suggest", opp_feed=session.opp_log
        )
    except LLMError as exc:
        reply, suggestion = f"(Kai lost connection: {exc})", None
    finally:
        session.partner_thinking = False
        await broadcast({"type": "thinking", "who": "partner", "active": False})

    session.partner_log.append({"role": "kai", "text": reply})
    await broadcast({"type": "partner", "role": "kai", "text": reply})
    if suggestion and not session.game.state()["game_over"]:
        session.suggestion = {"san": suggestion, "text": reply}
        await broadcast({"type": "suggestion", "san": suggestion, "text": reply})
    return {"ok": True}


@app.post("/api/partner_move")
async def partner_move():
    state = session.game.state()
    if state["game_over"]:
        return JSONResponse(status_code=409, content={"detail": "Game is over"})
    if state["turn"] != "white":
        return JSONResponse(status_code=409, content={"detail": "It's Black's turn"})
    if session.opponent_thinking or session.partner_thinking or session.autopilot_busy:
        return JSONResponse(status_code=409, content={"detail": "Someone is already thinking..."})

    session.partner_thinking = True
    await broadcast({"type": "thinking", "who": "partner", "active": True})
    move = None
    try:
        reply, move = await session.partner.respond(
            session.game, "(The human says: 'Kai, you play this one.')", mode="play", opp_feed=session.opp_log
        )
    except (LLMError, ValueError) as exc:
        session.partner_thinking = False
        await broadcast({"type": "thinking", "who": "partner", "active": False})
        await broadcast({"type": "error", "text": f"Kai misfired ({exc}) - engine picks for him."})
        move = engine.best(session.game.board)
        reply = "(Kai shrugs - the engine picked for him.)"

    session.partner_log.append({"role": "kai", "text": reply})
    await broadcast({"type": "partner", "role": "kai", "text": reply})
    if not move or not session.game.is_legal(move):
        move = engine.best(session.game.board)
    try:
        san = session.game.push(move)
    except ValueError as exc:
        await broadcast({"type": "error", "text": str(exc)})
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    session.partner_thinking = False
    await broadcast({"type": "thinking", "who": "partner", "active": False})
    session.suggestion = None
    await broadcast({"type": "clear_suggestion"})
    state = session.game.state()
    if state["game_over"]:
        await broadcast({"type": "game_over", "status": state["status"], "winner": state["winner"]})
    else:
        asyncio.create_task(run_opponent_turn())
    return {"ok": True, "san": san}


@app.post("/api/move")
async def user_move(inp: MoveIn):
    state = session.game.state()
    if state["game_over"]:
        return JSONResponse(status_code=409, content={"detail": "Game is over - start a new one"})
    if state["turn"] != "white":
        return JSONResponse(status_code=409, content={"detail": "It's Black's turn"})
    if session.opponent_thinking or session.autopilot_busy:
        return JSONResponse(status_code=409, content={"detail": "Someone is already thinking..."})

    try:
        san = session.game.push(inp.move)
    except ValueError as exc:
        await broadcast({"type": "error", "text": str(exc)})
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    session.suggestion = None
    await broadcast({"type": "clear_suggestion"})
    state = session.game.state()
    await broadcast({"type": "state", "state": state})
    if state["game_over"]:
        await broadcast({"type": "game_over", "status": state["status"], "winner": state["winner"]})
    else:
        asyncio.create_task(run_opponent_turn())
    return {"ok": True, "san": san}


@app.post("/api/autopilot")
async def autopilot_toggle(inp: AutopilotIn):
    session.autopilot = inp.enabled
    session.spectate = inp.enabled  # remember the preference across new games
    await broadcast({"type": "autopilot", "enabled": inp.enabled})
    if inp.enabled:
        state = session.game.state()
        if (
            not state["game_over"]
            and state["turn"] == "white"
            and not (session.opponent_thinking or session.partner_thinking or session.autopilot_busy)
        ):
            asyncio.create_task(run_autopilot_turn())
    return {"ok": True, "autopilot": session.autopilot}


@app.post("/api/new_game")
async def new_game():
    spectating = session.spectate
    session.reset()
    session.spectate = spectating
    if spectating:
        session.autopilot = True
    await broadcast({"type": "autopilot", "enabled": session.autopilot})
    await broadcast({"type": "feeds_cleared"})
    await broadcast(snapshot())
    if session.autopilot:
        asyncio.create_task(run_autopilot_turn())
    return {"ok": True}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    session.clients.add(ws)
    try:
        await ws.send_json(snapshot())
        while True:
            await ws.receive_text()  # ignore client pings
    except WebSocketDisconnect:
        pass
    finally:
        session.clients.discard(ws)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

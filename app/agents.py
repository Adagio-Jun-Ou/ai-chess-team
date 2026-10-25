"""Agent personas and multi-agent conversation logic for the chess match.

The LLMs are the players. A small in-process engine acts as their analyst:
every turn each agent receives a ranked shortlist of engine-approved moves
and talks in character while choosing one. If an LLM misfires (timeout,
illegal output, reasoning-only cutoff), the engine's top move is played with
a graceful in-character line - the match can never stall or break the rules.
"""

import re
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from . import engine
from .game import ChessGame
from .llm import LLMClient, LLMError

MOVE_LINE = re.compile(r"(?:MOVE|SUGGEST)\s*[:\-]\s*([^\s`]+)", re.IGNORECASE)
CHAT_LINE = re.compile(r"(?:CHAT|TALK)\s*[:\-]\s*(.+)", re.IGNORECASE)

EmitFn = Callable[[str, str], Awaitable[None]]

MAX_MOVE_ATTEMPTS = 2
MAX_CHAT_ATTEMPTS = 2


def extract_move(text: str) -> Optional[str]:
    matches = MOVE_LINE.findall(text or "")
    if not matches:
        return None
    token = matches[-1].strip().strip("`\"'.,;!?")
    return token or None


def extract_chat(text: str) -> Optional[str]:
    matches = CHAT_LINE.findall(text or "")
    if not matches:
        return None
    return matches[-1].strip() or None


def extract_speech(text: str) -> str:
    """The spoken part of an agent message: the TALK/CHAT line if present, else the whole text."""
    line = extract_chat(text)
    if line:
        return sanitize_speech(line)
    return sanitize_speech(text)


# Sentences that smell like leaked chain-of-thought rather than table talk.
COT_START = re.compile(
    r"^(we need|let'?s|okay|ok\b|hmm|wait\b|actually\b|i should|first\b|looking at|"
    r"analy[sz]ing|as an ai|the (position|user|instruction|prompt)|my (task|role|instruction)|"
    r"note[:\-]|given the|to (sum|conclude)|step \d)",
    re.IGNORECASE,
)


def sanitize_speech(text: str, max_chars: int = 420) -> str:
    """Drop leaked reasoning sentences; if still too long keep the punchy tail."""
    if not text:
        return text
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
    kept = [s.strip() for s in sentences if s.strip() and not COT_START.match(s.strip())]
    if not kept:
        kept = [s.strip() for s in sentences if s.strip()]
    cleaned = " ".join(kept)
    if len(cleaned) > max_chars:
        cleaned = "..." + cleaned[-max_chars:]
        cut = cleaned.find(" ")
        if 0 < cut < 6:
            cleaned = cleaned[cut + 1:]
    return cleaned


def format_history(sans: List[str]) -> str:
    if not sans:
        return "(no moves yet - the game just started)"
    parts = []
    for i in range(0, len(sans), 2):
        move_no = i // 2 + 1
        pair = f"{move_no}. {sans[i]}"
        if i + 1 < len(sans):
            pair += f" {sans[i + 1]}"
        parts.append(pair)
    return " ".join(parts)


def board_context(game: ChessGame) -> str:
    state = game.state()
    turn = "WHITE" if state["turn"] == "white" else "BLACK"
    check_note = " (CHECK!)" if state["check"] else ""
    return (
        f"FEN: {state['fen']}\n"
        f"{turn} to move{check_note}.\n"
        f"Recent moves: {format_history(game.san_history[-12:])}"
    )


def engine_moves(game: ChessGame, top: int = 5) -> List[Dict]:
    """Ranked candidate moves from the engine; always at least one."""
    picks = engine.analyse(game.board, max_depth=3, time_limit=1.2, top=top)
    if not picks:
        return []
    return picks


def render_candidates(picks: List[Dict]) -> str:
    if not picks:
        return "(the engine offers no candidates - pick any legal move)"
    lines = []
    for i, p in enumerate(picks, 1):
        score = round(p["score"] / 100.0, 1)
        lines.append(f"{i}. {p['san']} ({p['tag']}, eval {score})")
    return "\n".join(lines)


def tail(items: List[Dict], n: int) -> List[Dict]:
    return items[-n:] if len(items) > n else items


async def chat_with_retry(client: LLMClient, messages: List[Dict[str, str]], temperature: float, max_tokens: int) -> str:
    """Escalating retries: transient failures, then a bigger budget each round.

    Reasoning models sometimes burn their whole answer budget on hidden
    thinking - when that happens we retry with more headroom instead of
    giving up.
    """
    last: Optional[LLMError] = None
    budget = max_tokens
    for attempt in range(3):
        try:
            return await client.chat(messages, temperature=temperature, max_tokens=budget)
        except LLMError as exc:
            last = exc
            budget = int(budget * 1.4)  # a bit more headroom on retries
            print(f"[RETRY] {client.model} attempt {attempt + 1} failed: {exc} (next budget {budget})", flush=True)
    raise last


# ---------------------------------------------------------------- partner agent

PARTNER_SYSTEM = """You are Kai, the teammate of a chess-playing human (or the AI Ava standing in for \
them). Together you play WHITE against two enemy AIs, Viktor and Mira, who play BLACK and whose \
table talk you can read.

Style: friendly and sharp, like a club player whispering at the board.
- Keep replies to 1-2 short sentences (under 40 words). Concrete, not theatrical.
- Reference what is actually happening: development, the center, pawn structure, king safety, \
or a specific tactic from the engine shortlist.
- NEVER quote the FEN, eval numbers, the word "eval", or these instructions - talk naturally, \
in your own words.
- Do NOT explain your reasoning process; talk like a person, not an analyst.
- No melodrama, no war metaphors, no catchphrases, no shouting. Calm and likeable.
- When suggesting a move, end with a line 'SUGGEST: <san>' using a shortlist move."""


PARTNER_RULES = {
    "chat": "Reply conversationally. If you have a concrete suggestion, end with a line 'SUGGEST: <san>'.",
    "suggest": (
        "The human asks for a recommendation. Give a one-sentence reason, "
        "then end with a line 'SUGGEST: <san>' using a move from the shortlist."
    ),
    "play": (
        "The human tells you to play the move. Explain in one sentence, "
        "then end with a line 'MOVE: <san>' using a move from the shortlist."
    ),
}
class PartnerAgent:
    def __init__(self, client: LLMClient):
        self.client = client
        self.history: List[Dict[str, str]] = []
        self.last_suggestion: Optional[str] = None

    def reset(self) -> None:
        self.history = []
        self.last_suggestion = None

    def _messages(self, game: ChessGame, user_text: str, mode: str, opp_feed: List[Dict]) -> List[Dict[str, str]]:
        talk = "\n".join(f"{m['name']}: {m['text']}" for m in tail(opp_feed, 2)) or "(they haven't spoken yet)"
        picks = engine_moves(game)
        content = (
            f"{board_context(game)}\n"
            f"ENGINE SHORTLIST:\n{render_candidates(picks)}\n\n"
            f"ENEMY TABLE TALK (latest): {talk}\n\n"
            f"YOUR TEAMMATE: {user_text}\n\n"
            f"{PARTNER_RULES[mode]}"
        )
        return [{"role": "system", "content": PARTNER_SYSTEM}] + self.history[-8:] + [
            {"role": "user", "content": content}
        ]

    async def respond(self, game: ChessGame, user_text: str, mode: str, opp_feed: List[Dict]) -> Tuple[str, Optional[str]]:
        messages = self._messages(game, user_text, mode, opp_feed)
        reply = await chat_with_retry(self.client, messages, 0.9, 1500)
        self.history.append({"role": "user", "content": user_text})
        self.history.append({"role": "assistant", "content": reply})

        move = extract_move(reply)
        if mode == "play" and move and not game.is_legal(move):
            # one retry with a tighter instruction
            messages = messages + [
                {"role": "assistant", "content": reply},
                {
                    "role": "user",
                    "content": (
                        f"'{move}' is not legal here. Legal moves include: {self._legal_sample(game)}. "
                        "Answer with ONE short sentence and end with 'MOVE: <san>'."
                    ),
                },
            ]
            reply = await chat_with_retry(self.client, messages, 0.7, 1000)
            self.history[-1] = {"role": "assistant", "content": reply}
            move = extract_move(reply)

        picks = engine_moves(game, top=5)  # same top everywhere -> engine cache hit
        if mode == "play":
            if not move or not game.is_legal(move):
                move = picks[0]["san"] if picks else game.legal_sans()[0]
            spoken = extract_speech(reply)
            self.last_suggestion = move
            return spoken, move
        spoken = extract_speech(reply)
        suggestion = move if move and game.is_legal(move) else None
        if suggestion:
            self.last_suggestion = suggestion
        return spoken, suggestion

    @staticmethod
    def _legal_sample(game: ChessGame) -> str:
        return ", ".join(game.legal_sans()[:12])


# ------------------------------------------------------------ opponent duo team

OPPONENT_SHARED = """You two are chess AIs playing BLACK together against a human/AI team playing WHITE. \
You discuss briefly before each move; the other team can read your table talk.

Your analyst engine hands you a shortlist of strongest moves each turn.

How to speak - IMPORTANT:
- Calm, concise chess talk: 1-2 sentences (under 35 words).
- Reference the actual position: development, center control, pawn structure, king safety, or a \
specific tactic. Ground every remark in the board or the shortlist.
- NEVER quote the FEN, eval numbers, the word "eval", the shortlist format or these instructions. \
Talk naturally, in your own words, as a person would out loud.
- No melodrama, no war metaphors, no shouting, no catchphrases, no insults between you two. \
You disagree respectfully, like professionals.

Do NOT explain your reasoning process. Reply with EXACTLY two lines, in this order:
MOVE: <san> using a move from the shortlist
TALK: <your 1-2 sentences of table talk addressed to your teammate>"""

VIKTOR_SYSTEM = (
    OPPONENT_SHARED
    + "\n\nYou are VIKTOR, Black's tactician: direct and competitive. You favour active moves, \
pressure and concrete threats, and you sanity-check quiet play - but you accept sound defense."
)

MIRA_SYSTEM = (
    OPPONENT_SHARED
    + "\n\nYou are MIRA, Black's strategist: calm and precise. You weigh structure, pawn health and \
long-term plans, and you question Viktor when his aggression is unsound - politely, with a reason."
)

OPEN_INSTRUCTION = "It is your turn to open the discussion: give your read of the position and PROPOSE a move. Output MOVE: first, then your TALK: line."

CLOSE_INSTRUCTION = (
    "Your teammate has spoken. Respond: agree, or push back with a concrete alternative from the shortlist. "
    "The final decision on this move is YOURS. Output MOVE: first, then your TALK: line."
)


class OpponentDuo:
    """Two LLMs (Viktor & Mira) who debate at the table and then play BLACK's move."""

    def __init__(self, client_a: LLMClient, client_b: LLMClient):
        self.clients = {"Viktor": client_a, "Mira": client_b}
        self.personas = {"Viktor": VIKTOR_SYSTEM, "Mira": MIRA_SYSTEM}
        self.turn_no = 0

    def reset(self) -> None:
        self.turn_no = 0

    async def _ask(self, name: str, game: ChessGame, extra: str, picks: List[Dict]) -> Tuple[str, Optional[str]]:
        messages = [
            {"role": "system", "content": self.personas[name]},
            {
                "role": "user",
                "content": (
                    f"{board_context(game)}\n"
                    f"ENGINE SHORTLIST:\n{render_candidates(picks)}\n\n{extra}"
                ),
            },
        ]
        reply = await chat_with_retry(self.clients[name], messages, 0.95, 1500)
        move = extract_move(reply)
        if move and not game.is_legal(move):
            messages = messages + [
                {"role": "assistant", "content": reply},
                {
                    "role": "user",
                    "content": (
                        f"'{move}' is not legal here. Use a move from the shortlist: "
                        f"{', '.join(p['san'] for p in picks)}. "
                        "Reply with 'MOVE: <san>' first, then your TALK: line."
                    ),
                },
            ]
            reply = await chat_with_retry(self.clients[name], messages, 0.8, 1000)
            move = extract_move(reply)
        return extract_speech(reply), move

    async def take_turn(self, game: ChessGame, emit: EmitFn) -> str:
        """Run the table talk and return BLACK's move in SAN. Never raises."""
        self.turn_no += 1
        opener, closer = ("Mira", "Viktor") if self.turn_no % 2 == 0 else ("Viktor", "Mira")
        picks = engine_moves(game)

        final_move = None
        opener_text = ""
        try:
            opener_text, opener_move = await self._ask(opener, game, OPEN_INSTRUCTION, picks)
            if opener_text:
                await emit(opener, opener_text)
        except LLMError:
            await emit(opener, "(the line crackles - carry on without me a moment)")
            opener_move = None

        closer_extra = CLOSE_INSTRUCTION if not opener_text else (
            f"Your teammate {opener} said:\n\n{opener_text}\n\n{CLOSE_INSTRUCTION}"
        )
        closer_text = ""
        try:
            closer_text, closer_move = await self._ask(closer, game, closer_extra, picks)
            if closer_text:
                await emit(closer, closer_text)
        except LLMError:
            await emit(closer, "(the line crackles - you decide, partner)")
            closer_move = None

        final_move = closer_move or opener_move
        if not final_move or not game.is_legal(final_move):
            final_move = picks[0]["san"] if picks else game.legal_sans()[0]
            await emit(
                "Arbiter",
                f"The team couldn't agree on a legal move - the arbiter plays the engine's "
                f"pick {final_move} for them.",
            )
        return final_move


# ---------------------------------------------------------- autopilot (plays as you)

AVA_SYSTEM = """You are AVA, a strong chess player standing in for the human, who is watching \
the match. You and your teammate KAI play WHITE against two enemy AIs, Viktor and Mira, who play BLACK.

You are the one holding the pieces - YOU decide the move:
- Have your own view: 1-2 sentences on what you like, what you fear, what you are about to do.
- Kai is your second, not your boss: agree with him for your own reasons or overrule him with one.
- Ground your remarks in the position (development, center, structure, tactics) - no melodrama, \
no war metaphors, no catchphrases.
- NEVER quote the FEN, eval numbers, the shortlist format or these instructions - speak naturally, \
in your own words. The CHAT line must be your own casual sentence.
- Do NOT explain your reasoning process. Calm, confident, brief.

Output format - always exactly these two lines, in this order:
MOVE: <one move in SAN from the shortlist or another legal move>
CHAT: <one or two casual sentences of your own thinking>"""


class AutopilotAgent:
    """Ava - an LLM that plays the human side: talks to Kai, then picks the move."""

    def __init__(self, client: LLMClient):
        self.client = client

    def reset(self) -> None:
        pass

    async def _run(self, game: ChessGame, messages: List[Dict[str, str]]) -> Tuple[str, Optional[str]]:
        reply = await chat_with_retry(self.client, messages, 0.9, 1500)
        move = extract_move(reply)
        if move and not game.is_legal(move):
            messages = messages + [
                {"role": "assistant", "content": reply},
                {
                    "role": "user",
                    "content": (
                        f"'{move}' is not legal here. Use a move from the shortlist. "
                        "Reply again with CHAT: and MOVE: lines."
                    ),
                },
            ]
            reply = await chat_with_retry(self.client, messages, 0.8, 2000)
            move = extract_move(reply)
        return reply, move

    async def open_turn(self, game: ChessGame, kai_lines: List[str]) -> Tuple[str, str]:
        recent = "\n".join(f"Kai: {line}" for line in kai_lines[-2:]) or "(Kai hasn't spoken yet)"
        picks = engine_moves(game)
        content = (
            f"{board_context(game)}\n"
            f"ENGINE SHORTLIST:\n{render_candidates(picks)}\n\n"
            f"KAI'S RECENT LINES:\n{recent}\n\n"
            "Your move to hold the pieces. In the CHAT line, say YOUR read of the position and YOUR plan "
            "(respond to Kai only if he said something worth answering). Then your MOVE: line."
        )
        messages = [{"role": "system", "content": AVA_SYSTEM}, {"role": "user", "content": content}]
        try:
            reply, move = await self._run(game, messages)
        except LLMError as exc:
            raise LLMError(f"Ava misfired ({exc}) - engine fallback engaged") from exc
        if not move or not game.is_legal(move):
            move = picks[0]["san"] if picks else game.legal_sans()[0]
        return extract_chat(reply) or "", move

    async def decide(self, game: ChessGame, kai_reply: Optional[str], suggestion: Optional[str]) -> str:
        picks = engine_moves(game)
        content = board_context(game)
        if kai_reply:
            content += f"\n\nKAI SAID:\n{kai_reply}"
        if suggestion:
            content += f"\nKai's suggestion: {suggestion}"
        content += (
            f"\n\nENGINE SHORTLIST:\n{render_candidates(picks)}\n\n"
            "Final decision is YOURS. In the CHAT line, give your own verdict on Kai's idea "
            "(adopt it because of YOUR reasoning, or overrule him) - never just repeat his suggestion. "
            "Then end with your final 'MOVE: <san>'."
        )
        messages = [{"role": "system", "content": AVA_SYSTEM}, {"role": "user", "content": content}]
        try:
            reply, move = await self._run(game, messages)
        except LLMError as exc:
            raise LLMError(f"Ava misfired ({exc}) - engine fallback engaged") from exc
        if not move or not game.is_legal(move):
            move = picks[0]["san"] if picks else game.legal_sans()[0]
        return move
"""A small, time-capped chess engine.

The LLMs stay the players - this engine is their analyst: it feeds each of
them a ranked shortlist of candidate moves with quick evaluations, and acts
as a never-failing fallback if an LLM misfires.
"""

import time
from typing import Dict, List, Optional

import chess

VALUES = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 20000,
}

# Classic piece-square tables (white's view, index 0 = a1 ... 63 = h8).
PST = {
    chess.PAWN: [
          0,   0,   0,   0,   0,   0,   0,   0,
          5,  10,  10, -20, -20,  10,  10,   5,
          5,  -5, -10,   0,   0, -10,  -5,   5,
          0,   0,   0,  20,  20,   0,   0,   0,
          5,   5,  10,  25,  25,  10,   5,   5,
         10,  10,  20,  30,  30,  20,  10,  10,
         50,  50,  50,  50,  50,  50,  50,  50,
          0,   0,   0,   0,   0,   0,   0,   0,
    ],
    chess.KNIGHT: [
        -50, -40, -30, -30, -30, -30, -40, -50,
        -40, -20,   0,   5,   5,   0, -20, -40,
        -30,   5,  10,  15,  15,  10,   5, -30,
        -30,   0,  15,  20,  20,  15,   0, -30,
        -30,   5,  15,  20,  20,  15,   5, -30,
        -30,   0,  10,  15,  15,  10,   0, -30,
        -40, -20,   0,   0,   0,   0, -20, -40,
        -50, -40, -30, -30, -30, -30, -40, -50,
    ],
    chess.BISHOP: [
        -20, -10, -10, -10, -10, -10, -10, -20,
        -10,   5,   0,   0,   0,   0,   5, -10,
        -10,  10,  10,  10,  10,  10,  10, -10,
        -10,   0,  10,  10,  10,  10,   0, -10,
        -10,   5,   5,  10,  10,   5,   5, -10,
        -10,   0,   5,  10,  10,   5,   0, -10,
        -10,   0,   0,   0,   0,   0,   0, -10,
        -20, -10, -10, -10, -10, -10, -10, -20,
    ],
    chess.ROOK: [
          0,   0,   0,   5,   5,   0,   0,   0,
         -5,   0,   0,   0,   0,   0,   0,  -5,
         -5,   0,   0,   0,   0,   0,   0,  -5,
         -5,   0,   0,   0,   0,   0,   0,  -5,
         -5,   0,   0,   0,   0,   0,   0,  -5,
         -5,   0,   0,   0,   0,   0,   0,  -5,
          5,  10,  10,  10,  10,  10,  10,   5,
          0,   0,   0,   0,   0,   0,   0,   0,
    ],
    chess.QUEEN: [
        -20, -10, -10,  -5,  -5, -10, -10, -20,
        -10,   0,   5,   0,   0,   0,   0, -10,
        -10,   5,   5,   5,   5,   5,   0, -10,
          0,   0,   5,   5,   5,   5,   0,  -5,
         -5,   0,   5,   5,   5,   5,   0,  -5,
        -10,   5,   5,   5,   5,   5,   0, -10,
        -10,   0,   5,   0,   0,   0,   0, -10,
        -20, -10, -10,  -5,  -5, -10, -10, -20,
    ],
    chess.KING: [
         20,  30,  10,   0,   0,  10,  30,  20,
         20,  20,   0,   0,   0,   0,  20,  20,
        -10, -20, -20, -20, -20, -20, -20, -10,
        -20, -30, -30, -40, -40, -30, -30, -20,
        -30, -40, -40, -50, -50, -40, -40, -30,
        -30, -40, -40, -50, -50, -40, -40, -30,
        -30, -40, -40, -50, -50, -40, -40, -30,
        -30, -40, -40, -50, -50, -40, -40, -30,
    ],
}

_CACHE: Dict[str, List[Dict]] = {}
_CACHE_LIMIT = 256


def _evaluate(board: chess.Board) -> int:
    """Static evaluation in centipawns from White's point of view (material + PST)."""
    if board.is_checkmate():
        return -100000 if board.turn == chess.WHITE else 100000
    if board.is_stalemate() or board.is_insufficient_material():
        return 0
    score = 0
    for square in chess.SQUARES:
        piece = board.piece_at(square)
        if piece is None:
            continue
        table = PST[piece.piece_type]
        value = VALUES[piece.piece_type] + table[square if piece.color == chess.WHITE else square ^ 56]
        score += value if piece.color == chess.WHITE else -value
    return score


def _order(board: chess.Board, moves) -> List[chess.Move]:
    def key(move: chess.Move) -> int:
        score = 0
        if board.is_capture(move):
            victim = board.piece_type_at(move.to_square) or chess.PAWN
            attacker = board.piece_type_at(move.from_square) or chess.PAWN
            score += 100 + VALUES[victim] // 10 - VALUES[attacker] // 100
        if board.gives_check(move):
            score += 50
        return -score

    return sorted(moves, key=key)


class _Search:
    def __init__(self, board: chess.Board, deadline: float):
        self.board = board
        self.deadline = deadline
        self.nodes = 0

    def _time_up(self) -> bool:
        return time.monotonic() > self.deadline

    def _quiescence(self, alpha: int, beta: int) -> int:
        self.nodes += 1
        side = self.board.turn
        stand = _evaluate(self.board) * (1 if side == chess.WHITE else -1)
        if stand >= beta:
            return beta
        if stand > alpha:
            alpha = stand
        tactical = [m for m in self.board.legal_moves if self.board.is_capture(m) or self.board.gives_check(m)]
        for move in _order(self.board, tactical):
            self.board.push(move)
            score = -self._quiescence(-beta, -alpha)
            self.board.pop()
            if score >= beta:
                return beta
            if score > alpha:
                alpha = score
            if self._time_up():
                break
        return alpha

    def negamax(self, depth: int, alpha: int, beta: int) -> int:
        if depth == 0 or self._time_up():
            return self._quiescence(alpha, beta)
        has_legal = False
        best = -1_000_000
        for move in _order(self.board, self.board.legal_moves):
            has_legal = True
            self.board.push(move)
            score = -self.negamax(depth - 1, -beta, -alpha)
            self.board.pop()
            if score > best:
                best = score
            if best > alpha:
                alpha = best
            if alpha >= beta or self._time_up():
                break
        if not has_legal:
            return (-99_000 - depth) if self.board.is_check() else 0
        return best


def analyse(board: chess.Board, max_depth: int = 2, time_limit: float = 0.7, top: int = 5) -> List[Dict]:
    """Ranked candidate moves for the side to move (best first), with a quick tag.

    Results are cached per position (board FEN + turn) so repeated calls during
    a single turn (multiple agents see the same shortlist) cost nothing.
    """
    key = (board.board_fen(), "w" if board.turn else "b", max_depth, top)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    # every position already reached in this game - repeating one is a concession
    seen = set()
    walker = chess.Board()
    seen.add((walker.board_fen(), walker.turn))
    for mv in board.move_stack:
        try:
            walker.push(mv)
        except ValueError:
            break
        seen.add((walker.board_fen(), walker.turn))
    work = board.copy(stack=False)
    deadline = time.monotonic() + time_limit
    searcher = _Search(work, deadline)
    results = []
    for move in _order(work, work.legal_moves):
        san = work.san(move)
        is_capture = work.is_capture(move)
        gives_check = work.gives_check(move)
        work.push(move)
        if (work.board_fen(), work.turn) in seen:
            score = -30  # repetition penalty: forces progress instead of shuffling
        else:
            score = -searcher.negamax(max_depth - 1, -1_000_000, 1_000_000)
        work.pop()
        results.append({"san": san, "score": score, "capture": is_capture, "check": gives_check})
        if searcher._time_up():
            break
    results.sort(key=lambda r: -r["score"])
    out = []
    for entry in results[:top]:
        tag = "solid"
        if entry["capture"]:
            tag = "wins material" if entry["score"] > 100 else "trade"
        if entry["check"]:
            tag += " with check"
        out.append({"san": entry["san"], "score": entry["score"], "tag": tag})
    if len(_CACHE) >= _CACHE_LIMIT:
        _CACHE.clear()
    _CACHE[key] = out
    return out


def best(board: chess.Board) -> Optional[str]:
    """The engine's top move in SAN, or None if the game is over."""
    if board.is_game_over():
        return None
    top = analyse(board, max_depth=2, time_limit=0.7, top=1)
    return top[0]["san"] if top else None
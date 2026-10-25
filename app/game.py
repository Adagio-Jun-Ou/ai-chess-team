from typing import Dict, List, Optional

import chess


class ChessGame:
    """Thin wrapper around python-chess with SAN history and JSON-friendly state."""

    def __init__(self):
        self.board = chess.Board()
        self.san_history: List[str] = []

    def reset(self) -> None:
        self.board = chess.Board()
        self.san_history = []

    def legal_moves_detailed(self) -> List[Dict[str, str]]:
        return [
            {
                "san": self.board.san(move),
                "from": chess.square_name(move.from_square),
                "to": chess.square_name(move.to_square),
            }
            for move in self.board.legal_moves
        ]

    def legal_sans(self) -> List[str]:
        return [m["san"] for m in self.legal_moves_detailed()]

    def is_legal(self, move_text: str) -> bool:
        text = (move_text or "").strip()
        if not text:
            return False
        try:
            self.board.parse_san(text)
            return True
        except ValueError:
            pass
        try:
            move = chess.Move.from_uci(text.lower())
            return move in self.board.legal_moves
        except ValueError:
            return False

    def push(self, move_text: str) -> str:
        """Play a move given in SAN or UCI. Returns its SAN. Raises ValueError if illegal."""
        text = (move_text or "").strip()
        move = None
        try:
            move = self.board.parse_san(text)
        except ValueError:
            try:
                candidate = chess.Move.from_uci(text.lower())
                if candidate in self.board.legal_moves:
                    move = candidate
            except ValueError:
                pass
        if move is None:
            raise ValueError(f"Illegal move: {text}")
        san = self.board.san(move)
        self.board.push(move)
        self.san_history.append(san)
        return san

    def state(self) -> Dict:
        board = self.board
        status = "in_progress"
        winner = None
        if board.is_checkmate():
            status = "checkmate"
            winner = "white" if board.turn == chess.BLACK else "black"
        elif board.is_stalemate():
            status = "stalemate"
        elif board.is_insufficient_material():
            status = "insufficient_material"
        elif board.can_claim_threefold_repetition() or board.is_seventyfive_moves() or board.is_fivefold_repetition():
            status = "draw"
        elif board.is_game_over():
            status = "draw"

        last_from = last_to = None
        if board.move_stack:
            last = board.peek()
            last_from = chess.square_name(last.from_square)
            last_to = chess.square_name(last.to_square)

        return {
            "fen": board.fen(),
            "turn": "white" if board.turn == chess.WHITE else "black",
            "check": board.is_check(),
            "last_move": self.san_history[-1] if self.san_history else None,
            "last_from": last_from,
            "last_to": last_to,
            "history": list(self.san_history),
            "legal_moves": self.legal_moves_detailed(),
            "status": status,
            "winner": winner,
            "game_over": status != "in_progress",
        }

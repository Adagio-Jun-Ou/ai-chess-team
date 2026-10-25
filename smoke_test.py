"""Quick connectivity + logic check: run `.venv\\Scripts\\python smoke_test.py`."""

import asyncio

from app.agents import extract_move
from app.config import (
    AUTOPILOT_MODEL,
    OLLAMA_API_KEY,
    OLLAMA_BASE_URL,
    OPPONENT_A_MODEL,
    OPPONENT_B_MODEL,
    PARTNER_MODEL,
)
from app.game import ChessGame
from app.llm import LLMClient, LLMError


async def check_model(model: str) -> bool:
    client = LLMClient(OLLAMA_BASE_URL, OLLAMA_API_KEY, model, timeout=120)
    try:
        reply = await client.chat([{"role": "user", "content": "Reply with exactly: READY"}], temperature=0)
        print(f"  {model}: {reply[:60]!r}")
        return True
    except LLMError as exc:
        print(f"  {model}: FAILED - {exc}")
        return False
    finally:
        await client.aclose()


def check_chess_logic() -> bool:
    game = ChessGame()
    san = game.push("e4")
    assert san == "e4", san
    san = game.push("e7e5")  # UCI input
    assert san == "e5", san
    assert game.is_legal("Nf3")
    assert not game.is_legal("Qxf7")
    assert extract_move("I like this idea!\nMOVE: Nf3") == "Nf3"
    assert extract_move("SUGGEST: d4 is best.") == "d4"
    assert extract_move("no move here") is None
    try:
        game.push("e5")  # black pawn occupies e5 - illegal for White
        raise AssertionError("illegal move accepted")
    except ValueError:
        pass
    print("  chess logic + move parsing: OK")
    return True


async def main() -> None:
    print("Checking chess logic...")
    logic_ok = check_chess_logic()

    print("Checking models on Ollama Cloud...")
    models_ok = all(
        await asyncio.gather(
            check_model(PARTNER_MODEL),
            check_model(OPPONENT_A_MODEL),
            check_model(OPPONENT_B_MODEL),
            check_model(AUTOPILOT_MODEL),
        )
    )

    if logic_ok and models_ok:
        print("\nAll good. Start the app with:  .venv\\Scripts\\python run.py")
    else:
        print("\nSome checks failed - see output above.")
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

"""Human-in-the-loop reproduction loop.

Copy this file, edit the steps between the markers, and run it with
`run_interactive_command` so the user follows the prompts in the terminal
popup. It needs only the Python standard library and works on every OS.

Two helpers:
    step("<instruction>")            show an instruction, wait for Enter
    capture("VAR", "<question>")     show a question, keep the answer as VAR

At the end the captured values are printed as KEY=VALUE lines for the agent.
Capture observations; leave signing in, and anything secret, to a `step`.
"""
from __future__ import annotations

CAPTURED: dict[str, str] = {}


def step(instruction: str) -> None:
    print(f"\n>>> {instruction}")
    input("    [Enter when done] ")


def capture(name: str, question: str) -> str:
    print(f"\n>>> {question}")
    answer = input("    > ").strip()
    CAPTURED[name] = answer
    return answer


def main() -> None:
    # --- edit below ---------------------------------------------------------

    step("Open the app at http://localhost:3000 and sign in.")

    capture("ERRORED", "Click the 'Export' button. Did it throw an error? (y/n)")

    capture("ERROR_MSG", "Paste the error message (or 'none'):")

    # --- edit above ---------------------------------------------------------

    print("\n--- Captured ---")
    for name, value in CAPTURED.items():
        print(f"{name}={value}")


if __name__ == "__main__":
    main()

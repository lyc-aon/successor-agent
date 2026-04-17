"""Sandbox scenario definitions — tiered tests for model behavior
observation under the full Successor tool surface.

Tier 1: Single-capability probes (basic tool dispatch)
Tier 2: Multi-step with structured autonomy tools
Tier 3: Browser + vision compound verification
Tier 4: The game test primitive (snake + player)
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))

from e2e_chat_driver import Scenario  # noqa: E402


# ═══════════════════════════════════════════════════════════════════
# Tier 1 — Single-capability probes
# ═══════════════════════════════════════════════════════════════════

TIER_1 = [
    Scenario(
        name="hello-file",
        description="write a file with write_file, read it back with read_file",
        prompts=[
            (
                "Write a file called hello.txt containing exactly "
                "'Hello from Qwen 3.6' using the write_file tool. "
                "Do not use bash — use the native write_file tool."
            ),
            (
                "Now read hello.txt back using the read_file tool and "
                "confirm the contents are correct."
            ),
        ],
        assert_files={
            "hello.txt": "Hello from Qwen 3.6",
        },
        assert_min_text_in_final=["hello from qwen"],
        assert_each_settles=True,
    ),

    Scenario(
        name="bash-chain",
        description="create a directory, write a Python script, run it via bash",
        prompts=[
            (
                "Do these three things using bash:\n"
                "1. Create a directory called project/\n"
                "2. Write a Python script project/main.py that prints "
                "'calculation: 42'\n"
                "3. Run the script with python3 project/main.py\n"
                "Report the output."
            ),
        ],
        assert_files={
            "project/main.py": "42",
        },
        assert_min_text_in_final=["42"],
        assert_each_settles=True,
    ),

    Scenario(
        name="edit-precision",
        description="write JSON, use edit_file for a targeted change, read back",
        prompts=[
            (
                'Use write_file to create config.json with this content:\n'
                '{"name": "alpha", "version": 1, "debug": false}'
            ),
            (
                "Use read_file to read config.json, then use edit_file to "
                'change "alpha" to "beta". Do not rewrite the whole file — '
                "use edit_file with old_string and new_string."
            ),
            (
                "Read config.json one more time and confirm the change "
                "was applied correctly."
            ),
        ],
        assert_files={
            "config.json": '"beta"',
        },
        assert_min_text_in_final=["beta"],
        assert_each_settles=True,
    ),
]


# ═══════════════════════════════════════════════════════════════════
# Tier 2 — Multi-step with structured autonomy tools
# ═══════════════════════════════════════════════════════════════════

TIER_2 = [
    Scenario(
        name="task-ledger-discipline",
        description="build a CLI calculator with task and verify tracking",
        prompts=[
            (
                "Build a Python CLI calculator that:\n"
                "1. Accepts two numbers and an operator (+, -, *, /) "
                "from command line arguments\n"
                "2. Prints the result\n"
                "3. Exits with code 1 on invalid input "
                "(wrong arg count, bad operator, division by zero)\n"
                "\n"
                "Before you start coding, create a task ledger with one "
                "task per requirement. Update each task as you work on it. "
                "When you finish, use the verify tool to prove all three "
                "requirements work by running actual test commands."
            ),
        ],
        assert_files={
            "calculator.py": None,  # exists, content varies
        },
        assert_min_total_cards=3,  # at minimum: write + a few test runs
        assert_each_settles=True,
    ),

    Scenario(
        name="iterative-recovery",
        description="build a script for missing data, discover the gap, fix it",
        prompts=[
            (
                "Build a Python script called summarize.py that:\n"
                "1. Reads a CSV file called data.csv\n"
                "2. Sorts rows by the second column (numerically)\n"
                "3. Prints the sorted rows\n"
                "\n"
                "Here's the thing: there is no data.csv yet. I want you "
                "to attempt running the script first, discover it fails, "
                "then create appropriate test data (at least 5 rows with "
                "name,score columns), and run again to verify it works. "
                "Use the runbook tool to track your attempts."
            ),
        ],
        assert_files={
            "summarize.py": None,
            "data.csv": None,
        },
        assert_min_total_cards=3,  # write + fail + fix + succeed
        assert_each_settles=True,
    ),
]


# ═══════════════════════════════════════════════════════════════════
# Tier 3 — Browser + vision compound verification
# ═══════════════════════════════════════════════════════════════════

TIER_3 = [
    Scenario(
        name="counter-app",
        description="build an HTML counter, verify clicks via browser + vision",
        prompts=[
            (
                "Build a simple HTML page called counter.html with:\n"
                "- A counter display that starts at 0\n"
                "- An 'Increment' button that adds 1\n"
                "- A 'Decrement' button that subtracts 1\n"
                "- A 'Reset' button that sets it back to 0\n"
                "\n"
                "Use write_file to create it."
            ),
            (
                "Open counter.html in the browser. Click the Increment "
                "button three times. Then take a screenshot and use the "
                "vision tool to verify the counter shows 3. Report what "
                "you see."
            ),
        ],
        assert_files={
            "counter.html": "<button",
        },
        assert_each_settles=True,
    ),

    Scenario(
        name="form-validation",
        description="build a form with validation, test edge cases via browser",
        prompts=[
            (
                "Build an HTML page called form.html with a form containing:\n"
                "- Name field (required, minimum 3 characters)\n"
                "- Email field (must contain @)\n"
                "- A Submit button\n"
                "- A results div that shows 'Valid!' on success or "
                "'Invalid: [reasons]' on failure\n"
                "\n"
                "All validation should happen client-side in JavaScript. "
                "Use write_file to create it."
            ),
            (
                "Open form.html in the browser and test these three cases:\n"
                "1. Submit with both fields empty\n"
                "2. Submit with name='ab' and email='bad'\n"
                "3. Submit with name='Alice' and email='alice@test.com'\n"
                "\n"
                "For each case, type the values, click Submit, and report "
                "what the results div shows. Take a screenshot after each "
                "test and use vision to verify."
            ),
        ],
        assert_files={
            "form.html": "function",
        },
        assert_each_settles=True,
    ),
]


# ═══════════════════════════════════════════════════════════════════
# Tier 4 — The game test primitive
# ═══════════════════════════════════════════════════════════════════

TIER_4 = [
    Scenario(
        name="snake-game",
        description="build snake game + auto-player + verify via screenshots",
        prompts=[
            # Phase 1: Build the game
            (
                "Build a browser-playable snake game as a single "
                "index.html file. Requirements:\n"
                "- Canvas-based rendering (400x400, 20px grid cells)\n"
                "- Arrow key controls\n"
                "- Food spawns randomly on the grid\n"
                "- Snake grows when eating food\n"
                "- Game over on wall collision or self collision\n"
                "- Score display above the canvas\n"
                "- 'Game Over' overlay with final score when the game ends\n"
                "- Game loop at 10 FPS (100ms interval) for testability\n"
                "- A global `window.__snakeState` object that returns "
                "`{snake: [[x,y],...], food: [x,y], score: number, "
                "gameOver: boolean, direction: string}` so external "
                "scripts can inspect the game state\n"
                "\n"
                "Use write_file to create index.html."
            ),

            # Phase 2: Build the player
            (
                "Now build an automated player script called play-snake.js "
                "that uses Playwright to:\n"
                "1. Open index.html in a headless browser\n"
                "2. Read `window.__snakeState` each frame via page.evaluate\n"
                "3. Implement a simple greedy algorithm: always move toward "
                "the food while avoiding walls and the snake's own body\n"
                "4. Send arrow key presses based on the chosen direction\n"
                "5. Play until game over or 200 moves, whichever comes first\n"
                "6. Log each move (direction, score, snake length) to stdout\n"
                "7. Capture a screenshot of the final state as final-state.png\n"
                "8. Exit with code 0 if the game ended normally, code 1 if "
                "something broke\n"
                "\n"
                "The script should be runnable with: "
                "node play-snake.js\n"
                "\n"
                "Use write_file to create it."
            ),

            # Phase 3: Run and verify
            (
                "Run the player script with bash:\n"
                "  npx playwright install chromium 2>/dev/null; "
                "node play-snake.js\n"
                "\n"
                "Then:\n"
                "1. Check the exit code and review the move log output\n"
                "2. Open final-state.png with the vision tool and describe "
                "what you see — is it a snake game? Is there a score? "
                "Did the game reach a game-over state?\n"
                "3. Use the verify tool to record your findings with "
                "concrete observed evidence\n"
                "\n"
                "If anything is broken, fix it and re-run."
            ),

            # Phase 4: Adversarial test
            (
                "Now test an edge case: modify play-snake.js to force the "
                "snake to run directly into the top wall on the third move "
                "(send ArrowUp three times regardless of position). Save "
                "this as play-snake-crash.js.\n"
                "\n"
                "Run it, verify that game over triggers correctly, and "
                "capture a screenshot of the crash state. Use verify to "
                "record the adversarial test result."
            ),
        ],
        assert_files={
            "index.html": "window.__snakeState",
            "play-snake.js": "playwright",
        },
        assert_each_settles=True,
    ),
]


# ─── Registry ───

SANDBOX_SCENARIOS: dict[int, list[Scenario]] = {
    1: TIER_1,
    2: TIER_2,
    3: TIER_3,
    4: TIER_4,
}

# Development

- Auto-formatter: `uv run ruff format`
- Linter: `uv run ruff check`
- Test `uv run pytest -q`

## Git

Do not stage, unstage, or commit changes. Leave those actions to the user.

## Comments

Keep comments clean and concise.
Document API interfaces and expectations.
Minimal section markers are okay.

State invariants abstractly — just the rule as simple as possible.
Prefer referencing categories and high level concepts over implementation details.

Only add extra comments if the code is not self-explanatory.
DO NOT mention historical cruft or change logs

# Repository Guidelines

## Project Structure & Module Organization

`tg_archiver/` contains the async Python application: `__main__.py` provides the CLI, `collector.py` handles Telegram collection and downloads, `archive.py` saves observations, `db.py` defines persistence, `config.py` loads settings, and `web.py` serves HTML and attachments. The web UI has no separate frontend build. Tests live in `tests/`. Consult `REQUIREMENTS.md`, `DESIGN.md`, and `GLOSSARY.md` for scope, architecture, and terminology. Deployment files are `Dockerfile` and `compose.yaml`; configuration starts from `config.example.toml`.

## Build, Test, and Development Commands

Use Python 3.12+ and SQLite with FTS5 support.

- `python -m venv .venv` and `. .venv/bin/activate`: create and activate a local environment on Linux/macOS.
- `python -m pip install -e .`: install the package and declared dependencies for development.
- `tg-archiver login`: authorize a Telegram account after configuring credentials.
- `tg-archiver run`: start collection and the web UI at `http://127.0.0.1:8080` by default. `python -m tg_archiver` is an equivalent entry point.
- `python -m unittest discover -s tests -v`: run the test suite.
- `python -m compileall -q tg_archiver tests`: check Python compilation.
- `docker compose build`: build the container image.

## Coding Style & Naming Conventions

Follow existing Python style: four-space indentation, `snake_case` functions and variables, `PascalCase` classes, and `UPPER_CASE` constants. Use async APIs for collection, database access, and web handlers. Keep changes within the existing module responsibilities. No formatter or linter is configured; avoid unrelated formatting changes.

## Testing Guidelines

Tests use standard-library `unittest`, including `IsolatedAsyncioTestCase`. Name files `test_*.py` and methods `test_*`. Use synthetic Telegram objects, fake API boundaries, and temporary databases; routine tests must not access a real account. Install dependencies so integration tests run instead of skipping. Web tests need an ephemeral loopback socket. Cover affected persistence, recovery, search, or security behavior; no numeric coverage threshold is configured.

## Commit & Pull Request Guidelines

Existing commits use brief, plain descriptions such as `add gitignore`; no formal commit convention is established. Use concise, action-oriented subjects. Pull requests should explain the behavior change, reference relevant issues or design requirements, and report validation and limitations. Include screenshots for visible UI changes.

## Security & Configuration

Keep credentials, Telegram sessions, and archive data out of commits and logs. Copy the example to `config.toml` and supply `TG_API_ID` and `TG_API_HASH`. The web UI has no built-in authentication; retain loopback defaults. Preserve captured revisions and attachments, and never modify source Telegram messages. Stop the application before running `login`, `chats`, or `retry` against its data directory.

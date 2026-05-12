# Contributing

Thanks for your interest in contributing! This project is part of the `roguERGlike` ecosystem — a roguelike deckbuilder driven by smart trainer data.

## Before you start

1. Open an issue first for anything non-trivial. The project has a clear architecture (see [event schema](docs/event-schema.md)) and contributions that don't fit will be hard to merge.
2. By submitting a PR, you agree to the terms in [CLA.md](CLA.md). This lets us keep the option to relicense or use this code in the closed-source game.

## Development

```bash
pip install -e ".[dev]"
pre-commit install
pytest
```

## Workflow

- Fork → branch off `develop` → PR back to `develop`
- Conventional commits: `feat:`, `fix:`, `chore:`, `docs:`, `test:`, `refactor:`
- One logical change per PR
- Tests required for new functionality
- CI must pass

## Areas where help is especially welcome

- Testing against trainer brands I don't own (Tacx, Elite, Saris, JetBlack, etc.)
- Heart rate strap compatibility reports
- FIT/TCX export edge cases
- Replay file format documentation

## Areas that are out of scope

- Game-specific features (those live in a separate private repo)
- BLE protocols other than FTMS / HRS / standard cycling services (for now)

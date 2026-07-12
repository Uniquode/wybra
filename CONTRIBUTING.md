# Contributing to wybra

## Development Setup

### Requirements

- Python 3.14+
- uv package manager

### Installation

```bash
uv sync
```

## Development Workflow

### Running Tests

```bash
uv run pytest -q
```

### Type Checking

```bash
uv run ty check src/
```

### Linting

```bash
uv run ruff check src tests
```

### Formatting

```bash
uv run ruff format src tests
```

### Building

```bash
uv build
```

## Architecture

### Key Technologies

- **FastAPI / Starlette** - async web application foundation
- **SQLAlchemy / Alembic** - async persistence and migrations
- **FastAPI Users** - reusable identity and authentication primitives
- **Jinja2** - server-rendered templates
- **Click** - package-owned project commands
- **envex** - environment-backed configuration support

### Package Areas

- `wybra.core`: composition, settings loading, diagnostics, and conventions.
- `wybra.views`: reusable view helpers.
- `wybra.assets`: static asset settings, serving, collection, and validation.
- `wybra.template`: template settings, rendering, context, and validation.
- `wybra.forms`: form settings, CSRF protection, parsing, and validation.
- `wybra.security`: security headers, CORS policy, and validation.
- `wybra.errors`: exception handler registration, classification, and
  fallbacks.
- `wybra.api`: API request classification, response formatting, and validation.
- `wybra.db`: database URL helpers, async database helpers, SQLAlchemy metadata
  conventions, and Alembic command support.
- `wybra.tools`: project command adapters.
- `wybra.auth`: reusable local identity, authentication, templates, routes, and
  operator tooling.

## Project Commands

```bash
uv run wybra-runserver
uv run wybra-migrate --help
uv run wybra-routes --help
uv run wybra-validate --help
uv run wybra-authmgr --help
```

## Code Style

- Use type hints for public function signatures and non-obvious internal
  boundaries.
- Keep changes small and requirement-driven.
- Prefer existing package conventions over new framework structure.
- Use Ruff formatting and linting before submitting changes.

## Pull Request Process

1. Create a feature branch from `main`.
2. Make the smallest coherent change with tests.
3. Ensure tests, linting, formatting, type checking, and build checks pass.
4. Update documentation as needed.
5. Submit a pull request with a clear description of the change and its impact.

## License

MIT License - See LICENSE.md for details.

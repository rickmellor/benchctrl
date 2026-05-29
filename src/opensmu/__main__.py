"""Make `python -m opensmu` invoke the CLI."""

from opensmu.cli import main

if __name__ == "__main__":
    raise SystemExit(main())

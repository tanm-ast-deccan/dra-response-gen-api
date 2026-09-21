"""Enable `python -m dra_harness`."""

try:
    from .cli import main
except ImportError:  # run as a script from inside the package dir
    from cli import main

if __name__ == "__main__":
    main()

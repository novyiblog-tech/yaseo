"""Точка входа пакета: python -m yaseo <команда>."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())

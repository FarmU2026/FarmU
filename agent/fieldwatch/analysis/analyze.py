"""Canonical import path `fieldwatch.analysis.analyze:analyze` (interfaces.CANONICAL).

The real code is in `analyzer.py`. A submodule named `analyze` would normally shadow the package attribute
`fieldwatch.analysis.analyze` (the function) as soon as it is imported, so this module replaces itself in
`sys.modules` with the function. Then the import system binds the package attribute to the function in every
import order, `from fieldwatch.analysis.analyze import analyze` still works, and so does
`importlib.import_module("fieldwatch.analysis.analyze").analyze`.
"""
import sys

from .analyzer import analyze

analyze.analyze = analyze  # type: ignore[attr-defined]  # the "module" (the function) exposes `analyze`
sys.modules[__name__] = analyze  # type: ignore[assignment]

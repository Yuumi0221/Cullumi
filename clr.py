"""Compatibility loader required by pythonnet/pywebview on Windows."""

from pythonnet import load

load()
del load

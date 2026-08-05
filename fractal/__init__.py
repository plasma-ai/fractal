"""The ``fractal`` package.

Hierarchical agent loops with recursive self-organization.
"""

from . import cli, constants, core, exceptions, impl, typing, util
from .cli import *
from .constants import *
from .core import *

__version__ = '1.2.0.dev0'


def __getattr__(name):
    if name == 'tui':
        import importlib

        return importlib.import_module('fractal.tui')
    raise AttributeError(name)

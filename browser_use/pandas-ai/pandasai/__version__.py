import importlib.metadata

try:
    __version__ = importlib.metadata.version(__package__ or __name__)
except importlib.metadata.PackageNotFoundError:
    # WebRetriever vendors the MIT core as source instead of installing the
    # upstream distribution in a second Python environment.
    __version__ = "3.0.0"

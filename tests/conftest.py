import importlib.util, os, sys
sys.path.insert(0, os.path.expanduser("~/.hermes/hermes-agent"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "plugin"))

# The plugin lives at plugin/dakera/__init__.py; tests import it as `dakera_provider`.
if "dakera_provider" not in sys.modules:
    _init = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "plugin", "dakera", "__init__.py"))
    _spec = importlib.util.spec_from_file_location("dakera_provider", _init)
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["dakera_provider"] = _mod
    _spec.loader.exec_module(_mod)

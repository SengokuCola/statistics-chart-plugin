from importlib import import_module
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
import sys


@pytest.fixture(scope="session")
def plugin_module():
    spec = spec_from_file_location("statistics_chart_release", Path(__file__).parents[1] / "__init__.py")
    package = module_from_spec(spec)
    sys.modules[spec.name] = package
    spec.loader.exec_module(package)
    return import_module("statistics_chart_release.plugin")

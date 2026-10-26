"""测试时把插件目录当成一个包导入（与 MaiBot 加载插件的方式相同）。"""

from __future__ import annotations

from pathlib import Path

import importlib.util
import sys

PLUGIN_DIR = Path(__file__).resolve().parent.parent
PACKAGE = "x_browse_under_test"

if PACKAGE not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        PACKAGE, PLUGIN_DIR / "plugin.py", submodule_search_locations=[str(PLUGIN_DIR)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)

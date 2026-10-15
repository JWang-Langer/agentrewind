# Copyright 2026 WZMLab
# SPDX-License-Identifier: Apache-2.0
"""让 tests/ 与 src/、examples/ 都可导入，便于 `python -m unittest discover -s tests -t .`。"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _path in (
    os.path.join(ROOT, "src"),
    ROOT,
    os.path.join(ROOT, "examples"),
    os.path.join(ROOT, "tests"),
):
    if _path not in sys.path:
        sys.path.insert(0, _path)

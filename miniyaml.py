"""miniyaml - import shim.

The implementation moved into the installable package
(:mod:`openapi_contract_lint.miniyaml`) so that the wheel can ship it. This file
keeps `import miniyaml` working for anything run from a checkout, including the
test suite, by replacing itself in ``sys.modules`` with the real module: both
names end up referring to one module object, never to two copies of the code.
"""

import sys

from openapi_contract_lint import miniyaml as _implementation

sys.modules[__name__] = _implementation

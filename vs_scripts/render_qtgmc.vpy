"""VapourSynth entry point for the QTGMC y4m render stream."""

from __future__ import annotations

from pathlib import Path
import sys

import vapoursynth as vs
from vssource import BestSource


_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from vs_scripts.qtgmc import (  # noqa: E402
    QTGMCConfig,
    build_qtgmc_clip,
    source_field_order,
    validate_qtgmc_output,
)


def _script_arguments() -> dict[str, str]:
    """Read vspipe --arg values from injected globals or argv."""

    names = {"input", "preset", "field_order", "fps_mode"}
    values = {
        name: str(globals()[name])
        for name in names
        if name in globals() and globals()[name] is not None
    }

    argv = iter(sys.argv[1:])
    for token in argv:
        if token == "--arg":
            token = next(argv, "")
        elif token.startswith("--arg="):
            token = token[6:]
        if "=" not in token:
            continue
        name, value = token.split("=", 1)
        if name in names:
            values[name] = value
    return values


def _required(values: dict[str, str], name: str) -> str:
    value = values.get(name)
    if not value:
        raise ValueError(f"vspipe --arg {name}=... is required")
    return value


arguments = _script_arguments()
input_path = _required(arguments, "input")
config = QTGMCConfig(
    _required(arguments, "preset"),
    _required(arguments, "field_order"),
    _required(arguments, "fps_mode"),
)

source = BestSource().source(input_path)
actual_field_order = source_field_order(source)
if actual_field_order != config.field_order:
    raise ValueError(
        "QTGMC field-order mismatch: "
        f"source={actual_field_order}, cli={config.field_order}"
    )

output = build_qtgmc_clip(source, config)
validate_qtgmc_output(source, output, config)

# vspipe owns the y4m container and writes this output node to stdout.
vs.core.set_output(output)

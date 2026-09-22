import argparse
import importlib
import json
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


QTGMC_NAMESPACE = "vsdeinterlace"
QTGMC_FUNCTION = "QTempGaussMC"
BESTSOURCE_NAMESPACE = "vssource"
BESTSOURCE_FUNCTION = "BestSource"
RECOVERY_COMMAND = (
    "powershell -NoProfile -ExecutionPolicy Bypass -File "
    "scripts/install_vapoursynth.ps1 -Apply"
)


def load_vapoursynth():
    return importlib.import_module("vapoursynth")


def load_qtempgaussmc():
    module = importlib.import_module(QTGMC_NAMESPACE)
    return getattr(module, QTGMC_FUNCTION)


def _plugin_version(plugin):
    version = getattr(plugin, "version", None)
    return str(version) if version is not None else None


def collect_plugin_inventory(vs_module):
    inventory = {}
    plugins = getattr(vs_module.core, "plugins")()

    for plugin in plugins:
        namespace = str(getattr(plugin, "namespace", ""))
        functions = getattr(plugin, "functions")()
        function_names = sorted(
            str(getattr(function, "name", function)) for function in functions
        )
        inventory[namespace] = {
            "id": getattr(plugin, "id", None),
            "name": str(getattr(plugin, "name", "")),
            "version": _plugin_version(plugin),
            "functions": function_names,
        }

    return dict(sorted(inventory.items()))


def _vapoursynth_version(vs_module):
    version = getattr(vs_module, "__version__", None)
    if version is not None:
        return str(version)

    core = getattr(vs_module, "core", None)
    core_text = str(core) if core is not None else ""
    match = re.search(r"core_version=([^,>]+)", core_text)
    return match.group(1) if match else "unknown"


def _tool_path(name):
    path = shutil.which(name)
    return str(path) if path else None


def _synthetic_interlaced_clip(vs_module):
    clip = vs_module.core.std.BlankClip(
        width=64,
        height=64,
        format=vs_module.YUV420P8,
        length=2,
        fpsnum=30000,
        fpsden=1001,
        color=[16, 128, 128],
    )
    return vs_module.core.std.SetFrameProps(clip, _FieldBased=2)


def request_first_frame(vs_module, qtempgaussmc):
    clip = _synthetic_interlaced_clip(vs_module)
    processor = qtempgaussmc()
    if hasattr(processor, "deinterlace"):
        output = processor.deinterlace(clip, tff=True)
    else:
        output = processor(clip)
    output.get_frame(0)


def open_short_fixture_bestsource(ffmpeg_path):
    with tempfile.TemporaryDirectory(prefix="qtgmc-bestsource-") as temp_dir:
        fixture_path = Path(temp_dir) / "bestsource-preflight.mkv"
        ffmpeg_args = [
            ffmpeg_path,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=64x64:rate=30:duration=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=1",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-fps_mode",
            "cfr",
            "-shortest",
            str(fixture_path),
        ]
        process = subprocess.run(
            ffmpeg_args,
            capture_output=True,
            check=False,
        )
        if process.returncode != 0:
            stderr = process.stderr.decode("utf-8", "replace").strip()
            detail = stderr or "no ffmpeg stderr output"
            raise RuntimeError(
                "ffmpeg fixture generation failed (exit code {0}): {1}".format(
                    process.returncode, detail
                )
            )

        vssource = importlib.import_module(BESTSOURCE_NAMESPACE)
        bestsource = getattr(vssource, BESTSOURCE_FUNCTION)(
            cachemode=0,
            showprogress=False,
        )
        clip = bestsource.source(str(fixture_path))
        clip.get_frame(0)
        return {
            "best_source_opened": True,
            "best_source_frame_count": int(clip.num_frames),
        }


def _native_dependency_hint(error_text):
    match = re.search(r"name ([A-Za-z_][A-Za-z0-9_]*) exists", error_text)
    if match:
        return {"namespace": match.group(1), "function": None}

    match = re.search(
        r"(?:missing native plugin|unavailable)[: ]+"
        r"([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)",
        error_text,
        re.IGNORECASE,
    )
    if match:
        return {"namespace": match.group(1), "function": match.group(2)}

    return None


def _base_report():
    return {
        "status": "missing_dependency",
        "vapoursynth_version": None,
        "python_version": platform.python_version(),
        "vspipe_path": _tool_path("vspipe"),
        "ffmpeg_path": _tool_path("ffmpeg"),
        "plugin_inventory": {},
        "first_frame_requested": False,
        "best_source_opened": False,
        "best_source_frame_count": None,
        "recovery_command": RECOVERY_COMMAND,
    }


def _failure_report(
    report,
    error,
    stage,
    *,
    namespace=QTGMC_NAMESPACE,
    function=QTGMC_FUNCTION,
):
    error_text = str(error)
    report.update(
        {
            "status": "missing_dependency",
            "missing_dependency": {
                "namespace": namespace,
                "function": function,
            },
            "failure_stage": stage,
            "error": {
                "type": type(error).__name__,
                "message": error_text,
            },
        }
    )
    native_hint = _native_dependency_hint(error_text)
    if native_hint is not None:
        report["missing_native_dependency"] = native_hint
    return report


def run_preflight():
    report = _base_report()

    try:
        vs_module = load_vapoursynth()
    except Exception as error:
        return _failure_report(report, error, "vapoursynth_import")

    report["vapoursynth_version"] = _vapoursynth_version(vs_module)
    try:
        report["plugin_inventory"] = collect_plugin_inventory(vs_module)
    except Exception as error:
        report["plugin_inventory_error"] = {
            "type": type(error).__name__,
            "message": str(error),
        }

    try:
        qtempgaussmc = load_qtempgaussmc()
    except Exception as error:
        return _failure_report(report, error, "qtempgaussmc_import")

    try:
        request_first_frame(vs_module, qtempgaussmc)
    except Exception as error:
        return _failure_report(report, error, "first_frame_request")

    report["status"] = "ok"
    report["first_frame_requested"] = True
    if not report["ffmpeg_path"]:
        return _failure_report(
            report,
            FileNotFoundError("ffmpeg was not found on PATH"),
            "bestsource_fixture_generation",
            namespace="ffmpeg",
            function=None,
        )

    try:
        report.update(open_short_fixture_bestsource(report["ffmpeg_path"]))
    except Exception as error:
        return _failure_report(
            report,
            error,
            "bestsource_preflight",
            namespace=BESTSOURCE_NAMESPACE,
            function=BESTSOURCE_FUNCTION,
        )

    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check the Windows QTGMC runtime.")
    parser.add_argument("--json", action="store_true", help="write the report as JSON")
    args = parser.parse_args(argv)
    report = run_preflight()

    if args.json:
        json.dump(report, sys.stdout, ensure_ascii=False, sort_keys=True)
        sys.stdout.write("\n")
    else:
        print("status: {status}".format(**report))
        print("vapoursynth_version: {vapoursynth_version}".format(**report))
        print("python_version: {python_version}".format(**report))
        print("vspipe_path: {vspipe_path}".format(**report))
        print("ffmpeg_path: {ffmpeg_path}".format(**report))
        print("plugin_inventory: {plugin_inventory}".format(**report))
        print("best_source_opened: {best_source_opened}".format(**report))
        print("best_source_frame_count: {best_source_frame_count}".format(**report))
        if report["status"] != "ok":
            print("recovery_command: {recovery_command}".format(**report))

    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

import json
import sys

report = {"python": sys.version.split()[0]}

try:
    import vapoursynth as vs
    report["vapoursynth"] = vs.__version__
except Exception as e:
    report["vapoursynth_error"] = str(e)
    print(json.dumps(report, ensure_ascii=False))
    sys.exit(0)

def try_namespace(name):
    try:
        mod = __import__(name)
        report[name + "_import"] = "ok"
        return mod
    except Exception as e:
        report[name + "_import_error"] = str(e)
        return None

de = try_namespace("vsdeinterlace")
src = try_namespace("vssource")
hvs = try_namespace("havsfunc")

if de is not None:
    report["qtmpgaussmc_present"] = hasattr(de, "QTempGaussMC")

clip_ok = False
try:
    core = vs.core
    clip = core.std.BlankClip(width=32, height=32, color=[0, 0, 0], length=2, fpsnum=30000, fpsden=1001, format=vs.YUV420P8, colorspace=vs.BT601)
    frame = clip.get_frame(0)
    clip_ok = frame is not None
    report["synthetic_first_frame"] = "ok"
except Exception as e:
    report["synthetic_first_frame_error"] = str(e)

print(json.dumps(report, ensure_ascii=False))

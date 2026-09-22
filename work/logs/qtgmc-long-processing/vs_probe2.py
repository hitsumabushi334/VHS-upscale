import json
import sys

report = {"python": sys.version.split()[0]}

try:
    import vapoursynth as vs
    report["vapoursynth"] = list(vs.__version__)
except Exception as e:
    report["vapoursynth_error"] = str(e)
    print(json.dumps(report, ensure_ascii=False))
    sys.exit(0)

try:
    import vsdeinterlace
    report["vsdeinterlace"] = getattr(vsdeinterlace, "__version__", None)
except Exception as e:
    report["vsdeinterlace_error"] = str(e)

try:
    core = vs.core
    clip = core.std.BlankClip(width=32, height=32, color=[0, 0, 0], length=2, fpsnum=30000, fpsden=1001, format=vs.YUV420P8, colorspace=vs.Colorspace.BT601)
    f0 = clip.get_frame(0)
    report["synthetic_first_frame"] = "ok"
except Exception as e:
    report["synthetic_first_frame_error"] = str(e)

try:
    from vsdeinterlace import QTempGaussMC
    try:
        q = QTempGaussMC()
        out = q.deinterlace(clip, tff=True)
        fo = out.get_frame(0)
        report["qtgmc_first_frame"] = "ok"
    except Exception as e:
        report["qtgmc_first_frame_error"] = str(e)
except Exception as e:
    report["qtgmc_import_error"] = str(e)

try:
    import vssource
    from vssource import BestSource
    report["bestsource_present"] = True
except Exception as e:
    report["bestsource_error"] = str(e)

print(json.dumps(report, ensure_ascii=False))

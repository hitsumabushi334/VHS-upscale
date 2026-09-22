import json
import sys

report = {"python": sys.version.split()[0]}

try:
    import vapoursynth as vs
    report["vapoursynth"] = list(vs.__version__)
    core = vs.core
    clip = core.std.BlankClip(
        width=32, height=32, color=[0, 0, 0], length=4,
        fpsnum=30000, fpsden=1001, format=vs.YUV420P8,
        colorspace=vs.MATRIX_BT470_BG, colorprimaries=vs.PRIMARIES_BT470_BG,
        transfercharacteristics=vs.TRANSFER_BT601, sample_range=vs.RANGE_LIMITED,
    )
    f0 = clip.get_frame(0)
    report["synthetic_first_frame"] = "ok"
    report["frame_size"] = (f0.width, f0.height)
except Exception as e:
    report["synthetic_first_frame_error"] = str(e)
    clip = None

if clip is not None:
    try:
        from vsdeinterlace import QTempGaussMC
        q = QTempGaussMC()
        out = q.deinterlace(clip, tff=True)
        fo = out.get_frame(0)
        report["qtgmc_first_frame"] = "ok"
    except Exception as e:
        report["qtgmc_first_frame_error"] = str(e)

try:
    import vssource
    from vssource import BestSource
    report["bestsource_present"] = True
except Exception as e:
    report["bestsource_error"] = str(e)

print(json.dumps(report, ensure_ascii=False))

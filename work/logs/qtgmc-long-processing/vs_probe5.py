import json
import sys

report = {"python": sys.version.split()[0]}
clip = None

import vapoursynth as vs
report["vapoursynth"] = list(vs.__version__)
core = vs.core
for kwargs in (
    dict(width=32, height=32, length=4, fpsnum=30000, fpsden=1001, format=vs.YUV420P8),
    dict(width=32, height=32, length=4, fpsnum=30000, fpsden=1001, format=vs.YUV420P8, color=[0, 0, 0]),
):
    try:
        clip = core.std.BlankClip(**kwargs)
        report["blankclip_ok_args"] = sorted(kwargs)
        break
    except Exception as e:
        report["blankclip_error_" + str(sorted(kwargs))] = str(e)

if clip is not None:
    f0 = clip.get_frame(0)
    report["synthetic_first_frame"] = "ok"
    report["frame_props"] = {k: f0.props[k] for k in sorted(f0.props.keys()) if isinstance(f0.props[k], (int, str, tuple, list))}

if clip is not None:
    try:
        from vsdeinterlace import QTempGaussMC
        q = QTempGaussMC()
        out = q.deinterlace(clip, tff=True)
        fo = out.get_frame(0)
        report["qtgmc_first_frame"] = "ok"
    except Exception as e:
        report["qtgmc_first_frame_error"] = str(e)

print(json.dumps(report, ensure_ascii=False))

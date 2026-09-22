import importlib
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import uuid

import pytest


def _checker():
    return importlib.import_module("scripts.check_vapoursynth")


@pytest.mark.vapoursynth
def test_tc01_vapoursynth_preflight_requests_first_frame(capsys):
    checker = _checker()

    exit_code = checker.main(["--json"])
    report = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert report["status"] == "ok"
    assert report["vapoursynth_version"]
    assert report["python_version"]
    assert report["vspipe_path"]
    assert report["ffmpeg_path"]
    assert isinstance(report["plugin_inventory"], dict)
    assert report["first_frame_requested"] is True


@pytest.mark.parametrize("failure_stage", ["import", "frame"])
def test_tc01_failure_path_reporting(monkeypatch, capsys, failure_stage):
    checker = _checker()
    expected_inventory = {
        "std": {"functions": ["BlankClip", "SetFrameProps"]},
        "vsdeinterlace": {"functions": []},
    }

    class FakeVapourSynth:
        __version__ = "79"

    monkeypatch.setattr(checker, "load_vapoursynth", lambda: FakeVapourSynth)
    monkeypatch.setattr(
        checker,
        "collect_plugin_inventory",
        lambda _vs_module: expected_inventory,
    )

    if failure_stage == "import":
        def fail_import():
            raise ImportError("vsdeinterlace.QTempGaussMC is unavailable")

        monkeypatch.setattr(checker, "load_qtempgaussmc", fail_import)
    else:
        monkeypatch.setattr(
            checker,
            "load_qtempgaussmc",
            lambda: object(),
        )

        def fail_frame(_vs_module, _qtempgaussmc):
            raise RuntimeError("missing native plugin: std.SetFrameProps")

        monkeypatch.setattr(checker, "request_first_frame", fail_frame)

    exit_code = checker.main(["--json"])
    report = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert report["status"] == "missing_dependency"
    assert report["missing_dependency"] == {
        "namespace": "vsdeinterlace",
        "function": "QTempGaussMC",
    }
    assert report["plugin_inventory"] == expected_inventory
    assert "install_vapoursynth.ps1 -Apply" in report["recovery_command"]


def test_bestsource_failure_path_reporting(monkeypatch, capsys):
    checker = _checker()
    expected_inventory = {
        "std": {"functions": ["BlankClip", "SetFrameProps"]},
        "vsdeinterlace": {"functions": []},
    }

    class FakeVapourSynth:
        __version__ = "79"

    monkeypatch.setattr(checker, "load_vapoursynth", lambda: FakeVapourSynth)
    monkeypatch.setattr(
        checker,
        "collect_plugin_inventory",
        lambda _vs_module: expected_inventory,
    )
    monkeypatch.setattr(checker, "load_qtempgaussmc", lambda: object())
    monkeypatch.setattr(checker, "request_first_frame", lambda _vs_module, _qtempgaussmc: None)

    def fail_bestsource(*_args):
        raise RuntimeError("vssource.BestSource fixture frame request failed")

    monkeypatch.setattr(checker, "open_short_fixture_bestsource", fail_bestsource)

    exit_code = checker.main(["--json"])
    report = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert report["status"] == "missing_dependency"
    assert report["failure_stage"] == "bestsource_preflight"
    assert report["missing_dependency"] == {
        "namespace": "vssource",
        "function": "BestSource",
    }
    assert report["plugin_inventory"] == expected_inventory
    assert "install_vapoursynth.ps1 -Apply" in report["recovery_command"]


def _qtgmc():
    return importlib.import_module("vs_scripts.qtgmc")


def _task6_temp_dir() -> Path:
    root = Path.cwd().parents[1] / "work" / "logs" / "qtgmc-long-processing"
    return root / f"task6-test-{uuid.uuid4().hex}"


class _FakeStderrPipe:
    def __init__(self):
        self._condition = threading.Condition()
        self._buffer = bytearray()
        self._closed = False
        self._aborted = False
        self.total_written = 0
        self.total_read = 0

    def write(self, data):
        offset = 0
        while offset < len(data):
            with self._condition:
                while len(self._buffer) >= 4096 and not self._aborted:
                    self._condition.wait()
                if self._aborted:
                    self.total_written += len(data) - offset
                    return len(data)
                chunk = min(4096 - len(self._buffer), len(data) - offset)
                self._buffer.extend(data[offset : offset + chunk])
                self.total_written += chunk
                offset += chunk
                self._condition.notify_all()
        return len(data)

    def read(self, _size=-1):
        chunks = []
        while True:
            with self._condition:
                while not self._buffer and not self._closed and not self._aborted:
                    self._condition.wait()
                if self._buffer:
                    chunk = bytes(self._buffer)
                    self._buffer.clear()
                    self.total_read += len(chunk)
                    self._condition.notify_all()
                    chunks.append(chunk)
                    continue
                return b"".join(chunks)

    def finish(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def abort(self):
        with self._condition:
            self._aborted = True
            self._buffer.clear()
            self._condition.notify_all()

    def close(self):
        return None


class _FakeStdoutPipe:
    def __init__(self):
        self._condition = threading.Condition()
        self._buffer = bytearray()
        self._closed = False
        self._aborted = False

    def write(self, data):
        with self._condition:
            self._buffer.extend(data)
            self._condition.notify_all()

    def read(self):
        with self._condition:
            while not self._closed and not self._aborted:
                self._condition.wait()
            return bytes(self._buffer)

    def finish(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def abort(self):
        with self._condition:
            self._aborted = True
            self._condition.notify_all()

    def close(self):
        return None


class _FakeVspipeProcess:
    def __init__(self):
        self.stdout = _FakeStdoutPipe()
        self.stderr = _FakeStderrPipe()
        self.returncode = None
        self._finished = threading.Event()
        self._producer = threading.Thread(target=self._produce, daemon=True)
        self._producer.start()

    def _produce(self):
        self.stderr.write(b"v" * (1 << 20))
        self.stdout.write(b"YUV4MPEG2\n")
        self.stderr.finish()
        self.stdout.finish()
        self.returncode = 0
        self._finished.set()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if not self._finished.wait(timeout):
            raise TimeoutError("fake vspipe did not finish")
        return self.returncode

    def terminate(self):
        self.stderr.abort()
        self.stdout.abort()

    def kill(self):
        self.terminate()


class _FakeFfmpegProcess:
    def __init__(self, stdin):
        self._stdin = stdin
        self.returncode = None

    def communicate(self):
        self._stdin.read()
        self.returncode = 0
        return b"", b""

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self._stdin.abort()

    def kill(self):
        self.terminate()


class _FakePipeline:
    def __init__(self):
        self.vspipe = None
        self.ffmpeg = None

    def popen(self, command, **kwargs):
        if command == ["vspipe"]:
            self.vspipe = _FakeVspipeProcess()
            return self.vspipe
        assert command == ["ffmpeg"]
        self.ffmpeg = _FakeFfmpegProcess(kwargs["stdin"])
        return self.ffmpeg

    def abort(self):
        if self.ffmpeg is not None:
            self.ffmpeg.terminate()
        if self.vspipe is not None:
            self.vspipe.terminate()


def test_tc06_vspipe_stderr_is_drained_while_ffmpeg_runs(monkeypatch):
    qtgmc = _qtgmc()
    pipeline = _FakePipeline()
    monkeypatch.setattr(qtgmc.subprocess, "Popen", pipeline.popen)
    errors = []

    def run_pipeline():
        try:
            qtgmc._run_vspipe_to_ffmpeg(["vspipe"], ["ffmpeg"])
        except BaseException as exc:  # pragma: no cover - failure diagnostics
            errors.append(exc)

    worker = threading.Thread(target=run_pipeline, daemon=True)
    worker.start()
    worker.join(timeout=10)
    if worker.is_alive():
        pipeline.abort()
        worker.join(timeout=5)
        pytest.fail("QTGMC pipeline did not finish while vspipe stderr was full")

    assert errors == []
    assert pipeline.vspipe.stderr.total_written >= 1 << 20
    assert pipeline.vspipe.stderr.total_read == pipeline.vspipe.stderr.total_written


def test_tc07_stale_mux_partial_is_recorded_and_removed_before_audio(
    monkeypatch,
):
    qtgmc = _qtgmc()
    test_root = _task6_temp_dir()
    input_file = test_root.with_name(test_root.name + ".source.mkv")
    output_file = test_root.with_name(test_root.name + ".mkv")
    partial_file = test_root.with_name(test_root.name + ".partial.mkv")
    mux_partial_file = test_root.with_name(test_root.name + ".partial.mux.mkv")
    report_path = test_root.with_name(test_root.name + ".report.json")
    input_file.write_bytes(b"source")
    mux_partial_file.write_bytes(b"stale mux partial")
    report_path.write_text("{}\n", encoding="utf-8")
    manifest = {}
    source_probe = {
        "streams": [
            _tc08_video_stream(
                codec_name="mpeg2video",
                pix_fmt="yuv422p10le",
                field_order="tt",
                frame_rate="30000/1001",
                frames=300,
            )
        ]
    }

    monkeypatch.setattr(
        qtgmc,
        "build_vspipe_command",
        lambda *_args, **_kwargs: ["vspipe"],
    )
    monkeypatch.setattr(
        qtgmc,
        "_ffmpeg_video_command",
        lambda *_args, **_kwargs: ["ffmpeg", "video"],
    )
    monkeypatch.setattr(
        qtgmc,
        "_ffmpeg_audio_command",
        lambda *_args, **_kwargs: ["ffmpeg", "audio"],
    )
    monkeypatch.setattr(
        qtgmc,
        "_run_vspipe_to_ffmpeg",
        lambda *_args, **_kwargs: partial_file.write_bytes(b"video partial"),
    )
    monkeypatch.setattr(qtgmc, "validate_encoded_contract", lambda *_args: None)

    def fake_audio_run(_command, **_kwargs):
        assert not mux_partial_file.exists()
        report_snapshot = json.loads(report_path.read_text(encoding="utf-8"))
        assert report_snapshot["status"] == "interrupted"
        assert report_snapshot["partial_artifact"] == str(mux_partial_file)
        assert manifest["status"] == "interrupted"
        mux_partial_file.write_bytes(b"new mux partial")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(qtgmc.subprocess, "run", fake_audio_run)
    try:
        report = qtgmc.run_qtgmc_render(
            input_file,
            output_file,
            qtgmc.QTGMCConfig("raw", "tff", "double"),
            ffmpeg_path="ffmpeg",
            report_path=report_path,
            manifest=manifest,
            probe=lambda _path: source_probe,
        )

        assert report["status"] == "complete"
        assert output_file.exists()
        assert not mux_partial_file.exists()
    finally:
        for path in (
            input_file,
            output_file,
            partial_file,
            mux_partial_file,
            report_path,
        ):
            path.unlink(missing_ok=True)


class _FakeFrame:
    def __init__(self, props):
        self.props = props


class _FakeClip:
    def __init__(
        self,
        *,
        num_frames,
        fps_num,
        fps_den,
        width=720,
        height=480,
        field_based=0,
        sar=(10, 11),
        indices=None,
    ):
        self.num_frames = num_frames
        self.fps_num = fps_num
        self.fps_den = fps_den
        self.width = width
        self.height = height
        self.indices = tuple(range(num_frames) if indices is None else indices)
        self._frame_props = {
            "_FieldBased": field_based,
            "_SARNum": sar[0],
            "_SARDen": sar[1],
        }

    def get_frame(self, index):
        assert 0 <= index < self.num_frames
        return _FakeFrame(self._frame_props)


def test_tc04_qtgmc_double_output_has_twice_frames_and_rate(monkeypatch):
    qtgmc = _qtgmc()
    source = _FakeClip(
        num_frames=300,
        fps_num=30000,
        fps_den=1001,
        field_based=2,
    )
    output = _FakeClip(
        num_frames=600,
        fps_num=60000,
        fps_den=1001,
        field_based=0,
    )
    config = qtgmc.QTGMCConfig("raw", "tff", "double")

    qtgmc.validate_qtgmc_output(source, output, config)


def test_tc05_half_mode_selects_even_bobbed_source_indices(monkeypatch):
    qtgmc = _qtgmc()
    source = _FakeClip(
        num_frames=300,
        fps_num=30000,
        fps_den=1001,
        field_based=2,
    )
    bobbed = _FakeClip(
        num_frames=600,
        fps_num=60000,
        fps_den=1001,
        field_based=0,
    )
    calls = {}

    class FakeQTempGaussMC:
        def __init__(self, **params):
            calls["params"] = params

        def deinterlace(self, clip, *, tff):
            calls["source"] = clip
            calls["tff"] = tff
            return bobbed

    class FakeStd:
        @staticmethod
        def SelectEvery(clip, *, cycle, offsets):
            calls["select_every"] = (cycle, offsets)
            return _FakeClip(
                num_frames=300,
                fps_num=30000,
                fps_den=1001,
                field_based=0,
                indices=clip.indices[::2],
            )

    class FakeVS:
        core = type("FakeCore", (), {"std": FakeStd()})()

    monkeypatch.setattr(qtgmc, "QTempGaussMC", FakeQTempGaussMC)
    monkeypatch.setattr(qtgmc, "vs", FakeVS)

    output = qtgmc.build_qtgmc_clip(
        source,
        qtgmc.QTGMCConfig("raw", "tff", "half"),
    )

    assert calls["tff"] is True
    assert calls["select_every"] == (2, 0)
    assert output.num_frames == 300
    assert (output.fps_num, output.fps_den) == (30000, 1001)
    assert output.indices == tuple(range(0, 600, 2))


def test_tc06_preset_registry_is_exact_and_immutable_and_report_expands_mapping():
    qtgmc = _qtgmc()

    assert set(qtgmc.PRESETS) == {"raw", "light", "strong"}
    assert dict(qtgmc.preset_parameters("raw")) == {}
    assert dict(qtgmc.preset_parameters("light")) == {
        "denoise_tr": 1,
        "source_match_mode": qtgmc.QTempGaussMC.SourceMatchMode.BASIC,
    }
    assert dict(qtgmc.preset_parameters("strong")) == {
        "denoise_tr": 2,
        "source_match_mode": qtgmc.QTempGaussMC.SourceMatchMode.REFINED,
    }

    parameters = qtgmc.preset_parameters("light")
    with pytest.raises(TypeError):
        parameters["denoise_tr"] = 99

    with pytest.raises(ValueError, match="unknown QTGMC preset"):
        qtgmc.preset_parameters("unknown")
    with pytest.raises(ValueError, match="unknown QTGMC preset"):
        qtgmc.build_qtgmc_clip(
            object(),
            qtgmc.QTGMCConfig("unknown", "tff", "double"),
        )

    report = qtgmc.build_qtgmc_report(
        qtgmc.QTGMCConfig("light", "tff", "double")
    )
    assert report["preset"] == "light"
    assert report["parameters"] == dict(parameters)


def test_tc07_publish_is_contract_gated_and_never_overwrites_existing_artifact():
    qtgmc = _qtgmc()
    tmp_path = _task6_temp_dir()
    try:
        partial = tmp_path.with_name(tmp_path.name + ".capture.partial.mkv")
        final = tmp_path.with_name(tmp_path.name + ".capture.mkv")
        partial.write_bytes(b"new-valid-artifact")
        final.write_bytes(b"existing-valid-artifact")

        with pytest.raises(FileExistsError):
            qtgmc.publish_qtgmc_artifact(partial, final, contract_passed=True)
        assert final.read_bytes() == b"existing-valid-artifact"
        assert partial.read_bytes() == b"new-valid-artifact"

        final.unlink()
        with pytest.raises(ValueError, match="contract"):
            qtgmc.publish_qtgmc_artifact(partial, final, contract_passed=False)
        assert not final.exists()

        qtgmc.publish_qtgmc_artifact(partial, final, contract_passed=True)
        assert final.read_bytes() == b"new-valid-artifact"
        assert not partial.exists()
    finally:
        for path in (
            tmp_path.with_name(tmp_path.name + ".capture.partial.mkv"),
            tmp_path.with_name(tmp_path.name + ".capture.mkv"),
        ):
            path.unlink(missing_ok=True)


def test_tc07_stale_partial_is_recorded_as_interrupted_and_never_published():
    qtgmc = _qtgmc()
    tmp_path = _task6_temp_dir()
    try:
        partial = tmp_path.with_name(tmp_path.name + ".capture.partial.mkv")
        final = tmp_path.with_name(tmp_path.name + ".capture.mkv")
        partial.write_bytes(b"interrupted")
        report = {}

        qtgmc.record_stale_partial(partial, final, report)

        assert report["status"] == "interrupted"
        assert report["partial_artifact"] == str(partial)
        assert not final.exists()
        assert partial.exists()
    finally:
        for path in (
            tmp_path.with_name(tmp_path.name + ".capture.partial.mkv"),
            tmp_path.with_name(tmp_path.name + ".capture.mkv"),
        ):
            path.unlink(missing_ok=True)


def _tc08_video_stream(
    *,
    codec_name,
    pix_fmt,
    field_order,
    frame_rate,
    frames,
):
    return {
        "index": 0,
        "codec_type": "video",
        "codec_name": codec_name,
        "pix_fmt": pix_fmt,
        "field_order": field_order,
        "width": 720,
        "height": 480,
        "avg_frame_rate": frame_rate,
        "nb_frames": str(frames),
        "color_range": "tv",
        "color_space": "smpte170m",
        "color_primaries": "smpte170m",
        "color_transfer": "smpte170m",
        "sample_aspect_ratio": "10:11",
        "display_aspect_ratio": "4:3",
    }


def _tc08_audio_stream(index, codec_name, packets):
    return {
        "index": index,
        "codec_type": "audio",
        "codec_name": codec_name,
        "nb_read_packets": str(packets),
    }


def test_tc08_ffv1_contract_checks_video_metadata_and_all_audio_packets():
    qtgmc = _qtgmc()
    source_probe = {
        "streams": [
            _tc08_video_stream(
                codec_name="mpeg2video",
                pix_fmt="yuv422p10le",
                field_order="tt",
                frame_rate="30000/1001",
                frames=300,
            ),
            _tc08_audio_stream(1, "aac", 100),
            _tc08_audio_stream(2, "ac3", 200),
        ]
    }
    output_probe = {
        "streams": [
            _tc08_video_stream(
                codec_name="ffv1",
                pix_fmt="yuv422p10le",
                field_order="progressive",
                frame_rate="60000/1001",
                frames=600,
            ),
            _tc08_audio_stream(1, "aac", 100),
            _tc08_audio_stream(2, "ac3", 200),
        ]
    }
    config = qtgmc.QTGMCConfig("light", "tff", "double")

    qtgmc.validate_encoded_contract(source_probe, output_probe, config)

    output_probe["streams"][0]["pix_fmt"] = "yuv420p10le"
    with pytest.raises(AssertionError, match="pix_fmt"):
        qtgmc.validate_encoded_contract(source_probe, output_probe, config)

# VHS-upscale

This PC (Windows 11 + AMD RX 7900 XT) 向けのローカル VHS 復元パイプライン。
29.97i VHS 素材を Windows 側 VapourSynth/QTGMC で lossless な 59.94p progressive
に変換し、WSL2/ROCm 上の TAPE(公式 checkpoint)でアップスケールする。
最終成果物は TAPE restored FFV1 video + 元音声の Matroska と、Modal RealBasicVSR
へ渡す handoff manifest である。

設計書: `docs/superpowers/specs/2026-09-14-rx7900xt-tape-rocm-design.md`
実装計画書: `docs/qtgmc-long-processing/implementation_plan.md`
実装記録: `docs/qtgmc-long-processing/implementation_record.md`

## レポジトリ構成

| パス | 内容 |
|---|---|
| `pipeline/` | 長時間処理パイプライン本体(media contract、chunk planner、manifest state machine、resumable reference cache、orchestrator `run_local`、TAPE chunk worker) |
| `vs_scripts/` | VapourSynth/QTGMC render スクリプト(Windows 側) |
| `scripts/` | `run_short_inference.py`(短尺ランナー)、`check_vapoursynth.py`(preflight)、`install_vapoursynth.ps1` |
| `tests/` | パイプラインテスト(unit / integration / vapoursynth / gpu / e2e marker グループ) |
| `docs/` | 設計書・計画書・テストケース・実装記録 |
| `input/` | VHS 素材(未トラッキング) |
| `work/` | 作業ディレクトリ(logs、baseline、run 成果物。大半は未トラッキング) |
| `TAPE/` | TAPE upstream クローン(未トラッキング、commit ピン留め) |
| `worktrees/` | TAPE ローカル worktree(未トラッキング) |

## TAPE との関係

TAPE は依存であり、このレポジトリの一部ではない。このレポジトリのコードは
TAPE のコードを import せず、明示パス(WSL subprocess)で呼び出す。

- パイプラインが使う TAPE チェックアウトは
  `E:/vhs-upscale/worktrees/tape-qtgmc-long-processing`
  (ブランチ `codex/tape-qtgmc-long-processing`)であり、**ローカル限定
  (push/PR しない)** である。
- そのブランチの tip は TAPE 側の変更のみを含む: `real_world_inference.py`
  (external reference mode CLI)、`data/real_world_video_dataset.py`、
  `utils/` の reference 選択 helpers(CLIP prompt ensemble・Otsu)、ROCm
  環境スクリプト、TAPE 側テスト。
- 制約: TAPE worktree は上記パスに置くこと(このレポジトリの `conftest.py`
  と `scripts/run_short_inference.py --worktree` の default が前提とする)。
- ROCm 実行環境(PyTorch/HIP venv)は
  `E:/vhs-upscale/worktrees/tape-rocm-rx7900xt-luna/.venv-rocm`
  (WSL 側では `/mnt/e/vhs-upscale/worktrees/tape-rocm-rx7900xt-luna/.venv-rocm`)。

## 長時間処理 CLI(`pipeline/run_local.py`)

```text
python -m pipeline.run_local analyze --input <source> --work-root work/local-pipeline/<run>
python -m pipeline.run_local preview --work-root <run> --range <HH:MM:SS-HH:MM:SS>
python -m pipeline.run_local select --work-root <run> --preset <raw|light|strong> --tape-input-rate <60000/1001|30000/1001>
python -m pipeline.run_local status --work-root <run>
python -m pipeline.run_local estimate --work-root <run>
python -m pipeline.run_local canary --work-root <run> --seconds 30
python -m pipeline.run_local run --work-root <run>
python -m pipeline.run_local resume --work-root <run>
python -m pipeline.run_local verify --work-root <run>
python -m pipeline.run_local batch --base-work-root <base> <source1> <source2> ...
```

- `analyze`: ffprobe による media contract(フレームレート、field order、色、SAR)。
- `preview`: QTGMC preset(raw/light/strong)× TAPE 入力レート(59.94p/29.97p)の比較候補。
- `select`: ユーザーによる human selection gate(選択を hash 付きで固定)。
- `run`/`resume`: reference pool → TAPE chunk(WSL ROCm、GPU lock)→ concat/remux/verify。
  失敗 chunk のみ再実行。ディスク不足は fail-closed(`InsufficientDiskError`)。
- `status`/`estimate`: 経過・ETA・disk margin の JSON レポート。
- 成果物は `work/tape/<safe-stem>-<source-hash12>_tape.mkv` + 隣接 handoff JSON。

## 短尺ランナー

```text
python scripts/run_short_inference.py --input <5-10s source> --checkpoint <ckpt> --output <dir> [--python <venv python>] [--worktree <TAPE worktree>]
```

5〜10 秒素材を TAPE で復元し FFV1 化 + 元音声 remux。WSL 側の venv python
を `--python` に渡して WSL 内で実行する(ROCm 推論は WSL 側で行うため)。

## Windows 側 VapourSynth

```powershell
python scripts/check_vapoursynth.py --json          # preflight
powershell -ExecutionPolicy Bypass -File scripts/install_vapoursynth.ps1   # 導入手順表示
```

`install_vapoursynth.ps1 -Apply` はユーザー承認なしに実行しない。

## テスト

```text
python -m pytest -m "not vapoursynth and not gpu and not integration and not e2e"   # unit
python -m pytest -m integration
python -m pytest -m vapoursynth      # Windows 側の VapourSynth 環境が必要
python -m pytest -m gpu              # WSL 経由の ROCm 環境が必要
python -m pytest -m e2e              # 実 29.97i 素材が必要(現状 Task 14 GATE 待ち)
```

canonical green は WSL ハーネス(`wsl.exe -d Ubuntu-22.04`、venv python、
`/mnt/e` 経由)で判定する。WSL ゲートスクリプトは
`work/logs/qtgmc-long-processing/` に置いている。

## 現状(2026-09-22)

- Task 0〜15 完了(実装 + 独立レビュー記録は implementation_record.md 参照)。
- Task 14 GATE: 実 29.97i VHS source が未着のため canary/本処理は STOPPED(記録のみ)。
- Task 16: (a) `input/` へ実 29.97i 素材、(b) ユーザー preview 選択、
  (c) CLI default runner 配線(F2, 記録付き waived)の到着を待つ。
- TC-01: QTGMC native plugin 欠如のため Windows VapourSynth 群は RED(環境要因)。
  `-Apply`(ユーザー承認必須)で green 化する見込み。

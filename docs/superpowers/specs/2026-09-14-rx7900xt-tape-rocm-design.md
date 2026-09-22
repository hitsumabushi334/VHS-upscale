# RX 7900 XT向けローカルVHS復元パイプライン 設計書

## 目的

このPCのRX 7900 XTを使い、QTGMC前処理、TAPE公式checkpointによる復元、長時間動画のchunk・resume処理までをローカルで成立させる。RealBasicVSRはこのローカルパイプラインから分離し、lossless中間成果物を入力とするModal上の別サブプロジェクトとして構築する。

## 現在確認できている環境

- Windows GPU: AMD Radeon RX 7900 XT
- Windows表示ドライバー: `32.0.31021.5001`
- WSLディストリビューション: Ubuntu 22.04、WSL2
- WSLカーネル: `6.6.87.1-microsoft-standard-WSL2`
- WSL Python: `3.10.12`
- WSL ROCm: 未導入（`rocminfo`、`rocm-smi`、`/opt/rocm*`を検出できない）
- WSL PyTorch: 未導入
- TAPE upstream: `E:\vhs-upscale\TAPE`、commit `fb97c608a1733dec5dbcd5aeb77ed84e29b793f7`

## スコープ

### 対象

- WSL2 Ubuntu 22.04上へのROCm/PyTorch環境構築
- RX 7900 XTの可視性確認
- TAPE推論依存の固定
- 公式TAPE checkpoint、CLIP RN50x4、Swin-T weightsの取得・整合性確認
- Stage 1～3 smoke test
- 既知の参照frame選択、空候補、resize不具合の最小修正
- 5～10秒の実VHS素材での推論
- Windows側VapourSynth/QTGMCによる29.97iから59.94pへのdeinterlace
- raw、軽い前処理、強い前処理のpreview比較
- TAPE後の59.94p維持と29.97p化の比較
- 長時間動画のchunk、overlap、stage単位resume、batch、logging
- 元音声の保持と最終成果物へのremux
- ローカル処理後のlossless中間成果物をModal RealBasicVSRへ渡す境界
- FPS、VRAM、エラー、出力shape、有限値の記録

### 今回のローカル設計から分離する対象

- RealBasicVSRの実装とGPU benchmark
- Modal image、upload/download、課金計測
- GUI
- Windowsドライバーの自動downgrade
- TAPEの学習および再学習

RealBasicVSRとModalは実施しないという意味ではなく、ローカルTAPEパイプライン完成後に別spec・別implementation planで実施する。

## 採用方式

既存のUbuntu 22.04とPython 3.10を維持し、AMDが公開するUbuntu 22.04/cp310向けROCm 7.2・PyTorch 2.9.1 wheelを隔離venvへ導入する。TAPEのCUDA用condaコマンドは実行しない。NumPyはAMDの既知制約に合わせて`1.26.4`へ固定する。

Windowsドライバーは現状のまま試す。AMDのROCm 7.2対応表にある基準ドライバーと一致しないため、Stage 1で失敗しても直ちにTAPEを改変しない。`/dev/dxg`、ROCm userspace、PyTorch wheel、Windowsドライバーの境界ごとに原因を分離し、ドライバー変更が必要な場合はユーザー承認を別途得る。

QTGMCはWindows側のVapourSynthで実行する。WSL2はTAPE専用とし、WindowsとWSLの双方から見える`E:\vhs-upscale\work`をstage境界にする。QTGMC出力とTAPE出力はMatroskaコンテナのFFV1を既定とし、容量または互換性に問題がある場合だけlossless H.264を比較する。音声は入力から別streamとして保持し、AI処理後にremuxする。

長時間処理は動画全体を一度にJPEG展開しない。短いpreviewでpresetを確定後、時間範囲付きchunkとoverlapに分割し、各stageのmanifestへ入力hash、処理設定、frame範囲、成果物hash、完了状態を記録する。同一manifestと成果物が揃うstageだけをskipし、設定変更時は該当stage以降を再実行する。

TAPE内部のframe中間形式は`png`を必須とし、公式既定値の`jpg`を本処理では禁止する。入力frame抽出と復元frame保存の両方でlossless PNGを使い、JPEGは高速な目視previewを明示的に要求した場合だけ許可する。TAPE後はPNG sequenceからFFV1 Matroskaを生成し、元音声をremuxした`work/tape/*.mkv`をローカルstageの正式成果物とする。

色と表示情報は単なるmetadata copyで済ませず、decode前後とTAPE後で`ffprobe`比較する。manifestには`pix_fmt`、`color_range`、`color_space`、`color_primaries`、`color_transfer`、SAR、DAR、field order、time base、frame rateを記録する。SD NTSCはBT.601系を既定仮説とするが、入力実測値を優先し、RGB/BGR往復後のYUV encodeでrangeとmatrixを明示する。

reference selectionはchunkごとに独立実行しない。同一入力動画・前処理preset・frame-rate条件についてCLIP featureとclean candidateを動画単位で一度計算し、resume可能な共通reference cacheとして保存する。各推論chunkはこの共通poolを参照し、chunk境界で復元傾向が変わることを避ける。動画全体のcacheが容量・時間上の制約になる場合は、まず候補frameのsampling間隔を測定し、scene分割への変更は比較結果を得てから判断する。

## 構成と責務

- `TAPE/requirements-rocm.txt`: Torch以外の推論依存を固定する。
- `TAPE/scripts/check_rocm.py`: GPU名、Torch、HIP、基本tensor演算を検証する。
- `TAPE/scripts/check_assets.py`: checkpointと外部weightsの存在、hash、ロード形式を検査する。
- `TAPE/scripts/smoke_checkpoint.py`: 実checkpointをstrict loadし、小型tensorでforwardしてshapeと有限値を検証する。
- `TAPE/tests/`: 参照frame index、空候補、portrait resizeの回帰テストを置く。
- `TAPE/docs/rocm-rx7900xt.md`: 実際に成功したコマンドとversionだけを記録する。
- `vs_scripts/qtgmc.py`: Windows側QTGMC処理と3種類の前処理presetを定義する。top-level `vapoursynth/`はinstalled `vapoursynth` Python moduleをshadowするため使用しない。
- `pipeline/chunks.py`: chunk範囲とoverlapを決定し、frame/timebase対応を保持する。
- `pipeline/references.py`: 動画単位のCLIP feature、clean candidate、reference割当てをcacheし、全chunkへ共有する。
- `pipeline/media_contract.py`: ffprobe情報、BT.601系の色変換条件、音声remux、正式成果物の検証を担当する。
- `pipeline/manifest.py`: stage状態、入力・設定・成果物hashを管理する。
- `pipeline/run_local.py`: analyze、QTGMC、TAPE、frame-rate選択、audio remuxを順次実行する。
- `tests/`: chunk境界、resume、manifest無効化、音声remuxを検証する。
- `work/preprocessed/`: QTGMC後のlossless動画を保存する。
- `work/references/`: 動画・preset・frame-rate別のCLIP featureと共通reference poolを保存する。
- `work/tape/`: TAPE後のFFV1映像へ元音声をremuxしたMatroskaを保存し、Modal RealBasicVSRへの正式入力境界とする。
- `work/logs/`: 実機検証・長時間処理ログを保存する。元動画とTAPE upstream成果物を上書きしない。

## 検証フロー

### Stage 0: ホスト境界

WSL2、`/dev/dxg`、Windows GPU/driver、Ubuntu release、空き容量を記録する。ROCmやPython packageはまだ変更しない。

### Stage 1: ROCm/PyTorch

ROCm userspaceと隔離venvを構築する。最初に`/dev/dxg`と選択されたWSL runtime方式を記録する。legacy ROCm 7.2 wheel経路では、AMD公式手順に従ってvenv内`torch/lib`の`libhsa-runtime64.so`をWSL互換runtimeへ更新してから`torch`をimportする。ROCDXG経路を採用する場合は同じ置換を機械的に実行せず、`librocdxg`と公式手順に基づいて検証する。

続いて`torch.cuda.is_available()`、`torch.version.hip`、`torch.cuda.get_device_name(0)`、FP32/FP16の基本tensor演算を検証する。期待値はRX 7900 XT、HIP非null、有限な演算結果である。`rocm-smi`や`amd-smi`はWSLで未対応または限定的なため、成功条件や必須telemetry sourceにしない。

### Stage 2: 依存と資産

TAPE推論entrypointをimportし、公式release checkpointをCPUへ`weights_only=True`優先でロードする。checkpoint、CLIP、Swin-T weightsは公式配布元のみから取得し、SHA-256を記録する。外部ネットワークなしで再試験できる状態にする。

### Stage 3: checkpoint済みforward

実checkpointを`strict=True`でモデルへロードし、GPUへ転送する。小型の入力・参照tensorでforwardし、出力shape、`torch.isfinite`、peak VRAMをassertする。AMP失敗時はFP32で再実行し、モデル非互換と精度mode非互換を分離する。

### Stage 4: 5～10秒実素材

既知のupstream不具合を回帰テスト付きで最小修正した後、`--frame-format png`を強制して短いVHS clipを処理する。入力・復元frameがPNGであり、JPEGが生成されないことを自動検査する。OpenCV decode/write、出力directory、disk消費を検査し、復元映像をFFV1化して元音声をremuxする。元素材はread-onlyとして扱い、出力は別directoryへ保存する。

### Stage 5: QTGMC統合とpreview比較

代表区間を抽出し、raw、軽い前処理、強い前処理をQTGMC 59.94pで生成する。それぞれについて次の二経路を比較する。

- A: QTGMC 59.94pをTAPEへ入力し、59.94pで出力する。
- B: QTGMC 59.94pを決定論的に29.97p化してからTAPEへ入力し、29.97pで出力する。

必要ならAのTAPE出力を29.97p化した条件も比較用に残す。過剰denoise、色滲み、顔・模様の変形、temporal flicker、動きの滑らかさ、TAPE FPS、総処理時間を比較し、長時間処理用presetとTAPE入力frame rateを決める。

### Stage 6: 長時間chunk・resume

短い素材で確定したpresetを使い、まず30～60秒、次に実VHS一本を処理する。推論chunkの作成前に、対象動画全体のCLIP featureとclean reference候補を一度計算して`work/references/`へcacheする。全chunkが同じreference poolを使うことをmanifestで検証する。

chunk境界はoverlap区間を含めて生成し、重複frameを決定論的に除去する。各stageはmanifestと成果物hashに基づきresumeでき、失敗chunkだけを再実行できることを故障注入テストで確認する。

### Stage 7: Modal RealBasicVSRへの引き渡し

`work/tape/*.mkv`を正式入力契約として固定する。videoはTAPE restored FFV1、audioはoriginal streamをremux済み、metadataは検証済みのframe rate、time base、SAR/DAR、色属性を持つ。chunk manifestとreference cache manifestを併置する。Modal側は映像streamだけをSRし、同じ音声を最終成果物へremuxする。ローカルパイプラインはRealBasicVSR未実装でも単独で完了・再開できる状態にする。

## TDDと変更方針

参照frame index、空候補、portrait resize、PNG強制、共通reference pool、chunk境界、resume、media contractは、現在のupstreamまたは未実装状態で失敗する回帰テストを先に作る。失敗を確認してから最小修正し、テストが通ることを確認する。ROCm API名を理由なく`torch.cuda`から変更せず、custom kernelの追加やモデルarchitectureのrewriteは行わない。

## 成功条件

1. `torch.cuda.is_available()`がtrueでRX 7900 XTが表示される。
2. `torch.version.hip`が非nullである。
3. TAPE公式checkpointがCPUで安全にロードされ、`strict=True`でモデルへ適用できる。
4. RX 7900 XT上のcheckpoint済みforwardが有限値を返す。
5. 5～10秒素材が異常終了せず処理される。
6. TAPE本処理の入力・復元中間frameがPNGで、JPEGが生成されない。
7. QTGMC 59.94p出力をTAPEへlosslessで渡せる。
8. 59.94pと、TAPE入力前に29.97p化する条件をpreviewで比較できる。
9. 長時間動画が共通reference poolを使ってchunk処理され、途中失敗後に成功済みstageを再実行せず再開できる。
10. `work/tape/*.mkv`にTAPE restored FFV1 videoと元音声が格納される。
11. `pix_fmt`、`color_range`、`color_space`、`color_primaries`、`color_transfer`、SAR、DAR、field order、time base、frame rateがmanifestとffprobe比較で検証される。
12. 実行環境、hash、所要時間、peak VRAM、失敗時の原因分類がログに残る。
13. TAPE upstreamの既知ロジック不具合、PNG強制、共通reference pool、chunk/resumeに回帰テストがある。
14. `work/tape/`成果物がModal RealBasicVSRの入力契約を満たす。

## 停止条件とフォールバック

- Stage 1失敗時はTAPE sourceを変更せず、Windows driver、`/dev/dxg`、ROCm userspace、wheelのどこで失敗したかを確定する。
- checkpoint strict load失敗時は、checkpoint形式とPyTorchの`weights_only`既定変更を先に調べる。
- AMPだけ失敗する場合はFP32を暫定baselineとする。
- unsupported operatorが確認された場合だけ、最小再現を作ってHIP対応またはModal fallbackの費用対効果を判断する。
- Windowsドライバー変更、system-wide package削除、既存WSL環境の破壊的変更は自動実行しない。

## 実装体制

メインエージェントは計画、受入基準、レビュー、最終判定を担当する。実装と実機作業は`gpt-5.6-luna`、reasoning effort `xhigh`へ委任し、各TDD単位の後に独立レビューを行う。

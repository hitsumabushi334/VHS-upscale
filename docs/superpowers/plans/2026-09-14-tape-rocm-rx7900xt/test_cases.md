# TAPE ROCm RX 7900 XT テストケース

## 実行グループ

- `unit`: TC-01～TC-07、mock assetを使うTC-09～TC-10。GPU、network、実assetを要求しない。
- `gpu`: TC-08、TC-11。RX 7900 XTと構築済みROCm環境を要求する。
- `integration`: 実assetを使うTC-09～TC-10、TC-12～TC-14。FFmpeg/ffprobeと短尺fixtureを要求する。
- 各testは`pytest.ini`に登録した`gpu`、`integration` markerを使い、最終acceptanceで3群を個別実行する。

## TC-01: 参照候補indexの再対応

- 入力frame名: `000.png`、`001.png`、`002.png`、`003.png`
- clean candidate index: `[1, 3]`
- candidate内ranking: `[1, 0]`
- 期待結果: `003.png`、`001.png`
- 禁止結果: `001.png`、`000.png`

## TC-02: clean candidateが空

- 入力frame名: 3件
- clean candidate index: 空配列
- reference数: 5
- 期待結果: 決定論的fallbackで5件を返し、`IndexError`を発生させない

## TC-03: reference不足のpadding

- clean candidate: 2件
- reference数: 5
- 期待結果: 上位2件の後、先頭referenceを3回繰り返して常に5件を返す

## TC-04: portrait resize

- 入力: width 480、height 720、patch size 512
- 期待結果: width 341、height 512
- landscape入力 width 720、height 480の期待結果: width 512、height 341

## TC-05: 推論時のLightning非依存

- `pytorch_lightning`をimport不能にした環境で`data.real_world_video_dataset`をimportする
- 期待結果: import成功
- `init_logger()`を呼んだ場合だけ、説明付き依存エラーを返す

## TC-06: lossless frame形式

- CLI引数なしでparserを生成する
- 期待結果: `frame_format == "png"`
- 5frameのテスト動画を処理する
- 期待結果: input/restored frameはすべて`.png`で、`.jpg`と`.jpeg`は0件

## TC-07: ROCm環境契約

- Ubuntu 22.04、Python 3.10環境でsetup preflightを実行する
- 期待結果: OS、Python、`/dev/dxg`、runtime方式をJSONへ記録
- `--runtime legacy|rocdxg`の明示指定を要求し、省略時または未知値では変更前に失敗する
- legacy ROCm 7.2経路: venv内`libhsa-runtime64.so`が`/opt/rocm/lib/libhsa-runtime64.so.1.2`由来であること
- ROCDXG経路: `librocdxg`を検出し、legacy用削除処理を実行しないこと
- dry-runのROCDXG probeは`runtime=rocdxg`と`replace_legacy_hsa=false`を返す
- dry-runのlegacy probeは`runtime=legacy`と`replace_legacy_hsa=true`を返す
- dry-runはvenv、package、runtime libraryを変更しない
- ROCDXG preflight失敗時にlegacyへ自動fallbackしない

## TC-08: RX 7900 XT可視性

- 期待結果: `torch.cuda.is_available() is True`
- 期待結果: `torch.version.hip`が非null
- 期待結果: device名に`7900 XT`を含む
- FP32 matrix multiplication結果がすべて有限であることを必須とする
- FP16は別試験としてfinite/errorを記録し、失敗してもFP32成功と混同しない

## TC-09: asset provenance

- TAPE checkpoint sourceはGitHub release `weights/checkpoint.pth`
- CLIPはcommit `a1d0717`のRN50x4
- Swin-Tはtorchvision 0.24.0の公式weights
- 期待結果: URL、byte size、SHA-256、取得日時を`work/assets/assets.json`へ記録
- 期待結果: hashが変化したassetを再利用しない
- checkpointのcontainer形式とtop-level key概要を記録し、raw tensor mappingまたはupstream対応済みLightning `state_dict` wrapper以外を拒否

## TC-10: checkpoint strict load

- checkpointをCPUへ`weights_only=True`優先でロード
- 期待結果: `SwinUNet.load_state_dict(..., strict=True)`成功
- missing key、unexpected keyはいずれも0件
- constructorとcheckpoint解包規則は`docs/tape-model-contract.md`に記録したupstream sourceを根拠とし、未知形式を推測で解包しない

## TC-11: checkpoint済みGPU forward

- batch 1、input frames 5、reference frames 5、RGB、64x64 tensor
- input layoutは`[B,T,C,H,W]`、reference layoutは`[B,D,C,H,W]`
- 期待結果: 出力shapeはupstream `models/swin_unet.py:176-187`に基づく`[1, 5, 3, 64, 64]`
- 期待結果: `torch.isfinite(output).all()`がtrue
- AMP失敗時はFP32を別試験として実行し、結果を混同しない

## TC-12: 5～10秒動画

- 入力: `input/VHS_10s.mkv`
- 入力契約: progressive、CFR、5～10秒。raw interlaced captureまたはVFRは本計画では拒否
- 期待結果: 入力を変更しない
- 期待結果: PNG intermediate、FFV1 video、元音声を含むMatroskaを別directoryへ生成
- 期待結果: decoder/writer open、frame数、fps、durationを検証
- 期待結果: JPEG intermediateは0件

## TC-13: media contract

- `ffprobe`で入力と出力を比較
- 入力manifest項目: `pix_fmt`、`color_range`、`color_space`、`color_primaries`、`color_transfer`、SAR、DAR、`field_order`、`avg_frame_rate`、`r_frame_rate`、`time_base`、audio stream情報
- 保持項目: 色属性、SAR/DAR、CFR frame rate、audio stream数・codec・duration
- 意図的変更項目: video codecはFFV1、`pix_fmt`は`yuv422p10le`、`field_order`はprogressive、container/time baseはMatroska mux結果
- `time_base`は文字列完全一致ではなく、frame count、CFR rate、duration/timestamp整合で検証
- 必須の色・SAR情報が欠損した入力は推測で補わず拒否
- QTGMC後続計画へ渡す入力境界はprogressive/CFRで、59.94pまたは比較対象29.97p、検証済み色属性・SAR/DARを持つ
- PNG保存時bit depth、OpenCV decode dtype、model input/output dtype・value range、FFV1 encode bit depthをreportへ記録
- 現行upstreamの8-bit PNG経路を明記し、10-bit FFV1と8-bit信号精度を同一視しない

## TC-14: 非破壊性

- 実行前後で入力のsize、mtime、SHA-256を比較
- 期待結果: すべて一致
- 既存output directoryへ実行しても入力を変更せず、明確なエラーまたはresume動作になる

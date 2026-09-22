# 独立レビュー記録: QTGMC・長時間ローカル処理

> 実施日: 2026-09-20  
> reviewer model: `cursor/glm-5.2`  
> Codex session: `01a0bd3b-5b44-75c0-b71f-e029fb5c1cc6`  
> 判定: Critical 0、Important 8、条件付き可

## 反映した必須指摘

| # | 指摘 | 解消内容 |
|---|---|---|
| 1 | WSL起動方法が曖昧 | `wsl.exe -d ... -- <absolute-python> <absolute-script> ...`の純argvへ固定し、`bash -lc`を禁止 |
| 2 | 複数audioと既存short契約が衝突 | low-level probe helperだけ共有し、longは1本以上、shortはちょうど1本のpreflightへ分離 |
| 3 | CLIP入力の色順が未定義 | FFmpeg `rgb24`からNumPy `uint8[h,w,3]`、PIL RGB、既存preprocessの順を固定 |
| 4 | reference algorithmの変更点が不明 | prompt/normalize/Otsuは維持し、all-pairsからtimeline-bin固定poolへの変更を明記 |
| 5 | 正式成果物pathが設計書と不一致 | 計画側を設計書の`work/tape/*.mkv`契約へ合わせ、hash付き安全名へ固定 |
| 6 | `real_world_inference.py`がmonolith | extraction、selection、restoreへ先に分割し、short回帰GREEN後にexternal modeを追加 |
| 7 | source stem/concat pathが未防御 | ASCII safe stem、hash suffix、trusted-root検査、CR/LF/単一quote拒否を追加 |
| 8 | 実装baseが曖昧 | `c4ee9aae...`を直接baseとし、統合済みの場合だけancestor確認済みdescendantを許可 |

## 追加で反映した改善

- external modeのmode判定とskip範囲を固定した。
- chunk encode/concatが同一Windows FFmpeg path/version/CFR policyを使う契約を追加した。
- CLIP feature、pool PNG、TAPE PNGの8-bit精度境界を明記した。
- long modeの空reference poolはfail closedとし、short用fallbackを禁止した。
- feature resumeはv1ではshard単位に限定した。
- 将来QTGMC chunkingを行う場合のfull-vs-chunk seam testを必須化した。
- fault injection使用時の`fault_injected=true`記録をtestへ追加した。

正式成果物pathに関する指摘は、元設計書を変更してrun-local配下へ寄せるのではなく、実装計画を既存設計の`work/tape`境界へ適合させて解消した。

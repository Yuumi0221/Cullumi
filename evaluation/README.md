# 评估工具

`evaluation` 提供眨眼检测、NIQE 和扫描性能评估。脚本只读取本地样本，结果写入指定目录，不修改原始照片。

## 眨眼检测评估

准备授权盲测集后运行。清单需要包含照片、分组和授权信息，标注文件需要包含人脸框及睁闭眼状态。
盲测集建议至少包含 300 张照片和 60 组连拍。

```powershell
.\.venv\Scripts\python.exe -m evaluation.evaluate_blink `
  --manifest evaluation\dataset_manifest.csv `
  --annotations evaluation\annotations.csv `
  --output evaluation\results `
  --profile balanced --runs 3 --warmup 20
```

工具会输出逐张预测、耗时和汇总报告，并检查授权字段。

## NIQE 评估

`benchmark_niqe.py` 使用最长边 512 像素的预览，统计 NIQE 的初始化时间、P50、P95 和每千张照片的预计耗时。
默认取 120 张样本，可用 `--limit` 调整。需要和质量实验室版本对照时，同时传入 `--lab-python` 与 `--lab-root`。

```powershell
.\.venv\Scripts\python.exe -m evaluation.benchmark_niqe `
  ..\cullumi-quality-lab\test-photos `
  --limit 120 `
  --output evaluation\performance-results\niqe.json
```

NIQE 参数哈希、许可证、实验室数值一致性和退化图片由 `tests/test_niqe.py` 覆盖。

## 性能基准

- `benchmark_scan.py` 检查首次扫描、未变化复扫、单张失效和单进程与双进程吞吐。
- `benchmark_raw_preview.py` 比较 RAW 完整解码和缩小解码的速度、指标及阈值变化。
- `benchmark_large_library.py` 测量大图库发现、相似分组、查询和完全重复确认，可用 `--quick` 做快速检查，或用 `--compare` 对照两次结果。

示例

```powershell
.\.venv\Scripts\python.exe -m evaluation.benchmark_scan `
  ..\cullumi-quality-lab\test-photos `
  --repeats 2 `
  --output evaluation\performance-results\scan.json

.\.venv\Scripts\python.exe -m evaluation.benchmark_raw_preview `
  ..\cullumi-quality-lab\test-photos `
  --output evaluation\performance-results\raw.json

.\.venv\Scripts\python.exe -m evaluation.benchmark_large_library `
  --quick `
  --output evaluation\performance-results\library.json
```

`niqe_frozen_probe.py` 用于便携版冒烟检查，确认 NIQE 参数、许可证和并行分析进程都能正常启动。

日常回归运行 `verify.ps1`，需要浏览器检查时运行 `verify.ps1 -Browser`。基准输出和打包产物位于已忽略的
`evaluation/performance-results` 目录，不应提交到版本库。

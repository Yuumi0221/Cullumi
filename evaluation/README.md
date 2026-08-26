# 眨眼发布评估

最终盲测集至少需要 300 张、60 组已授权连拍。`dataset_manifest.csv` 必须包含
`photo_id,group_id,path,quality_score,authorized,license_id`；`annotations.csv`
必须包含 `photo_id,face_id,x,y,width,height,status,primary`。人脸框使用 YuNet
640×640 分析画布坐标，状态为 `open/closed/uncertain/not_analyzable`。照片编号
必须唯一，每组至少 2 张，清单中的每张照片都必须有至少一行人工标注；没有人脸时
仍需添加一行 `not_analyzable` 标注。

两名标注者应独立标注并经第三人裁决后再生成最终 `annotations.csv`。阈值冻结后执行：

```powershell
.\.venv\Scripts\python.exe evaluation\evaluate_blink.py `
  --manifest evaluation\dataset_manifest.csv `
  --annotations evaluation\annotations.csv `
  --output evaluation\results `
  --profile balanced --runs 3 --warmup 20
```

工具验证授权字段，输出逐人脸预测、逐张耗时、JSON 与 Markdown 报告，并按精确率
95%、召回率 80%、推荐成功率 90% 和 P50 50 ms 四项门槛返回退出码。

## 大图库性能基线

大图库基准独立于普通单元测试。默认生成 10 万个发现条目、5 万张照片元数据、
1 万条相似边、1 千组 RAW/JPG 拍摄变体和 5 千张按 100 种大小分组的完全重复照片，
记录耗时、Python 峰值内存、进程常驻内存增量、接口响应体积、SQL 语句数、取消响应
时间和确定性的结果摘要。基准还覆盖图库深分页和相似组首屏分页：

```powershell
.\.venv\Scripts\python.exe evaluation\benchmark_large_library.py `
  --output evaluation\performance-results\baseline.json
```

修改后使用同一规模复跑并生成对比字段：

```powershell
.\.venv\Scripts\python.exe evaluation\benchmark_large_library.py `
  --compare evaluation\performance-results\baseline.json `
  --output evaluation\performance-results\optimized.json
```

开发时可添加 `--quick` 只做流程冒烟。性能结果依赖机器和当前负载，因此不会由普通
测试套件作耗时或内存断言；结果摘要一致性和查询数量约束由单元测试继续保护。
定位单项回退时可使用 `--only similarity_groups`、`--only similarity_api` 或
`--only photo_queries` 等参数只运行一个场景。

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

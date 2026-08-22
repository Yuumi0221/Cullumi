from __future__ import annotations

import argparse
import csv
import json
import statistics
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from cullumi.config import BUILTIN_PROFILES, validate_profile
from cullumi.face_analysis import MODEL_VERSION, FaceAnalyzer

TRUE_VALUES = {"1", "true", "yes", "是"}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        return list(csv.DictReader(source))


def number(row: dict[str, str], key: str) -> float:
    try:
        return float(row[key])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"{key} 必须是数字：{row.get(key, '')}") from error


def iou(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_x2, left_y2 = left["x"] + left["width"], left["y"] + left["height"]
    right_x2, right_y2 = right["x"] + right["width"], right["y"] + right["height"]
    width = max(0.0, min(left_x2, right_x2) - max(left["x"], right["x"]))
    height = max(0.0, min(left_y2, right_y2) - max(left["y"], right["y"]))
    intersection = width * height
    union = left["width"] * left["height"] + right["width"] * right["height"] - intersection
    return intersection / union if union > 0 else 0.0


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lower = int(index)
    upper = min(len(ordered) - 1, lower + 1)
    weight = index - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def load_profile(value: str) -> dict[str, Any]:
    if value in BUILTIN_PROFILES:
        profile = json.loads(json.dumps(BUILTIN_PROFILES[value]))
    else:
        profile = json.loads(Path(value).read_text(encoding="utf-8"))
    validate_profile(profile)
    return profile


def annotation_rows(path: Path) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in read_csv(path):
        status = row.get("status", "").strip().lower()
        if status not in {"open", "closed", "uncertain", "not_analyzable"}:
            raise ValueError(f"无效人工状态：{status}")
        grouped[row["photo_id"]].append({
            "face_id": row["face_id"],
            "x": number(row, "x"),
            "y": number(row, "y"),
            "width": number(row, "width"),
            "height": number(row, "height"),
            "status": status,
            "primary": row.get("primary", "true").strip().lower() in TRUE_VALUES,
        })
    return grouped


def match_faces(
    predictions: list[dict[str, Any]],
    annotations: list[dict[str, Any]],
) -> list[tuple[int, int, float]]:
    candidates = sorted(
        (
            (iou(prediction, annotation), prediction_index, annotation_index)
            for prediction_index, prediction in enumerate(predictions)
            for annotation_index, annotation in enumerate(annotations)
        ),
        reverse=True,
    )
    matches: list[tuple[int, int, float]] = []
    used_predictions: set[int] = set()
    used_annotations: set[int] = set()
    for overlap, prediction_index, annotation_index in candidates:
        if overlap < 0.5:
            break
        if prediction_index in used_predictions or annotation_index in used_annotations:
            continue
        used_predictions.add(prediction_index)
        used_annotations.add(annotation_index)
        matches.append((prediction_index, annotation_index, overlap))
    return matches


def aggregate_metrics(
    predictions: dict[str, dict[str, Any]],
    annotations: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    true_positive = false_positive = false_negative = reliable_matches = eligible = 0
    ground_truth_ratio: dict[str, float | None] = {}
    for photo_id, result in predictions.items():
        truth = [
            item
            for item in annotations.get(photo_id, [])
            if item["primary"] and item["status"] in {"open", "closed"}
        ]
        eligible += len(truth)
        closed_truth = sum(item["status"] == "closed" for item in truth)
        ground_truth_ratio[photo_id] = closed_truth / len(truth) if truth else None
        faces = result["faces"]
        matches = match_faces(faces, truth)
        matched_truth = {truth_index: prediction_index for prediction_index, truth_index, _ in matches}
        for prediction_index, truth_index, _ in matches:
            prediction_status = faces[prediction_index]["status"]
            truth_status = truth[truth_index]["status"]
            if prediction_status in {"open", "closed"}:
                reliable_matches += 1
            if prediction_status == "closed":
                if truth_status == "closed":
                    true_positive += 1
                else:
                    false_positive += 1
        matched_prediction_ids = {prediction_index for prediction_index, _, _ in matches}
        false_positive += sum(
            face["status"] == "closed" and index not in matched_prediction_ids
            for index, face in enumerate(faces)
        )
        false_negative += sum(
            item["status"] == "closed"
            and (
                truth_index not in matched_truth
                or faces[matched_truth[truth_index]]["status"] != "closed"
            )
            for truth_index, item in enumerate(truth)
        )
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
    return {
        "tp": true_positive,
        "fp": false_positive,
        "fn": false_negative,
        "precision": precision,
        "recall": recall,
        "reliable_coverage": reliable_matches / eligible if eligible else 0.0,
        "eligible_faces": eligible,
        "ground_truth_ratio": ground_truth_ratio,
    }


def recommendation_success(
    manifest: list[dict[str, str]],
    predictions: dict[str, dict[str, Any]],
    truth_ratio: dict[str, float | None],
    coverage_min: float,
) -> tuple[int, int]:
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in manifest:
        if row.get("group_id"):
            groups[row["group_id"]].append(row)
    successes = eligible_groups = 0
    for members in groups.values():
        ratios = [truth_ratio.get(row["photo_id"]) for row in members]
        if not any(ratio == 0 for ratio in ratios):
            continue
        eligible_groups += 1

        def rank(row: dict[str, str]) -> tuple[Any, ...]:
            result = predictions[row["photo_id"]]
            face_count = int(result["blink_face_count"])
            reliable = face_count - int(result["blink_uncertain_face_count"])
            coverage = reliable / face_count if face_count else 0.0
            status = result["blink_status"]
            category = 1
            if status == "open" and face_count and coverage >= coverage_min:
                category = 0
            elif status == "closed" and coverage >= coverage_min:
                category = 2
            quality = float(row.get("quality_score") or 0)
            return (
                category,
                float(result["blink_closed_ratio"] or 0) if category == 2 else 0,
                int(result["blink_closed_face_count"]) if category == 2 else 0,
                -quality,
                row.get("path", "").casefold(),
            )

        recommended = min(members, key=rank)
        best_ratio = min(ratio for ratio in ratios if ratio is not None)
        if truth_ratio.get(recommended["photo_id"]) == best_ratio:
            successes += 1
    return successes, eligible_groups


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    with path.open("w", encoding="utf-8-sig", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Cullumi 授权连拍眨眼发布评估")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--annotations", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", default="balanced")
    parser.add_argument("--model-root", type=Path, default=Path("models"))
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--warmup", type=int, default=20)
    args = parser.parse_args()

    manifest = read_csv(args.manifest)
    if len(manifest) < 300:
        raise ValueError("最终盲测清单必须至少包含 300 张照片")
    photo_ids = [row.get("photo_id", "").strip() for row in manifest]
    if any(not photo_id for photo_id in photo_ids):
        raise ValueError("每张照片都必须填写 photo_id")
    if len(set(photo_ids)) != len(photo_ids):
        raise ValueError("最终盲测清单中的 photo_id 必须唯一")
    for row in manifest:
        if row.get("authorized", "").strip().lower() not in TRUE_VALUES:
            raise ValueError(f"照片未记录授权：{row.get('photo_id', '')}")
        if not row.get("license_id", "").strip():
            raise ValueError(f"照片缺少授权编号：{row.get('photo_id', '')}")
    grouped_manifest: dict[str, int] = defaultdict(int)
    for row in manifest:
        grouped_manifest[row.get("group_id", "").strip()] += 1
    burst_groups = {
        group_id
        for group_id, count in grouped_manifest.items()
        if group_id and count >= 2
    }
    if len(burst_groups) < 60:
        raise ValueError("最终盲测清单必须至少包含 60 组、每组至少 2 张连拍")
    annotations = annotation_rows(args.annotations)
    unknown_annotations = set(annotations) - set(photo_ids)
    if unknown_annotations:
        raise ValueError(
            f"人工标注包含清单外照片：{sorted(unknown_annotations)[0]}"
        )
    missing_annotations = set(photo_ids) - set(annotations)
    if missing_annotations:
        raise ValueError(
            f"照片缺少人工标注：{sorted(missing_annotations)[0]}"
        )
    profile = load_profile(args.profile)
    analyzer = FaceAnalyzer(args.model_root.resolve())
    paths = [
        (args.manifest.parent / row["path"]).resolve()
        if not Path(row["path"]).is_absolute()
        else Path(row["path"])
        for row in manifest
    ]
    dummy = {"cover_source": "still", "cover_time_ms": 0, "cover_revision": 0}
    for path in paths[: max(0, args.warmup)]:
        analyzer.analyze_detailed(path, dummy, profile)

    first_results: dict[str, dict[str, Any]] = {}
    prediction_rows: list[dict[str, Any]] = []
    performance_rows: list[dict[str, Any]] = []
    all_times: list[float] = []
    for run in range(1, max(1, args.runs) + 1):
        for row, path in zip(manifest, paths):
            started = time.perf_counter()
            result = analyzer.analyze_detailed(path, dummy, profile)
            elapsed_ms = (time.perf_counter() - started) * 1000
            all_times.append(elapsed_ms)
            performance_rows.append({
                "run": run,
                "photo_id": row["photo_id"],
                "elapsed_ms": round(elapsed_ms, 3),
            })
            if run != 1:
                continue
            first_results[row["photo_id"]] = result
            for index, face in enumerate(result["faces"], 1):
                prediction_rows.append({
                    "photo_id": row["photo_id"],
                    "prediction_id": index,
                    **face,
                })

    metrics = aggregate_metrics(first_results, annotations)
    blink = profile["similarity"]["blink"]
    recommended, eligible_groups = recommendation_success(
        manifest,
        first_results,
        metrics.pop("ground_truth_ratio"),
        float(blink["reliable_coverage_min"]),
    )
    recommendation_rate = recommended / eligible_groups if eligible_groups else 0.0
    report = {
        "photos": len(manifest),
        "groups": len(burst_groups),
        "model_version": MODEL_VERSION,
        "thresholds": blink,
        **metrics,
        "recommendation_successes": recommended,
        "recommendation_eligible_groups": eligible_groups,
        "recommendation_success_rate": recommendation_rate,
        "performance_ms": {
            "p50": percentile(all_times, 0.5),
            "p95": percentile(all_times, 0.95),
            "mean": statistics.fmean(all_times) if all_times else 0.0,
        },
    }
    report["release_gates"] = {
        "precision_at_least_95": report["precision"] >= 0.95,
        "recall_at_least_80": report["recall"] >= 0.80,
        "recommendation_at_least_90": recommendation_rate >= 0.90,
        "p50_at_most_50_ms": report["performance_ms"]["p50"] <= 50,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    write_csv(args.output / "predictions.csv", prediction_rows)
    write_csv(args.output / "performance_runs.csv", performance_rows)
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    markdown = [
        "# Cullumi 眨眼发布评估",
        "",
        f"- 照片：{report['photos']} 张",
        f"- 闭眼精确率：{report['precision']:.2%}",
        f"- 闭眼召回率：{report['recall']:.2%}",
        f"- 可靠覆盖率：{report['reliable_coverage']:.2%}",
        f"- 推荐成功率：{recommendation_rate:.2%}",
        f"- P50 / P95：{report['performance_ms']['p50']:.2f} / {report['performance_ms']['p95']:.2f} ms",
        "",
        "## 发布门槛",
        "",
        *[
            f"- {'通过' if passed else '未通过'}：{name}"
            for name, passed in report["release_gates"].items()
        ],
    ]
    (args.output / "report.md").write_text("\n".join(markdown), encoding="utf-8")
    return 0 if all(report["release_gates"].values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())

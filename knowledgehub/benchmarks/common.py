"""实验公共设施（§15 总原则）：固定种子、原始 CSV、统计图、环境记录。

所有实验脚本共用：percentile 统计、CSV 追加写入、matplotlib 中文出图、
运行环境元数据（硬件/软件版本/配置）落 JSON，保证结果可复现可追溯。
"""

import csv
import json
import platform
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

RESULTS_DIR = Path(__file__).resolve().parent / "results"

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False


def ensure_results_dir() -> Path:
    RESULTS_DIR.mkdir(exist_ok=True)
    return RESULTS_DIR


def percentile(values: list[float], p: float) -> float:
    """线性插值百分位（ms 由调用方保证单位一致）。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * p / 100.0
    lower, upper = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (k - lower)


def stats(values: list[float]) -> dict:
    return {
        "n": len(values),
        "p50_ms": round(percentile(values, 50), 3),
        "p95_ms": round(percentile(values, 95), 3),
        "p99_ms": round(percentile(values, 99), 3),
        "mean_ms": round(sum(values) / len(values), 3) if values else 0.0,
        "stdev_ms": round(_stdev(values), 3),
    }


def _stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return (sum((v - mean) ** 2 for v in values) / (len(values) - 1)) ** 0.5


def append_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    """追加写 CSV（首轮写表头）。原始数据不覆盖、只追加。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def save_environment(name: str, extra: dict | None = None) -> Path:
    env = {
        "experiment": name,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "host": socket.gethostname(),
        "platform": platform.platform(),
        "processor": platform.processor(),
        "python": platform.python_version(),
        "seed_policy": "随机种子固定于数据生成脚本参数，见各实验脚本头部",
    }
    if extra:
        env.update(extra)
    path = ensure_results_dir() / f"{name}_environment.json"
    path.write_text(json.dumps(env, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def line_chart(path: Path, series: dict[str, list[tuple[float, float]]], title: str,
               xlabel: str, ylabel: str, logy: bool = False) -> Path:
    """多序列折线图（series: {标签: [(x, y), ...]}）。"""
    fig, ax = plt.subplots(figsize=(7, 4.5), dpi=150)
    for label, points in series.items():
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        ax.plot(xs, ys, marker="o", label=label)
    ax.set_title(title)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    if logy:
        ax.set_yscale("log")
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return path


def bar_chart(path: Path, categories: list[str], values: list[float], title: str,
              ylabel: str) -> Path:
    fig, ax = plt.subplots(figsize=(7, 4.5), dpi=150)
    ax.bar(categories, values, color="#526bea", alpha=0.85)
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.grid(True, axis="y", alpha=0.3)
    for i, v in enumerate(values):
        ax.text(i, v, f"{v:.1f}", ha="center", va="bottom", fontsize=9)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return path


class Timer:
    def __init__(self):
        self.t0 = time.perf_counter()

    def ms(self) -> float:
        return (time.perf_counter() - self.t0) * 1000

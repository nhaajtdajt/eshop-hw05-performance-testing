"""Analyze the HTTP, workflow, and resource records collected by Run-Scenario.ps1."""

import argparse
from collections import Counter, defaultdict
import csv
from dataclasses import dataclass
from datetime import datetime
import json
import math
from pathlib import Path
from statistics import median

from make_plans import SCENARIOS, STEPS


ROOT = Path(__file__).resolve().parents[1]
HTTP_LABELS = {step[0] for step in STEPS}
FLOW = "flow_03"
HTTP_P95_LIMIT = 120
FLOW_P95_LIMIT = 600
ERROR_LIMIT = 1
SOAK_RPS_VARIATION = 0.15
SOAK_MEMORY_GROWTH_MIB = 32


@dataclass(frozen=True, slots=True)
class Sample:
    timestamp: int
    elapsed: int
    idle: int
    label: str
    ok: bool
    code: str
    stage: str
    line: int

    @property
    def end(self):
        return self.timestamp + self.elapsed + self.idle

    @property
    def event(self):
        return self.end if self.label == FLOW else self.timestamp


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as file:
        yield from csv.DictReader(file)


def metrics(samples, seconds):
    if not samples or seconds <= 0:
        return None
    times = sorted(s.elapsed for s in samples)
    failures = Counter(s.code for s in samples if not s.ok)
    last = max(samples, key=lambda s: s.elapsed)
    return {
        "n": len(samples), "mean": round(sum(times) / len(times), 2),
        "p95": times[math.ceil(len(times) * 0.95) - 1], "max": times[-1],
        "rps": round(len(samples) / seconds, 2),
        "error_pct": round(100 * sum(failures.values()) / len(samples), 4),
        "failures": dict(failures), "max_line": last.line, "max_timestamp": last.timestamp,
    }


def window(samples, start, end):
    seconds = (end - start) / 1000
    grouped = defaultdict(list)
    http, flow = [], []
    for s in samples:
        if start <= s.event < end:
            grouped[s.label].append(s)
            (flow if s.label == FLOW else http).append(s)
    return {
        "start_ms": start, "end_ms": end, "seconds": round(seconds, 3),
        "http": metrics(http, seconds), "flow": metrics(flow, seconds),
        "endpoints": {label: metrics(rows, seconds) for label, rows in sorted(grouped.items())
                      if label in HTTP_LABELS},
    }


def stable(record):
    http, flow = record["http"], record["flow"]
    return (
        http is not None and flow is not None and record["endpoints"].keys() == HTTP_LABELS
        and all(m["p95"] <= HTTP_P95_LIMIT and m["error_pct"] <= ERROR_LIMIT
                for m in record["endpoints"].values())
        and flow["p95"] <= FLOW_P95_LIMIT and flow["error_pct"] <= ERROR_LIMIT
    )


def read_run(scenario, results):
    folder = results / scenario
    run = json.loads((folder / "run.json").read_text(encoding="utf-8-sig"))
    props = dict(p.split("=", 1) for p in run["load_properties"])
    samples = []
    for line, row in enumerate(read_csv(folder / f"{scenario}.jtl"), 2):
        label = row["label"]
        if label in HTTP_LABELS or label == FLOW:
            samples.append(Sample(int(row["timeStamp"]), int(row["elapsed"]), int(row.get("IdleTime") or 0), label,
                                  row["success"].lower() == "true", row["responseCode"],
                                  row["threadName"].split()[0], line))
    http_samples = [s for s in samples if s.label in HTTP_LABELS]
    if not http_samples:
        raise ValueError(f"{scenario}: JTL does not contain HTTP samples for this workflow")
    head = min(s.timestamp for s in http_samples)
    tail = max(s.end for s in samples) + 1
    values = list(read_csv(folder / "backend-resources.csv"))
    resources = [
        (int(datetime.fromisoformat(r["utc"][:26] + "+00:00").timestamp() * 1000),
         float(r["cpu_percent"]), float(r["working_set_mib"]), float(r["private_mib"]))
        for r in values
    ]
    during_run = [r for r in resources if head <= r[0] < tail]
    result = window(samples, head, tail)
    result.update({
        "pid": run["backend_pid"], "start_utc": run["start_utc"],
        "resource": {"samples": len(during_run), "peak_cpu": max(r[1] for r in during_run),
                     "peak_ws": max(r[2] for r in during_run), "peak_private": max(r[3] for r in during_run)}
        if during_run else None,
    })

    stages = defaultdict(list)
    for s in samples:
        stages[s.stage].append(s)
    if scenario == "Stress":
        result["stages"] = {}
        for stage, default_users, default_ramp, default_duration, _, _ in SCENARIOS[scenario][2]:
            group = stages[stage]
            if not group:
                continue
            start = min(s.event for s in group)
            ramp = int(props.get(stage + "_ramp", default_ramp))
            duration = int(props.get(stage + "_seconds", default_duration))
            end = min(start + (duration - 2) * 1000, max(s.event for s in group))
            rec = window(group, start + (ramp + 1) * 1000, end)
            rec["users"] = int(props.get(stage + "_users", default_users))
            rec["stable"] = stable(rec)
            result["stages"][stage] = rec
    if scenario == "Spike" and stages["burst"]:
        burst = stages["burst"]
        start = min(s.event for s in burst)
        end = max(s.end for s in burst) + 1
        result["stages"] = {
            "pre": window(samples, head + 5000, start),
            "burst": window(samples, start, end),
            "recovery": window(samples, end, tail),
        }
    if scenario == "Soak":
        ramp = int(props.get("endurance_ramp", 8))
        duration = int(props.get("endurance_seconds", 630))
        start = head + (ramp + 1) * 1000
        end = min(head + (duration - 1) * 1000, tail)
        minutes = [window(samples, left, left + 60000)
                   for left in range(start, end - 59999, 60000)]
        result["users"] = int(props.get("endurance_users", 8))
        result["minutes"] = minutes
        rates = [r["http"]["rps"] for r in minutes if r["http"]]
        result["median_rps"] = median(rates) if rates else None
        start_mem = [r[3] for r in resources if start <= r[0] < start + 60000]
        last_end = minutes[-1]["end_ms"] if minutes else start
        end_mem = [r[3] for r in resources if last_end - 60000 <= r[0] < last_end]
        growth = round(median(end_mem) - median(start_mem), 2) if start_mem and end_mem else None
        result["private_growth"] = growth
        result["stable"] = (
            len(minutes) >= 10 and all(stable(m) for m in minutes)
            and result["median_rps"] is not None
            and all(abs(r - result["median_rps"]) <= SOAK_RPS_VARIATION * result["median_rps"] for r in rates)
            and growth is not None and growth <= SOAK_MEMORY_GROWTH_MIB
        )
    return result


def choose_candidate(stress):
    stages = [record for record in stress["stages"].values() if record["stable"]]
    if not stages:
        return None
    return max(stages, key=lambda m: (m["http"]["rps"], m["users"]))["users"]


def table_row(name, r):
    http, flow = r["http"], r["flow"]
    h = f'{http["n"]} | {http["rps"]} | {http["p95"]} | {http["error_pct"]}' if http else "— | — | — | —"
    f = f'{flow["n"]} | {flow["p95"]} | {flow["error_pct"]}' if flow else "— | — | —"
    return f'| {name} | {max(0, r["seconds"]):.2f} | {h} | {f} |'


def report(all_runs):
    lines = [
        "# Kết quả đo HW05",
        "",
        "Tính từ CSV JTL. RPS chỉ đếm mẫu HTTP. p95 dùng nearest rank trên cột `elapsed` (ms), "
        "và lỗi dựa trên `success=false`. Bảng pha loại khoảng ramp như mô tả trong báo cáo.",
        "",
        "| Kịch bản hoặc pha | Giây | HTTP | HTTP RPS | HTTP p95 ms | HTTP lỗi % | Flow | Flow p95 ms | Flow lỗi % |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for scenario, result in all_runs.items():
        lines.append(table_row(scenario, result))
        for phase, m in result.get("stages", {}).items():
            name = f'{phase} ({m["users"]} VU)' if "users" in m else phase
            lines.append(table_row(name, m))
        if scenario == "Soak":
            for minute, m in enumerate(result["minutes"], 1):
                lines.append(table_row(f"Soak phút {minute}", m))
    lines.extend(["", "| Kịch bản | PID | Mẫu tài nguyên | CPU đỉnh % | WS đỉnh MiB | Private đỉnh MiB |",
                  "| --- | ---: | ---: | ---: | ---: | ---: |"])
    for scenario, m in all_runs.items():
        r = m["resource"]
        values = f'{r["samples"]} | {r["peak_cpu"]} | {r["peak_ws"]} | {r["peak_private"]}' if r else "0 | — | — | —"
        lines.append(f'| {scenario} | {m["pid"]} | {values} |')
    soak = all_runs["Soak"]
    candidate = choose_candidate(all_runs["Stress"])
    lines.extend(["", "## Ngưỡng duy trì quan sát được", "",
                  f'- Stress chọn **{candidate if candidate else "chưa có mức đạt"}** VU.',
                  f'- Soak chạy **{soak["users"]} VU** với **{len(soak["minutes"])}** cửa sổ 60 giây, '
                  f'RPS trung vị **{soak["median_rps"]}**, thay đổi private MiB **{soak["private_growth"]}**.',
                  f'- Tiêu chí duy trì **{"Đạt" if soak["stable"] else "Chưa đạt"}**.',
                  "- Kết quả chỉ áp dụng cho cấu hình, dữ liệu và các mức VU đã thử.", ""])
    for scenario, result in all_runs.items():
        lines.extend([f"## {scenario} theo endpoint", "",
                      "| Mẫu HTTP | Số mẫu | p95 ms | Max ms | Lỗi % | Dòng JTL của max |",
                      "| --- | ---: | ---: | ---: | ---: | ---: |"])
        for label, m in result["endpoints"].items():
            lines.append(f'| `{label}` | {m["n"]} | {m["p95"]} | {m["max"]} | {m["error_pct"]} | {m["max_line"]} |')
        for kind in ("http", "flow"):
            m = result[kind]
            if m and m["failures"]:
                detail = ", ".join(f"{code}: {count}" for code, count in sorted(m["failures"].items()))
                lines.extend(["", f"- Mẫu {kind} lỗi theo `responseCode` — {detail}."])
        lines.append("")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=ROOT / "results")
    parser.add_argument("--candidate", action="store_true")
    args = parser.parse_args()
    if args.candidate:
        print(choose_candidate(read_run("Stress", args.results)) or 0)
        return
    all_runs = {scenario: read_run(scenario, args.results) for scenario in ("Load", "Stress", "Spike", "Soak")}
    (ROOT / "measurements.md").write_text(report(all_runs), encoding="utf-8")
    (ROOT / "measurements.json").write_text(json.dumps(all_runs, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("measurements.md and measurements.json written.")


if __name__ == "__main__":
    main()

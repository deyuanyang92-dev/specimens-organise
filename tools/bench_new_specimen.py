"""S3 性能基线脚本：测 next_voucher / create_specimen / get_specimen 在大工作区下的开销。

跑法：
    cd specimens-organise
    python tools/bench_new_specimen.py [n_seed=500] [n_runs=100]

输出三组数据（毫秒，分别为 mean/median/max）：
    1. next_voucher 单次（验证 S3.1 voucher index + S3.2 max_serial 自维护）
    2. create_specimen 单次（验证 S3.3 增量 append）
    3. get_specimen O(1) 索引查询（验证 S3.1 _find_one）

n_seed = 启动前先预填多少行（模拟大工作区现状）；n_runs = 每项测试次数。
"""
from __future__ import annotations

import statistics
import sys
import tempfile
import time
from pathlib import Path

# 让脚本可直接 python tools/bench_new_specimen.py 运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from specimen_app.excel_store import ExcelStore  # noqa: E402


def _ms(elapsed: float) -> float:
    return round(elapsed * 1000.0, 2)


def main(n_seed: int = 500, n_runs: int = 100) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        print(f"工作区：{tmp_path}")
        print(f"预填 {n_seed} 行 specimen 行...")
        store = ExcelStore(tmp_path)
        seeded = []
        for _ in range(n_seed):
            seeded.append(store.create_specimen())
        print(f"已预填 {len(seeded)} 行；末尾 voucher = {seeded[-1]}")

        # 1. next_voucher 单次开销
        print(f"\n[bench] next_voucher × {n_runs}")
        ts: list[float] = []
        for _ in range(n_runs):
            t0 = time.perf_counter()
            _ = store.next_voucher()
            ts.append(time.perf_counter() - t0)
        print(f"  mean={_ms(statistics.mean(ts))}ms  "
              f"median={_ms(statistics.median(ts))}ms  "
              f"max={_ms(max(ts))}ms")

        # 2. create_specimen 单次开销（含 _append_row_incremental + _append_index + _ensure_summary_row + _record_action）
        print(f"\n[bench] create_specimen × {n_runs}")
        ts = []
        for _ in range(n_runs):
            t0 = time.perf_counter()
            _ = store.create_specimen()
            ts.append(time.perf_counter() - t0)
        print(f"  mean={_ms(statistics.mean(ts))}ms  "
              f"median={_ms(statistics.median(ts))}ms  "
              f"max={_ms(max(ts))}ms")

        # 3. get_specimen O(1) 索引查询
        print(f"\n[bench] get_specimen × {n_runs}  (随机已存在 voucher)")
        import random
        sample = random.sample(seeded, min(n_runs, len(seeded)))
        ts = []
        for v in sample:
            t0 = time.perf_counter()
            _ = store.get_specimen(v)
            ts.append(time.perf_counter() - t0)
        print(f"  mean={_ms(statistics.mean(ts))}ms  "
              f"median={_ms(statistics.median(ts))}ms  "
              f"max={_ms(max(ts))}ms")

        store.close()


if __name__ == "__main__":
    n_seed = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    n_runs = int(sys.argv[2]) if len(sys.argv) > 2 else 100
    main(n_seed=n_seed, n_runs=n_runs)

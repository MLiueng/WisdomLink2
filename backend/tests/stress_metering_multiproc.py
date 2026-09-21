"""A-1 复核：多进程（多 worker）计量聚合并发压测。

验证目标（A-1 修复，2026-09-19）：多进程并发聚合写入下，
「UPDATE 原子增量先行 + rowcount=0 回落插入」不丢增量——
旧「先 SELECT 再 UPDATE/INSERT」存在读改写窗口，两个进程同时读到旧值
各自累加会互相覆盖。

阶段设计（每阶段独立清空隔离库，判定口径为精确相等）：
  P0 基线   1 进程 × 200（=200） —— 证明记账口径本身无损
  P1 并发   2 进程 × 300（=600） —— 模拟 uvicorn workers=2
  P2 并发   4 进程 × 250（=1000）—— 放大竞争窗口
  P3 对照   4 进程 × 250 用旧口径（SELECT 后读改写）—— 证明压测具备判别力
            （预期丢增量或聚合行分裂；若未触发说明并发窗口未被覆盖，仅告警）

数据隔离：data/stress_metering.db（不碰主库）；聚合键固定为
(model=stress-model, kb=1, purpose=chat)，全部进程写同一聚合行。

运行：.venv/Scripts/python tests/stress_metering_multiproc.py
"""
import json
import multiprocessing as mp
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
# Windows spawn 子进程重新执行模块级代码：隔离库环境变量必须在模块级设置，
# 否则子进程会落到 .env 默认库（污染生产数据）
os.environ["WL2_DB_URL"] = f"sqlite:///{(ROOT / 'data' / 'stress_metering.db').as_posix()}"

DB_PATH = ROOT / "data" / "stress_metering.db"
MODEL_ID = "stress-model"
PROVIDER = "stress"
KB_ID = 1
PURPOSE = "chat"
PT, CT, CACHED = 100, 50, 10     # 每条记录固定 token 数（便于精确核对）
BATCH = 50                        # 与生产批量阈值一致


def _clean_db() -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(str(DB_PATH) + suffix).unlink(missing_ok=True)


def _init_schema() -> None:
    from app.db import Base, engine
    import app.models  # noqa: F401  注册全部模型
    Base.metadata.create_all(engine)
    engine.dispose()   # Windows：释放连接池句柄，否则阶段切换删库被占用


def _dispose() -> None:
    """子进程退出前释放连接（Windows 文件句柄纪律）。"""
    try:
        from app.db import engine
        engine.dispose()
    except Exception:
        pass


def worker(n_records: int, barrier) -> None:
    """新口径（A-1 修复后）：record_usage 入队 + 每 50 条同步刷盘。

    关键确定性处理：把 `_FLUSH_BATCH` 调到极大，禁用 `_enqueue` 的自动刷盘
    守护线程——否则守护线程与测试同步 `flush_now()` 抢锁，进程退出时守护线程
    未提交完即被杀，产生与 A-1 无关的假丢失。刷盘全部由本函数同步驱动。
    """
    from app.core import metering
    from app.core.metering import record_usage, flush_now
    metering._FLUSH_BATCH = 10 ** 9   # 禁用自动刷盘线程（测试确定性）
    barrier.wait()   # 多进程同时开跑，最大化并发重叠
    for i in range(n_records):
        record_usage(MODEL_ID, PROVIDER, PURPOSE, KB_ID, PT, CT,
                     is_estimated=False, cached_tokens=CACHED)
        if (i + 1) % BATCH == 0:
            flush_now()
    flush_now()
    _dispose()


def worker_legacy(n_records: int, barrier) -> None:
    """旧口径对照：批内聚合后「SELECT → 读改写 UPDATE / INSERT」。

    复刻 A-1 修复前的竞态形态：SELECT 与写回之间存在窗口，
    多进程交错时后写覆盖先写 → 丢增量。
    """
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models import ModelUsage
    from datetime import date

    def _write(batch_calls: int) -> None:
        db = SessionLocal()
        try:
            # 旧口径无唯一约束兜底：竞态下并发 INSERT 会产生重复聚合行，
            # 此处取第一行（.first()）继续读改写，不崩溃以量化丢失总量
            row = db.scalars(select(ModelUsage).where(
                ModelUsage.stat_date == date.today(),
                ModelUsage.model_id == MODEL_ID,
                ModelUsage.kb_id == KB_ID,
                ModelUsage.purpose == PURPOSE)).first()
            time.sleep(0.002)   # 放大读改写窗口（生产中窗口即事务间隙）
            if row:
                row.prompt_tokens += PT * batch_calls
                row.completion_tokens += CT * batch_calls
                row.call_count += batch_calls
                row.cached_tokens += CACHED * batch_calls
            else:
                db.add(ModelUsage(stat_date=date.today(), model_id=MODEL_ID,
                                  provider=PROVIDER, kb_id=KB_ID, purpose=PURPOSE,
                                  prompt_tokens=PT * batch_calls,
                                  completion_tokens=CT * batch_calls,
                                  call_count=batch_calls, cached_tokens=CACHED * batch_calls))
            db.commit()
        finally:
            db.close()

    barrier.wait()
    for i in range(n_records // BATCH):
        _write(BATCH)
    _dispose()


def verify(expected_calls: int) -> dict:
    """核对 model_usage 聚合总和 == 预期（精确相等）；返回判定明细。"""
    con = sqlite3.connect(DB_PATH, timeout=15)
    try:
        rows, calls, pt, ct, cached = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(call_count),0), COALESCE(SUM(prompt_tokens),0),"
            " COALESCE(SUM(completion_tokens),0), COALESCE(SUM(cached_tokens),0)"
            " FROM model_usage WHERE model_id=? AND kb_id=? AND purpose=?",
            (MODEL_ID, KB_ID, PURPOSE)).fetchone()
    finally:
        con.close()
    exp_pt, exp_ct, exp_cached = expected_calls * PT, expected_calls * CT, expected_calls * CACHED
    return {
        "expected_calls": expected_calls, "rows": rows,
        "call_count": calls, "prompt_tokens": pt,
        "completion_tokens": ct, "cached_tokens": cached,
        "lost_calls": expected_calls - calls,
        "pass": calls == expected_calls and pt == exp_pt and ct == exp_ct and cached == exp_cached,
    }


def run_phase(name: str, n_proc: int, per_proc: int, legacy: bool = False) -> dict:
    _clean_db()
    _init_schema()
    target = worker_legacy if legacy else worker
    barrier = mp.Barrier(n_proc)
    procs = [mp.Process(target=target, args=(per_proc, barrier)) for _ in range(n_proc)]
    t0 = time.time()
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)
    elapsed = round(time.time() - t0, 2)
    assert all(p.exitcode == 0 for p in procs), f"{name} 子进程异常退出: {[p.exitcode for p in procs]}"
    res = verify(n_proc * per_proc)
    res.update({"phase": name, "processes": n_proc, "per_process": per_proc,
                "legacy": legacy, "elapsed_s": elapsed})
    return res


def main() -> None:
    results = [
        run_phase("P0 基线（单进程）", 1, 200),
        run_phase("P1 并发 2 进程", 2, 300),
        run_phase("P2 并发 4 进程", 4, 250),
        run_phase("P3 旧口径对照 4 进程", 4, 250, legacy=True),
    ]

    print("\n===== A-1 多 worker 计量聚合压测 =====")
    for r in results:
        tag = "PASS" if r["pass"] else "FAIL"
        if r["legacy"]:
            # 对照组预期丢增量：丢了=压测有判别力（修复有效的前提成立）；没丢=窗口未触发（告警）
            tag = "RACE-TRIGGERED" if not r["pass"] else "RACE-NOT-TRIGGERED(告警)"
        print(f"  [{tag}] {r['phase']} | 期望 call_count={r['expected_calls']}"
              f" 实际={r['call_count']} 丢失={r['lost_calls']}"
              f" | tokens p/c/cached={r['prompt_tokens']}/{r['completion_tokens']}/{r['cached_tokens']}"
              f" | 聚合行数={r['rows']} | {r['elapsed_s']}s")
    print("======================================")

    out = ROOT / "data" / "stress_metering_report.json"
    out.write_text(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                               "model_id": MODEL_ID, "per_record": {"pt": PT, "ct": CT, "cached": CACHED},
                               "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告已归档: {out}")

    core = [r for r in results if not r["legacy"]]
    legacy = next(r for r in results if r["legacy"])
    assert all(r["pass"] for r in core), "新口径多进程聚合存在丢增量，A-1 修复未通过复核！"
    if legacy["pass"]:
        print("告警：旧口径对照未触发竞态（本次压测判别力不足），建议加大进程数/批量重试。")
    else:
        print(f"对照组证实：旧口径在同等并发下丢失 {legacy['lost_calls']} 次调用"
              f"（聚合行数={legacy['rows']}）→ A-1 原子增量修复有效。")
    print("A1_STRESS_PASS")


if __name__ == "__main__":
    main()

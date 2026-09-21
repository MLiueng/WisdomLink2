"""规则单测（BR-006/007/011 可判定样例 = 需求文档 §4.5 的标准答案）。"""
from app.core.rules import (next_version, normalize_question, qa_pass_thresholds, rrf_fuse)


def test_br006_version_minor():
    assert next_version("v1.0", major=False) == "v1.1"


def test_br006_version_major():
    assert next_version("v1.1", major=True) == "v2.1"


def test_br007_rrf_sample():
    # 正例：稠密第2、稀疏第1 → 1/62 + 1/61 ≈ 0.0325
    fused = rrf_fuse([["a", "c"], ["c", "b"]], k=60)
    scores = dict(fused)
    assert abs(scores["c"] - (1 / 62 + 1 / 61)) < 1e-9


def test_br007_rrf_tiebreak_stable():
    # 并列分按 chunk_id 升序稳定排序
    fused = rrf_fuse([["b", "a"], ["a", "b"]], k=60)
    assert fused[0][0] == "a" and fused[1][0] == "b"


def test_br011_normalize_exact():
    # 正例-精确："如何申请年假？" 归一化后与 "如何申请年假" 全等
    assert normalize_question("如何申请年假？") == normalize_question("如何申请年假")
    assert normalize_question("ＷｉＦｉ 密码？") == normalize_question("wifi 密码")


def test_br011_threshold_pass():
    assert qa_pass_thresholds([0.95, 0.80], 0.92, 0.02) is True


def test_br011_threshold_fail_low():
    assert qa_pass_thresholds([0.85, 0.80], 0.92, 0.02) is False


def test_br011_threshold_fail_margin():
    # 相似 0.93 与次高 0.92 分差不足 → 不命中（防边界抖动）
    assert qa_pass_thresholds([0.93, 0.92], 0.92, 0.02) is False

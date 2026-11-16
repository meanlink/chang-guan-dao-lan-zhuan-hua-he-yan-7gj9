"""2×2 列联表统计（仅标准库）。

与主项目口径保持一致：Pearson χ² 不使用连续性校正；
自由度为 1 时生存函数可由余误差函数精确给出：p = erfc(sqrt(χ²/2))。
"""
from __future__ import annotations

import math
from typing import List


def expected(observed: List[List[int]]) -> List[List[float]]:
    rows = [sum(r) for r in observed]
    cols = [sum(observed[r][c] for r in range(len(observed)))
            for c in range(len(observed[0]))]
    total = sum(rows)
    return [[rows[r] * cols[c] / total for c in range(len(cols))]
            for r in range(len(rows))]


def chi_square(observed: List[List[int]]) -> dict:
    exp = expected(observed)
    total = sum(sum(r) for r in observed)
    if total == 0 or any(e == 0 for row in exp for e in row):
        raise ValueError("样本退化：存在零合计，无法估计 χ²")
    chi2 = sum((observed[r][c] - exp[r][c]) ** 2 / exp[r][c]
               for r in range(len(observed)) for c in range(len(observed[0])))
    df = (len(observed) - 1) * (len(observed[0]) - 1)
    if df != 1:
        raise ValueError("本试点仅支持 2×2 表（df=1）")
    p_value = math.erfc(math.sqrt(chi2 / 2.0))
    cramers_v = math.sqrt(chi2 / total)
    return {
        "chi2": round(chi2, 6),
        "df": df,
        "p_value": round(p_value, 6),
        "cramers_v": round(cramers_v, 6),
        "expected": [[round(e, 4) for e in row] for row in exp],
        "min_expected": round(min(e for row in exp for e in row), 4),
        "expected_warning": min(e for row in exp for e in row) < 5,
    }

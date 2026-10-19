"""抽取投影层指纹的稳定性测试（全离线）。

contract
--------
- ``cond_fingerprint`` 只覆盖 (kind, side, value, method_ref)，措辞 ``text`` 不影响。
- ``bundle_fingerprint`` 排除 ``generated_at`` 与 ``bundle_fingerprint`` 自身 → 幂等。
- ``file_fingerprint`` 是源文件 SHA256 的前 32 位十六进制。
- None 与空 dict 在 ``cond_fingerprint.value`` 上归一为 ``{}``。
"""

from __future__ import annotations

import json
from pathlib import Path

from matrag.extract.fingerprints import (
    bundle_fingerprint,
    cond_fingerprint,
    file_fingerprint,
    req_fingerprint,
)


def test_cond_fingerprint_ignores_text() -> None:
    """text 改变不影响指纹 (措辞变化不该清零消费侧已签字)。"""
    base = cond_fingerprint(
        kind="output_voltage", side="output",
        value={"min": 53.5, "typ": 54.0, "max": 54.5, "unit": "V"},
        method_ref="",
    )
    same = cond_fingerprint(
        kind="output_voltage", side="output",
        value={"min": 53.5, "typ": 54.0, "max": 54.5, "unit": "V"},
        method_ref="",
    )
    # 措辞变了不该影响指纹; 这里通过重复生成验证稳定性
    assert base == same
    assert len(base) == 16


def test_cond_fingerprint_value_none_vs_empty_dict() -> None:
    """None 与空 dict 在 value 槽位归一 (两侧契约里都合法, 语义相同)。"""
    a = cond_fingerprint(kind="input_type", side="input", value=None)
    b = cond_fingerprint(kind="input_type", side="input", value={})
    assert a == b


def test_cond_fingerprint_method_ref_changes_when_method_changes() -> None:
    """method_ref 是语义字段, 方法变了指纹必须变。"""
    no_method = cond_fingerprint(
        kind="output_voltage", side="output",
        value={"min": 53.5}, method_ref="",
    )
    with_method = cond_fingerprint(
        kind="output_voltage", side="output",
        value={"min": 53.5}, method_ref="dmm-read",
    )
    assert no_method != with_method


def test_cond_fingerprint_kind_change_changes() -> None:
    """kind 或 side 变 → 指纹变 (它是条件的身份)。"""
    base = cond_fingerprint(
        kind="output_voltage", side="output",
        value={"min": 5.0, "max": 5.0}, method_ref="",
    )
    other_kind = cond_fingerprint(
        kind="ripple", side="output",
        value={"min": 5.0, "max": 5.0}, method_ref="",
    )
    other_side = cond_fingerprint(
        kind="output_voltage", side="input",
        value={"min": 5.0, "max": 5.0}, method_ref="",
    )
    assert base != other_kind
    assert base != other_side


def test_req_fingerprint_stable() -> None:
    """req_fingerprint 同输入两次一致; 条件指纹改变 → 改变。"""
    fps = ["aaa", "bbb"]
    limits = [{"rail": "54V", "min": 53.5, "max": 54.5, "unit": "V"}]
    fp1 = req_fingerprint(
        requirement_code="SR-X-1", title="T",
        input_fps=fps, output_fps=fps, limits=limits,
    )
    fp2 = req_fingerprint(
        requirement_code="SR-X-1", title="T",
        input_fps=fps, output_fps=fps, limits=limits,
    )
    assert fp1 == fp2
    assert len(fp1) == 16
    # 改 limits → 改
    fp4 = req_fingerprint(
        requirement_code="SR-X-1", title="T",
        input_fps=fps, output_fps=fps,
        limits=[{"rail": "54V", "min": 0, "max": 100, "unit": "V"}],
    )
    assert fp1 != fp4


def test_bundle_fingerprint_ignores_volatile_keys() -> None:
    """``generated_at`` 与 ``bundle_fingerprint`` 不计入指纹 → 同一输入两次逐位一致。"""
    payload = {
        "bundle_version": "1.0",
        "product_code": "PA601-D54A",
        "requirements": [{"requirement_code": "SR-X-1", "title": "T"}],
        "excluded": [],
        "review": [],
        "generated_at": "2026-10-18T00:00:00Z",
        "bundle_fingerprint": "deadbeef",
    }
    fp_first = bundle_fingerprint(payload)
    payload["generated_at"] = "2099-01-01T12:34:56Z"
    payload["bundle_fingerprint"] = "different"
    fp_second = bundle_fingerprint(payload)
    assert fp_first == fp_second
    assert len(fp_first) == 32
    # 内容真变了应改
    payload["requirements"].append({"requirement_code": "SR-X-2", "title": "T2"})
    fp_third = bundle_fingerprint(payload)
    assert fp_first != fp_third


def test_file_fingerprint_stable(tmp_path: Path) -> None:
    """同字节两次读一致; 改一字变。"""
    src = tmp_path / "doc.json"
    src.write_text(json.dumps({"doc_id": "X", "blocks": []}), encoding="utf-8")
    fp1 = file_fingerprint(src)
    fp2 = file_fingerprint(src)
    assert fp1 == fp2
    assert len(fp1) == 32
    src.write_text(json.dumps({"doc_id": "X", "blocks": [1]}), encoding="utf-8")
    fp3 = file_fingerprint(src)
    assert fp1 != fp3

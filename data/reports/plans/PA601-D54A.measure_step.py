"""由 ATERag 流程规划器生成 —— 草稿, 上机前须补齐 FILL_IN 标记。

动作      : measure_dc_voltage
条件种类  : output_voltage
来源      : SR-PA601-D54A-1201 / 场景 24 / -54V 额定输出（半载）
仪表资源  : DMM_1   (须与夹具拓扑一致)
量纲      : V
判据      : -53.2 ~ -54.8

不要手改本文件 —— 改 config/test_method_bindings.yaml 后重新生成。
手改的副本会在下次生成时被覆盖, 且与绑定表失去同步。

执行协议 (据 ate_platform/executor/step_executor.py):
  * 正常返回              -> PASSED
  * raise AssertionError  -> FAILED
  * 其它异常              -> ERROR
注意这个协议**没有** SKIP 通道: 想"不判定"只能不抛异常, 而那等于 PASSED。
所以缺判据时本脚本抛 AssertionError(FAILED), 而不是安静跳过。
"""

#: 本步骤占用的仪表资源, 与 test_method_bindings.yaml 一致。
RESOURCES = ['DMM_1']

#: 规格书抽取出的判据。单边限值是常态(过压只有上限), 故 min/max 均可为 None,
#: 含义是"这一侧不要求"而非"忘了填"。
LIMITS = {'min': -54.8, 'typ': -54.0, 'max': -53.2}

#: 判据来源, 便于从不合格品记录反查规格书条款。
ORIGIN = 'SR-PA601-D54A-1201 / 场景 24 / -54V 额定输出（半载）'

_setpoint = {'min': -53.2, 'typ': -54.0, 'max': -54.8, 'unit': 'V', 'rail': '-54V',
             'scenario': {'load': '半载'}, 'method_ref': 'en300132_steady_state'}


def _driver():
    """仿真接线 —— Phase A.4 决策 D2：物理仪器未连接，返回仪器仿真器支撑的 DMM。

    真实站点此处应 ``DriverRegistry.get_driver("dmm")`` 取驱动类并 connect；
    仿真模式下改为实例化 ``ate_platform.simulation.InstrumentSimulator``，
    测量真值取脚本 SETPOINT 的 typ（缺省取 min/max 中点），再叠加噪声模型。
    """
    from ate_platform.drivers.base_hal import BaseDriver
    from ate_platform.simulation.instrument_simulator import (
        InstrumentSimulator,
        NoiseConfig,
        NoiseModel,
    )

    class _SimHAL(BaseDriver):
        """SIM 模式 HAL：不打开真实 VISA 资源。"""

        def __init__(self):
            self._mode = "SIM"
            self._instrument = None
            self._resource_manager = None
            self._address = ""

        def connect(self, address):  # noqa: D102
            self._address = address

        def disconnect(self):  # noqa: D102
            self._address = ""

        def query(self, command, delay=None):  # noqa: D102
            return "1.0"

    _sim = InstrumentSimulator(
        _SimHAL(),
        instrument_type="DMM",
        config=NoiseConfig(model=NoiseModel.FULL, noise_sigma=0.01, seed=42),
    )

    class _SimulatedDMM:
        """仿真 DMM：measure_dc_voltage 返回带噪读数。"""

        def measure_dc_voltage(self, setpoint=None):
            """按 setpoint 真值生成一次仿真测量。"""
            sp = setpoint or {}
            true_value = sp.get("typ")
            if true_value is None:
                vals = [v for v in (sp.get("min"), sp.get("max")) if isinstance(v, (int, float))]
                true_value = sum(vals) / len(vals) if vals else 0.0
            return _sim.simulate_measurement(float(true_value))

    return _SimulatedDMM()



def _judge(measured):
    """按 LIMITS 判定。返回即通过, 不通过抛 AssertionError -> FAILED。

    global 声明不可省: result_* 必须在模块命名空间里, executor 从那里读
    输出。少了它, 判定结果就传不回上层。

    两侧皆空 -> 抛错而非放过: 规格书没给判据的测量, 放过等于每台都报通过。
    读数为 None 同样抛错: 仪表没读到东西不等于产品合格。

    result_cond_fingerprint: 本步读数归属的条件指纹, 与 measured_value 一同
    落盘 —— 审计报告据此把读数对回条款。仅在正常返回(PASSED)时赋值: 抛错路径
    executor 不收集任何 outputs, 半截指纹没有被归因价值。
    """
    global result_measured_value, result_passed, result_cond_fingerprint

    lo, hi = LIMITS.get("min"), LIMITS.get("max")
    if lo is None and hi is None:
        raise AssertionError(
            f"无条件判据, 无法判定: {ORIGIN} / {LIMITS}。"
            f" 该测量被排入序列但没有上下限 —— 请补判据或改为定性确认, "
            f"不得默认通过。"
        )
    if measured is None:
        raise AssertionError(f"仪表未返回读数, 无法判定: {ORIGIN}")

    out_of_range = (lo is not None and measured < lo) or (hi is not None and measured > hi)
    if out_of_range:
        raise AssertionError(
            f"实测 {measured}{'V'} 超出 [{lo}, {hi}]: {ORIGIN}"
        )

    result_measured_value = measured
    result_passed = True
    result_cond_fingerprint = ''
    return True

# ── 执行体在模块级, 不在函数内 ──────────────────────────────────────────
# executor 收集的是 exec() 的模块命名空间里的 result_* 变量。若把执行体放进
# 函数, 那些变量是函数局部变量, 永远不会出现在命名空间里, 于是每一步都会
# 返回空 outputs —— 不报错, 只是结果全丢。这一点只有真跑一遍才会发现。
_drv = _driver()
result_measured = _drv.measure_dc_voltage(setpoint=_setpoint)
_judge(result_measured)

if __name__ == "__main__":
    pass

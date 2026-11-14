"""领域异常：预检不通过时抛出，不静默丢弃任何原子。"""


class UnsupportedFixtureError(ValueError):
    """文件可解析，但包含本任务声明范围之外的结构（芳香键等）。"""


class PrecheckError(ValueError):
    """元素/同位素计数不守恒、原子数超限等硬性预检失败。"""

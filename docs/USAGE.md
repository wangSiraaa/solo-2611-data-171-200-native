# 小型扩散张量拟合（dtifit）使用说明

纯 Python 3.11 + NumPy + nibabel 实现，对应 `README.md` 的固定线性模型。
不包含运动矫正、非线性拟合、纤维追踪、脑区分割或临床解释。

## 1. 环境与运行

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 拟合 plan.json 中的全部案例
.venv/bin/python run_fit.py --plan fixtures/plan.json \
                           --fixtures fixtures --out outputs

# 独立验证（从写回的 NIfTI 重新读入并重算，不调用拟合内部）
.venv/bin/python verify_outputs.py --plan fixtures/plan.json \
                                  --fixtures fixtures --out outputs

# 合成小例与拒绝路径测试（22 项，也可 pytest）
.venv/bin/python tests/test_synthetic.py
.venv/bin/python -m pytest tests/ -q
```

## 2. 线性模型与方向口径

信号模型（世界 RAS 坐标，对称张量 D）：

```
S(b, g) = S0 · exp(-b · gᵀ D g)
y = -ln(S / S0) / b = X · d
X 行 = [gx², gy², gz², 2·gx·gy, 2·gx·gz, 2·gy·gz]
d    = [Dxx, Dyy, Dzz, Dxy, Dxz, Dyz]      （世界 RAS，mm²/s）
```

梯度表声明 `gradient_frame = voxel_axes`，即方向写在**图像轴基底**上。
世界方向只允许用去掉 spacing 的正交 affine 列变换：

```
A = affine[:3, :3]；列范数 = 体素 spacing；R = A / 列范数；g_world = R · g_input
```

- voxel spacing 只用于识别列范数，**绝不乘进方向长度**；
- R 是正交矩阵，允许 `det R = -1`（轴交换/反手基底），单位长度与手性被正确保留；
- sform 为权威；qform 在 sform 无剪切时必须逐元素一致（容差 1e-5）；
- 归一化列非正交（剪切）、sform/qform 冲突、空间单位非 mm、非单位梯度、
  梯度行与第四维不能一一对应等情况直接抛出 `FiducialError`/`GradientError`。

## 3. 逐体素有效性处理（不以整表满秩代替体素判断）

每个体素独立执行，被排除的测量只影响该体素：

1. b0（b ≤ 50）：仅正且有限的信号参与，S0 = 有效 b0 均值；少于 2 个 →
   `excluded_insufficient_valid_b0`；
2. 扩散方向：非正/非有限测量不能取 log，逐 volume 记录排除原因；
3. 剩余行数 < 6 → `excluded_insufficient_diffusion_rows`；
4. 对**该体素实际保留的设计矩阵**做 SVD：秩 < 6 →
   `excluded_design_rank_deficient`；条件数 > 1e6 →
   `excluded_design_condition_too_large`；
5. 通过后用普通最小二乘（`numpy.linalg.lstsq`）拟合，记录残差平方和与条件数。

诊断中保留每个体素的有效 b0 数、有效方向数、S0、条件数、RSS、
被排除 volume 及原因、状态字符串；数据支持不足的体素不产生伪结果。

## 4. 指标、非物理标记与主方向

- 特征值来自对称张量 `eigh`（升序）；
- `MD = mean(λ)`，
  `FA = sqrt(1.5 · Σ(λ−MD)² / Σλ²)`；
- 特征值 `< -1e-7 mm²/s` → 标记 `fitted_unphysical_negative_eigenvalue`，
  **不静默修正为正定**，FA/MD 留缺失；落在 `[-1e-7, 0)` 的仅按声明容差置零并记录；
- 三个特征值全为 0 时 FA 是 0/0，属未定义（缺失 mask），与真正各向同性
  体素的 `FA = 0`（仍是有效数值）严格区分；
- 主、次特征值相对间隔 `< 0.02`（含各向同性/近并列）时不给出虚假的唯一
  主方向，方向写 NaN 并以独立 mask 标识；
- 有效主方向以世界 RAS 分量输出，符号约定为：最大绝对分量为正。

## 5. 交付产品（每个案例一个目录，保留各自输入网格）

| 文件 | 含义 |
| --- | --- |
| `tensor.nii.gz` | 6 分量世界 RAS 对称张量，末轴 `[Dxx,Dyy,Dzz,Dxy,Dxz,Dyz]` |
| `fa.nii.gz` / `md.nii.gz` | 标量指标，缺失为 NaN |
| `eigenvalues.nii.gz` / `eigenvectors.nii.gz` | 升序特征值与对应特征向量（列为向量） |
| `principal_direction.nii.gz` | 世界 RAS 主方向；未定处 NaN |
| `s0.nii.gz`、`residual_sum_sq.nii.gz`、`condition_number.nii.gz` | 拟合量 |
| `fitted_mask`、`physical_mask`、`metric_valid_mask`、`principal_direction_defined_mask`、`missing_results_mask` | uint8 独立有效性 mask |
| `n_valid_b0.nii.gz`、`n_valid_directions.nii.gz` | 逐体素有效计数 |
| `voxel_diagnostics.json` / `.csv` | 逐体素诊断、排除原因、世界梯度与 R |

所有图像沿用输入的 affine、shape 与 mm 空间单位；未定义位置 float32 NaN，
并由 mask 无歧义标识。

## 6. 独立验证内容（`verify_outputs.py`）

1. 写回的 NIfTI 实际可读，affine/zooms/mm 单位与源图一致；
2. 从输出张量独立重算特征值、FA、MD；
3. 用张量重建信号，仅按该体素实际保留的方向重算残差并比对 RSS；
4. 两个空间轴不同的案例按**世界坐标体素中心**配对，核对标量与世界张量一致
   （含 `det R = -1` 的反手案例）；
5. NaN 与 mask 一致，各向同性 FA=0 不被当成缺失。

## 7. 固定 12 体素案例的预期状态

每案例：3 体素各向异性可给主方向，6 体素各向同性/近并列（FA≈0 有效但主方向
未定），1 体素有效方向不足，1 体素有效 b0 不足，1 体素拟出显著负特征值
（非物理，不修正）。两个轴交换案例的世界张量逐体素一致。

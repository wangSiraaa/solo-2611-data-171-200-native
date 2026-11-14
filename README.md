# 小型扩散NIfTI与固定张量模型
两个真实4D NIfTI表示同一物理数据，第二份交换空间x/y轴并同步重表达gradient。信号用int16 payload和NIfTI缩放保存，须读取缩放后的物理值。最后一维与gradient.volume一一对应（零基），不能按文件排序猜序号。空间单位mm；sform为权威，要求qform一致到计划容差，非零列范数为spacing，归一化的三个affine列组成正交基R（允许det=-1）。不支持shear。gradient_frame为voxel_axes：世界RAS方向g_world=R*g_input，不可把缩放也乘入方向。

b0为b<=50，其gradient不用作方向；非b0向量应单位长度（容差1e-5）。每体素S0取有效正有限b0信号均值，至少2个。正有限扩散观测构成y=-ln(S/S0)/b，设计行[gx²,gy²,gz²,2gxgy,2gxgz,2gygz]，未知量[Dxx,Dyy,Dzz,Dxy,Dxz,Dyz]在世界RAS坐标。用普通最小二乘；实际有效设计需满秩且条件数在plan范围。被丢的测量及原因、S0和有效方向数必须保留。

对称D的特征值给MD=mean(lambda)，FA=sqrt(1.5*sum((lambda-MD)^2)/sum(lambda^2))。显著负特征值(<-tol)标为非物理，不静默截断；[-tol,0)仅可按声明容差置0并记录。MD/FA不支持时保持缺失mask，不能伪装成FA=0。主特征值与次值相对间隔不足时主方向未定；有效主向量用世界分量，符号按最大绝对分量为正规范。

仅12体素、13个体积。固定简化模型不含运动矫正、非线性拟合、纤维追踪、脑区分割或临床解释。输出NIfTI需保留各输入自己的空间网格，两个案例应按world坐标对应后比较。

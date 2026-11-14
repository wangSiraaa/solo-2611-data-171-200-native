# SEG-Y NMO材料和固定物理约定
真实SEG-Y为big endian、format 5 IEEE float32，每道420样本，二进制及道头dt=2000微秒。CDP(道头字节21–24)、offset(37–40，米，带符号)、TraceIdentificationCode(29–30)、DelayRecordingTime(109–110，毫秒)均写入真实道头。采样间隔道头117–118，样本数115–116。仅code=1为live，其他道报告排除。trace文件顺序不是CDP/offset排序。

采用已给RMS速度的固定近似，不反演速度。对零偏移输出时间t0，线性插值速度v(t0)，源时间tx=sqrt(t0²+(offset/v)²)。道的实际采样时刻为delay_ms/1000+k*dt_s。只在源支持内线性插值，不外推。伸长因子固定用tx/t0-1：t0=0且offset=0取0，t0=0且offset!=0视为无穷；大于stretch_limit切除，等于保留。不做振幅Jacobian补偿。正负offset具有相同NMO时间映射。

同时检查每道在计划网格上的tx严格递增；给定速度表覆盖全部输出时间，若另换表导致折回/非正速度应诊断。无有效来源、dead、stretch mute均不计fold，不能先填零再求平均。stack=sum(valid corrected)/fold，fold=0的stack明确缺失。校正矩阵、有效mask和原因须关联原trace序号、CDP与offset；输出t0就是共同的零偏移时间轴。

材料含非零delay、负offset、dead道和超出支持/伸长切除区。物理模型只是教材级NMO，不承诺用于真实地层解释。输出可使用NPZ/CSV，不必重新实现SEG-Y写入。

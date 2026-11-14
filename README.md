# 真实小BAM到相位材料
reads.bam为coordinate-sorted真实BAM，包含15对primary片段和1条应过滤secondary记录，同名mate不保证相邻。一个染色体，SNV表为一基参考坐标，allele0=A、allele1=G，每个位点均为已知二倍体杂合。reference.fa用于核对REF，不发现新变异。tiny.bam与tiny-variants.csv供极小独立核验；小表之外的位点忽略。

先排除unmapped/secondary/supplementary及MAPQ<20，再按query_name取一对primary read1/read2，身份不完整或重复应诊断。支持M/= /X/I/D/S。BAM SEQ已按比对方向存储，不能因reverse flag再反向互补一次；按CIGAR由参考SNV坐标定位query碱基与质量。I/S不消耗参考，D不提供碱基。非REF/ALT、低于Q20或无覆盖均不是0等位；记录缺观测原因。

片段每位点最多一条证据。只有一端有效用该端Q作权重；两端一致取max(Q1,Q2)，不能按两条read重复计数；冲突且质量差>=10时取高Q等位、权重=质量差，差不足则该片段/位点未定，不参与MEC。这里Q只按给定规则转为整数代价，不另做概率建模。保留两端来源、CIGAR、query索引、原碱基/Q及裁决。

对主相位h，另一条为1-h，片段代价为对两条相位的加权不一致和中较小者；总和为weighted MEC。按实际有效共覆盖建连通块，单点不连接别的位点。组件首位固定h=0消除整体交换。组件内相邻位置的local probe强制其异或与主解相反，再重求最小代价；增量0表示同值替代，仅需一份替代证据，不要求输出全部最优解或概率区间。

材料含reverse mate、插入、软剪切、=/X、删除、低质量未观测、重叠mate一致/冲突、独立块和孤立位点。只生产已给SNV的片段证据与相位，不做read比对、变异calling、indel位点、转录本兼容或全基因组基准。

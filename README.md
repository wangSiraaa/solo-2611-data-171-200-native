# 真实 OSM XML 小路网
道路XML完全合成，不抓取在线OSM。只处理plan的公路类型，保留被过滤way的诊断。每对相邻nd生成物理segment，再按oneway=yes/1/true（正向）、-1（逆向）、no/0/false（双向）展开；无oneway的roundabout默认正向，其他默认双向。无其他access语义，不猜测限制。距离按README中给定球半径的haversine米计算。

连接只认相同node ID。几何交叉、相同坐标不同ID不相连；bridge/layer供来源解释，不能据坐标自动造连接，也不靠layer否认明确同node身份。保留原way_id、segment序号和方向，不能合并相同坐标点。way50横跨另一道路但没有公共node；node13与node3坐标相同但ID不同。

输出turn图以有向segment为状态，转移要求前一边终点=后一边起点。via-node restriction作用于进入via且way=from的状态：no_*排除匹配to的后继；only_*仅允许匹配to后继；多个限制取交集。no_u_turn在from=to时只排除回到刚来节点的反向segment，不排除继续前进。除这种情况，本题from/to为不同way且在via为端点；不支持via-way、conditional、except、复杂中间way及其他类型，必须明确诊断。无需按角度重新猜标签名称。

所有规则应由真实可行的有向进出边组合解释；方向导致relation没有可作用进出组合时应诊断。不得静默忽略不支持限制后宣称得到完整可驾驶图。交付节点/有向边/允许转移及每个被禁止转移的relation证据；不是求路线或匹配GPS。

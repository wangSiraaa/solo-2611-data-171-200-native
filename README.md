# mailarch — 离线 EML 邮件档案整理工具

读取一个 `.eml` 文件目录,输出规范元数据(JSON 清单)、必要资源文件以及静态只读
HTML 索引。纯 Python 3.11 标准库实现,不连接邮箱、不发送邮件、无第三方依赖。

## 用法

```sh
python3 -m mailarch ingest fixtures/eml out/     # 读取目录,导出档案
python3 -m mailarch verify out/                  # 依据导出清单独立核对
python3 -m mailarch verify out/ --src fixtures/eml   # 一并核对来源字节
```

输出结构:

```
out/
  manifest.json        # 规范元数据:消息、part、会话证据、诊断、报告
  index.html           # 会话索引(只读、无脚本)
  threads/<conv>.html  # 会话树(占位节点、环、冲突均显式标注)
  messages/<node>.html # 单封邮件页(正文、资源、附件、诊断)
  assets/<sha16>.<ext> # 导出的资源,内容寻址的稳定身份
```

## 处理规则(与 fixtures/rules.json 对齐)

**MIME 树与正文选择**

- `multipart/alternative`:优先可用的 `text/html`,保留 `text/plain` 替代;
  空 HTML 视为不可用并回退纯文本。
- `multipart/related`:根由 `start` 参数的 Content-ID 决定,缺省取第一个子项。
- `multipart/mixed`:第一个子项承载正文位置,其余为附件。
- `message/rfc822`:递归解析为可单独展示的附件邮件,**不作为顶层会话成员**;
  其内部悬空引用也不产生顶层占位节点。
- CID 绑定仅限所属最近的 `multipart/related` 作用域;嵌套作用域不外泄,
  相同 CID 在其他嵌套范围不会抢占(见 fixtures/eml/11)。
- 每个 part 记录:原字节摘要(传输解码后的 payload)、part 路径、声明类型、
  实际角色(body / alternative / inline / attachment / attached-message)。
- 附件文件名解码后仅作展示;落盘使用 `<sha256前16位>.<扩展名>` 的稳定身份。

**会话构建**

- 父候选:`References` 末项优先;无 References 时取单个 `In-Reply-To`;
  完整 References 链作为证据保留在节点上。
- `In-Reply-To` 与 References 不一致时保留冲突(`irt-conflict` 诊断),不静默丢弃。
- 同 Message-ID:仅原字节完全一致才折叠(记录重复来源文件);内容冲突的版本
  全部保留为冲突变体,指向该 ID 的引用标记为 `ambiguous-parent`,不随意选代表。
- 缺失父邮件生成占位节点;引用环与自引用显式诊断,所有边保留,
  仅在页面渲染时标注 `↩ 引用环` 而不再展开。
- 主题相同但无引用证据的邮件保持独立;主题从不建立父子关系。

**HTML 安全策略**

- 标签/属性白名单;不支持的标记转义为文本展示;script/style/iframe/form 等
  内容整体丢弃;事件属性、`style`、远程 URL、`data:` URL 一律不输出。
- `<img>` 仅允许绑定在所属 related 作用域内的 `cid:` 引用且限于安全位图类型;
  其他绑定资源渲染为下载链接;未绑定 CID 显示缺口标记。
- 页面不含任何 JavaScript,不加载远程资源。

## 独立核对(verify)

`verify` 只依据导出清单与磁盘文件(不重解析邮箱):

- 每个资源文件的字节摘要与清单一致;assets/ 下无多余文件。
- 每个被引用资源(含 CID 绑定)回溯到来源 part 并核对字节摘要。
- 附件邮件的摘要与其来源 message/rfc822 part 一致。
- 会话证据不变量:会话 ID 重算一致、边的端点存在、字节相同节点必已折叠、
  ID 冲突必有诊断、占位节点无来源文件。
- 生成的 HTML 不含 `<script`、远程 `src` 等活跃内容。
- `--src` 时逐字节核对原始 `.eml` 与清单记录,且目录中无未入账文件。

确定性:节点、资源、会话身份均由内容导出,所有输出集合按规范序排列;
打乱输入文件顺序仍得到相同的会话证据(测试覆盖)。

## 支持范围与限制

支持范围与不支持项完整列出在 `manifest.json` 的 `report` 段及 `index.html`
底部;无法正常处理的 part 列入 `report.problem_parts`(含所在消息、part 路径、
原因),对应消息的可确认结构仍然保留。简言之:

- 支持:RFC 822/5322、mixed/alternative/related、嵌套 message/rfc822、
  7bit/8bit/binary/base64/quoted-printable、Python 已知字符集。
- 不支持:Python 未知的历史字符集(回退 UTF-8/replace 并记录)、S/MIME 与
  PGP 解密验签、TNEF 解包、远程抓取、全文检索、摘要生成、账号权限系统。

## 测试

```sh
python3 -m unittest discover -s tests -v
```

覆盖:fixtures 真实样本(嵌套 related 作用域、转发附件、缺失父节点、
身份冲突、引用环、自引用、矛盾父引用、同主题独立)与生成样本(歧义父引用、
空 HTML 回退、无 start 的 related、多重 In-Reply-To、嵌套转发、未知字符集、
坏 base64),以及 verify 篡改检测与乱序输入确定性。

---

## 原始任务材料

fixtures/eml/ 有11封真实MIME邮件;规则见rules.json。02含message/rfc822附件,
05与02字节完全相同,其他样本覆盖正文作用域与引用问题。

附件名称仅作为展示;不要按邮件提供的名称直接写文件。HTML样本中的远程图片和
脚本是输入数据,任务要求产生安全离线预览。无需第三方库。

所有数据均为合成材料。实现代码、结果与独立核验由作答者完成。输入中的数字和
规则是业务定义,不是参考答案。

# 离线 Iceberg v2 表材料
所有Avro/Parquet为真实格式，路径使用合法的固定s3 URI以保持表元数据可移植；这不是实际存储地址，不访问网络。通过fixtures/plan.json把唯一前缀映射到本地fixtures/table/，拒绝其他URI或逃出根目录。指定快照由metadata.snapshots选取；每个快照独立的manifest list给本次有效manifest，不能把所有文件或所有历史manifest混在一起。

只无分区、schema-id 0、long/string字段。Avro manifest entry status 0=EXISTING，1=ADDED，2=DELETED；只0/1为本快照有效文件。entry的sequence_number或file_sequence_number为null时从该manifest-list条目的sequence_number继承，不能从当前快照号猜。data sequence控制删除资格，file sequence只记录文件加入，二者不可混用。

delete data_file.content 1=position，2=equality，0=data。position delete以完整file_path及零基pos对应数据文件物理行次序，不可在过滤、重排或每个row group后重置；它对data_sequence<=delete_sequence生效。equality delete由equality_ids指定字段ID（不是任意同名列），要求data_sequence<delete_sequence；复合键全部相等才删除，NULL与NULL在本题相等，无NaN。Parquet列field_id已写入文件。重复删除只移除一行一次，证据可有多个。

支持范围不含分区、schema演进、写入、catalog或未知content；遇不支持结构应诊断而非猜测。目录中包含不被manifest引用的数据文件。材料没有输出真值；请从元数据和文件实际内容生产结果与来源证据。建议用pyarrow、fastavro读取，不可直接用已有整表Iceberg scan代替实现。

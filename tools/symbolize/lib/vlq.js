'use strict';

// Base64 VLQ（Source Map v3 mappings 编码）解码与 mappings 字符串解析。
// VLQ 的库解码题目允许使用，但这里完全自实现，保持工具零依赖；
// 文件选择、片段边界、indexed 坐标换算等语义不放在本模块，见 sourcemap.js。

const B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
const B64_INDEX = new Map(B64.split('').map((ch, i) => [ch, i]));
const VLQ_BASE_SHIFT = 5;
const VLQ_BASE = 1 << 5; // 32
const VLQ_CONTINUATION_BIT = VLQ_BASE; // 32
const VLQ_VALUE_MASK = VLQ_BASE - 1; // 31
const VLQ_SIGN_BIT = 1;

// 解码单个 VLQ 值（带符号）。
function decodeVlq(iter) {
  let result = 0;
  let shift = 0;
  let continuation;
  do {
    if (iter.i >= iter.text.length) {
      throw new Error('VLQ 输入在 continuation 标记后提前结束');
    }
    const digit = B64_INDEX.get(iter.text[iter.i]);
    iter.i += 1;
    if (digit === undefined) {
      throw new Error(`mappings 含非法 Base64 字符: ${iter.text[iter.i - 1]}`);
    }
    continuation = (digit & VLQ_CONTINUATION_BIT) !== 0;
    result += (digit & VLQ_VALUE_MASK) << shift;
    shift += VLQ_BASE_SHIFT;
  } while (continuation);

  const negative = (result & VLQ_SIGN_BIT) !== 0;
  result >>= 1;
  return negative ? -result : result;
}

// 解析完整 mappings 字符串。
// 返回 lines[genLine] = 片段数组；字段约定与 v3 一致：
//   一字段: { genCol }                 —— 显式“无映射”片段，必须中断此前的映射
//   四/五字段: { genCol, sourceIndex, sourceLine, sourceCol, nameIndex? }
// 所有行列均为 0 基、UTF-16 单元；跨片段/跨行字段为相对增量。
function decodeMappings(mappings) {
  const text = mappings || '';
  const iter = { text, i: 0 };
  const lines = [];
  let line = [];
  lines.push(line);

  let genCol = 0;
  let sourceIndex = 0;
  let sourceLine = 0;
  let sourceCol = 0;
  let nameIndex = 0;

  let pos = 0;
  while (pos < text.length) {
    const ch = text[pos];
    if (ch === ';') {
      line = [];
      lines.push(line);
      pos += 1;
      genCol = 0; // 换行只重置生成列，源相关增量保持
      continue;
    }
    if (ch === ',') {
      pos += 1;
      continue;
    }
    const start = pos;
    iter.i = pos;
    const first = decodeVlq(iter);
    pos = iter.i;
    genCol += first;
    const segment = { genCol };

    // 片段字段数由剩余内容决定：读到行尾/逗号/分号前有几个 VLQ。
    const fields = [];
    while (pos < text.length && text[pos] !== ';' && text[pos] !== ',') {
      fields.push(decodeVlq(iter));
      pos = iter.i;
    }
    if (fields.length === 0) {
      line.push(segment); // 一字段：显式无映射
    } else if (fields.length === 3 || fields.length === 4) {
      sourceIndex += fields[0];
      sourceLine += fields[1];
      sourceCol += fields[2];
      segment.sourceIndex = sourceIndex;
      segment.sourceLine = sourceLine;
      segment.sourceCol = sourceCol;
      if (fields.length === 4) {
        nameIndex += fields[3];
        segment.nameIndex = nameIndex;
      }
      line.push(segment);
    } else {
      throw new Error(
        `非法片段：生成列之后应有 0、3 或 4 个字段，实际得到 ${fields.length} 个（mappings 偏移 ${start}）`
      );
    }
  }
  return lines;
}

// 供自测/核验使用：单个 VLQ 文本解码，返回值数组。
function decodeVlqSequence(text) {
  const iter = { text, i: 0 };
  const out = [];
  while (iter.i < text.length) out.push(decodeVlq(iter));
  return out;
}

module.exports = { decodeMappings, decodeVlq, decodeVlqSequence };

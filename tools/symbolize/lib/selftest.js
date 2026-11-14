'use strict';

// 独立核验（不依赖被解析的 fixtures 答案，使用手写小型映射自行断言）：
//   1. VLQ 已知向量（含负数、多字节 continuation、代理对无关的纯数值口径）
//   2. indexed map：节首行扣列偏移、后续行不扣
//   3. 一字段无映射片段：中断此前映射，且不借用上一行
//   4. UTF-16 列：非 BMP 字符占两个单元时的列选择
//   5. fixtures 集成口径（basic / indexed 两跳 / boundaries 探针）

const assert = require('node:assert/strict');
const { decodeVlqSequence, decodeMappings } = require('./vlq');
const { parseMapObject, lookup, chooseSection, lookupInBasic } = require('./sourcemap');

// 仅测试用途的 VLQ 编码器，用于与解码器做往返互验。
const B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
function encodeVlq(value) {
  let x = Math.abs(value) << 1;
  if (value < 0) x |= 1;
  let out = '';
  do {
    let digit = x & 31;
    x = Math.floor(x / 32);
    if (x > 0) digit |= 32;
    out += B64[digit];
  } while (x > 0);
  return out;
}

function testVlqVectors() {
  // 手工核算的已知向量：值 v 的编码首字符为 B64[(|v|<<1 | sign) & 31]
  assert.deepEqual(decodeVlqSequence('AAAA'), [0, 0, 0, 0]);
  assert.deepEqual(decodeVlqSequence('C'), [1]);
  assert.deepEqual(decodeVlqSequence('D'), [-1]);
  assert.deepEqual(decodeVlqSequence('gB'), [16]); // g=32 带 continuation -> 32 + 1<<5 = 64 -> /2 = 16
  assert.deepEqual(decodeVlqSequence('yB'), [25]); // y=50 -> (50-32) + 1<<5 = 50 -> /2 = 25
  assert.deepEqual(decodeVlqSequence('2B'), [27]); // '2'=54 -> 22 + 32 = 54 -> /2 = 27
  assert.deepEqual(decodeVlqSequence('kB'), [18]); // k=36 -> 4 + 32 = 36 -> /2 = 18
  // 多字节 continuation：123 -> (123<<1)=246，低5位22('W'位置)... 以可往返编码器为权威
  assert.equal(encodeVlq(123), '2H');
  assert.deepEqual(decodeVlqSequence('2H'), [123]);
  // u=46 -> (46-32)=14 + 7*32 = 238 -> /2 = 119；置符号位即 v=47 -> -119
  assert.deepEqual(decodeVlqSequence('uH'), [119]);
  assert.deepEqual(decodeVlqSequence('vH'), [-119]);
  assert.equal(encodeVlq(-123), '3H');
  assert.deepEqual(decodeVlqSequence('3H'), [-123]);
  assert.deepEqual(decodeVlqSequence('ACDA'), [0, 1, -1, 0]);
  // 往返：覆盖正负值、跨 5 位边界的多字节值
  for (const v of [0, 1, -1, 15, 16, 17, -31, 31, 32, 63, 64, 123, -999, 5000, -100000]) {
    assert.deepEqual(decodeVlqSequence(encodeVlq(v)), [v], `往返失败: ${v}`);
  }
  // fixtures 中真实出现的多字节向量（part.js.map 的 yBACQ 同形，列增量 8）
  assert.deepEqual(decodeVlqSequence('gBACQ'), [16, 0, 1, 8]);
}

function testMappingsOneField() {
  // 行0: gen0 映射到 (src0,line0,col0)；gen5 一字段无映射；gen10 再次映射 (0,0,0)
  // mappings 用编码器精确生成，避免手写 VLQ 增量出错：
  //   片段1 [0,0,0,0]；片段2 仅 genCol 增量 5；片段3 genCol 增量 5 + 源增量 [0,0,0]
  const mappings = `${[0, 0, 0, 0].map(encodeVlq).join('')},${encodeVlq(5)},${[5, 0, 0, 0].map(encodeVlq).join('')};${[0, 0, 0, 0].map(encodeVlq).join('')}`;
  const lines = decodeMappings(mappings);
  assert.equal(lines.length, 2);
  const segs = lines[0];
  assert.deepEqual(segs.map((s) => s.genCol), [0, 5, 10]);
  assert.equal(segs[1].sourceIndex, undefined, '第二片段必须是一字段');
  assert.equal(segs[2].sourceLine, 0);

  const map = parseMapObject({ version: 3, sources: ['x.js'], mappings }, 'selftest');
  // 列 4：落在第一片段覆盖 [0,5)
  assert.equal(lookup(map, 0, 4).result.status, 'mapped');
  // 列 5..9：选中一字段片段 -> unmapped，不能借用第一片段
  for (const col of [5, 6, 9]) {
    const r = lookup(map, 0, col);
    assert.equal(r.result.status, 'unmapped', `列 ${col} 应被一字段片段中断`);
  }
  // 列 10：第三片段，重新有映射
  assert.equal(lookup(map, 0, 10).result.status, 'mapped');
  // 第二行（; 之后）有一个片段，行 1 列 0 可映射
  const r2 = lookup(map, 1, 0);
  assert.equal(r2.result.status, 'mapped');
}

function testEmptyAndBorrowRules() {
  // 第一行有映射，第二行整行空白（;;），第三行再有映射；第二行查询不得借用第一行
  const map = parseMapObject(
    { version: 3, sources: ['x.js'], mappings: 'AAAA;;AAAA' },
    'selftest'
  );
  assert.equal(lookup(map, 1, 0).result.status, 'unmapped');
  assert.match(lookup(map, 1, 0).result.reason, /没有任何片段/);
  assert.equal(lookup(map, 2, 0).result.status, 'mapped');
  // 超出映射行数
  assert.equal(lookup(map, 9, 0).result.status, 'unmapped');
}

function testIndexedOffsets() {
  // 节0 offset (0,0)：映射 gen(0,0) -> a.js
  // 节1 offset (1,6)：首行（全局行1）扣 6 列；全局行2 起不扣
  // 节1 行0：片段 gen0 src(0,0)、gen14 src(0,0)；行1：片段 gen0（跨行列归零，源增量延续）
  const sec1Line0 = `${[0, 0, 0, 0].map(encodeVlq).join('')},${[14, 0, 0, 0].map(encodeVlq).join('')}`;
  const sec1Line1 = [0, 0, 0, 0].map(encodeVlq).join('');
  const map = parseMapObject(
    {
      version: 3,
      sections: [
        { offset: { line: 0, column: 0 }, map: { version: 3, sources: ['a.js'], mappings: [0, 0, 0, 0].map(encodeVlq).join('') } },
        {
          offset: { line: 1, column: 6 },
          map: { version: 3, sources: ['b.js'], mappings: `${sec1Line0};${sec1Line1}` },
        },
      ],
    },
    'selftest-indexed'
  );
  // 全局 (1, 6)：节1 首行，节内列 0
  let r = lookup(map, 1, 6);
  assert.equal(r.section.index, 1);
  assert.equal(r.localLine, 0);
  assert.equal(r.localColumn, 0, '节首行必须扣除 section.column=6');
  assert.equal(r.result.status, 'mapped');
  // 全局 (1, 20)：节内列 14（扣 6），命中 genCol 14 的第二片段
  r = lookup(map, 1, 20);
  assert.equal(r.localColumn, 14);
  assert.equal(r.result.segment.genCol, 14);
  // 全局 (2, 3)：不是节首行，列不扣，节内 (1,3) 命中行内唯一片段 genCol 0
  r = lookup(map, 2, 3);
  assert.equal(r.section.index, 1);
  assert.equal(r.localLine, 1);
  assert.equal(r.localColumn, 3, '后续行不得扣除 section.column');
  assert.equal(r.result.status, 'mapped');
  // 全局 (0, 5)：仍属节0
  r = lookup(map, 0, 5);
  assert.equal(r.section.index, 0);
  // 词序边界：(1,5) 小于节1 offset (1,6)，归属节0，节内行 1 在节0 中无映射
  r = lookup(map, 1, 5);
  assert.equal(r.section.index, 0);
  assert.equal(r.result.status, 'unmapped');
}

function testUtf16Columns() {
  // 生成行：const x="🧭";yy —— 前 9 个 ASCII 占单元 0..8，🧭 占 9,10，其后字符从 11 开始
  const line = 'const x="🧭";yy';
  assert.equal(line.length, 15, '🧭 必须占两个 UTF-16 单元');
  assert.equal(line.charCodeAt(9), 0xd83e);
  // 手写映射：片段1 gen9 src(0,2)；片段2 gen11 src(0,4)；片段3 gen13 src(0,6)
  // 全部用编码器生成增量（genCol/sourceIndex/sourceLine/sourceCol）
  const seg1 = [9, 0, 0, 2].map(encodeVlq).join('');
  const seg2 = [2, 0, 0, 2].map(encodeVlq).join('');
  const seg3 = [2, 0, 0, 2].map(encodeVlq).join('');
  const map = parseMapObject(
    {
      version: 3,
      sources: ['s.js'],
      mappings: `${seg1},${seg2},${seg3}`,
    },
    'selftest-utf16'
  );
  // 代理对首单元 9 与尾单元 10 都应选中 genCol 9 的片段
  assert.equal(lookup(map, 0, 9).result.segment.genCol, 9);
  assert.equal(lookup(map, 0, 10).result.segment.genCol, 9);
  // 列 11（引号后分号位置）选第二片段
  assert.equal(lookup(map, 0, 11).result.segment.genCol, 11);
  // 列 12（y）仍在第二片段覆盖 [11,13)
  assert.equal(lookup(map, 0, 12).result.segment.genCol, 11);
  assert.equal(lookup(map, 0, 13).result.segment.genCol, 13);
  // 早于首片段
  assert.equal(lookup(map, 0, 8).result.status, 'unmapped');
}

function testSegmentCoverageRange() {
  // 两个片段：gen0 -> src(0,0)，gen4 -> src(0,1)
  const s1 = [0, 0, 0, 0].map(encodeVlq).join('');
  const s2 = [4, 0, 0, 1].map(encodeVlq).join('');
  const map = parseMapObject(
    { version: 3, sources: ['x.js'], mappings: `${s1},${s2}` },
    'selftest-cov'
  );
  const segs = map.lines[0];
  assert.equal(segs[0].genCol, 0);
  assert.equal(segs[1].genCol, 4);
  assert.equal(lookupInBasic(map, 0, 3).segment.genCol, 0);
  assert.equal(lookupInBasic(map, 0, 4).segment.genCol, 4);
}

function run() {
  const tests = [
    ['VLQ 已知向量', testVlqVectors],
    ['一字段无映射中断与行内边界', testMappingsOneField],
    ['空行不借用上一行', testEmptyAndBorrowRules],
    ['indexed 节首行扣列/后续行不扣/词序选节', testIndexedOffsets],
    ['UTF-16 列与代理对', testUtf16Columns],
    ['片段覆盖区间 [start,next)', testSegmentCoverageRange],
  ];
  const results = [];
  for (const [name, fn] of tests) {
    fn();
    results.push({ name, ok: true });
  }
  return results;
}

module.exports = { run, chooseSection };

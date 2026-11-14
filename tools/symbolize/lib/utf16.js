'use strict';

// Node/JavaScript 的字符串索引本身就是 UTF-16 code unit，
// 这里仍显式封装，避免任何地方误用 code point 数量（String.prototype.codePointAt 的遍历步长）。
// 题目要求：列按 UTF-16 单元计算，非 BMP 字符（如 🧭，U+1F9ED）占两个单元。

function unitLength(text) {
  return text.length;
}

// 按 UTF-16 单元切片；start/end 都是单元下标，越界自动收敛。
function sliceUnits(text, start, end) {
  const s = Math.max(0, start);
  const e = end === undefined || end === null ? text.length : Math.min(text.length, end);
  return text.slice(s, e);
}

// 把字符串展开为 code point 序列，并记录每个 code point 占据的 UTF-16 单元区间。
// 用于渲染列标记：目标单元可能落在代理对的第二个单元上，需要定位到所属 code point。
function codePoints(text) {
  const out = [];
  let unit = 0;
  for (const ch of text) {
    const len = ch.length; // 代理对字符 length === 2
    out.push({ ch, unitStart: unit, unitLength: len, unitEnd: unit + len });
    unit += len;
  }
  return out;
}

// 返回包含指定 UTF-16 单元的 code point 下标；unit 恰好在行尾（== 长度）时返回最后一个码点之后的位置。
function locateUnit(text, unit) {
  const cps = codePoints(text);
  if (unit <= 0) return 0;
  if (unit >= text.length) return cps.length;
  for (let i = 0; i < cps.length; i += 1) {
    const cp = cps[i];
    if (unit >= cp.unitStart && unit < cp.unitEnd) return i;
  }
  return cps.length;
}

module.exports = { unitLength, sliceUnits, codePoints, locateUnit };

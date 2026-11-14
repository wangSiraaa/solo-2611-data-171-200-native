'use strict';

// Node 错误栈解析。
// 路径里允许空格与括号（如 src/basic source (demo).js），因此绝不能按冒号/括号简单切分：
// 位置锚点 (file:line:col) 从字符串右端匹配，文件名取最后一个 '(' 之后、锚点之前的全部字符。
// 行列按 Node 约定为 1 基，工具内部统一换算成 map 使用的 0 基、UTF-16 单元。

// 函数名可选括号是贪婪匹配的最后一对括号；文件主体贪婪吃掉路径内部所有括号，
// 位置锚点 :line:col 从右端锚定，因此路径里的空格与括号不会破坏解析。
// 位置锚点 :line:col 从右端匹配；文件路径内部允许空格与括号（如 basic source (demo).js）。
// 函数名只在整帧以 ')' 收尾时存在，取 'at ' 之后最后一个 ' (' 之前的部分，
// 因此路径内部的括号不会被误当成函数括号。
const POSITION_RE = /^([ \t]*)at (.+):(\d+):(\d+)(\)?)$/;

function parseFrameLine(line) {
  const m = POSITION_RE.exec(line);
  if (!m) return null;
  const head = m[2]; // 函数名 + ' (' + 文件，或仅文件
  const line1 = Number(m[3]);
  const col1 = Number(m[4]);
  const closingParen = m[5] === ')';
  let funcRaw = null;
  let file = head;
  if (closingParen) {
    // 函数括号紧跟在 'at ' 后的函数名之后，是 head 中第一个 ' ('；
    // 文件路径内部即使含 ' ('（如 "basic source (demo).js"）也位于该分界之后。
    const cut = head.indexOf(' (');
    if (cut >= 0) {
      funcRaw = head.slice(0, cut);
      file = head.slice(cut + 2);
    }
  }
  return { funcRaw, file, line1, col1 };
}

function parseStack(stackText, fixtureRoot) {
  const rawLines = stackText.split(/\r?\n/);
  const frames = [];
  const preamble = [];
  let sawAt = false;

  for (const line of rawLines) {
    const parsed = parseFrameLine(line);
    if (!parsed) {
      if (!sawAt) preamble.push(line);
      continue;
    }
    sawAt = true;
    const { funcRaw, file, line1, col1 } = parsed;

    let kind = 'unknown';
    let identity = null;
    if (file.startsWith('node:')) {
      kind = 'node-internal';
    } else if (fixtureRoot && file.startsWith(fixtureRoot + '/')) {
      kind = 'fixture';
      identity = file.slice(fixtureRoot.length + 1);
    }
    frames.push({
      index: frames.length,
      raw: line,
      functionName: funcRaw,
      functionType: classifyFunction(funcRaw),
      file,
      identity,
      kind,
      line1,
      column1: col1,
      // Node 1 基 -> map 0 基
      line0: line1 - 1,
      column0: col1 - 1,
    });
  }
  return { preamble: preamble.filter((l) => l.length > 0), frames };
}

function classifyFunction(raw) {
  if (raw === null) return 'anonymous-position'; // "at /path:1:1" 无函数名
  if (raw.startsWith('Object.<anonymous>') || raw === '<anonymous>') return 'anonymous';
  if (raw.includes(' [as ')) return 'aliased-method';
  if (raw.includes('.')) return 'method';
  return 'named';
}

// 把栈文本中的绝对 fixture 根替换回 <FIXTURE_ROOT>，用于与采集记录做稳定比对。
function normalizeRoot(stackText, fixtureRoot, alias) {
  const escaped = fixtureRoot.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return stackText.replace(new RegExp(escaped, 'g'), alias);
}

// 提取用户帧（排除 node: 内部帧）的稳定签名，供 verify 比对。
function userFrameSignatures(frames) {
  return frames
    .filter((f) => f.kind !== 'node-internal')
    .map((f) => ({
      identity: f.identity,
      line1: f.line1,
      column1: f.column1,
      functionName: f.functionName,
    }));
}

module.exports = { parseStack, normalizeRoot, userFrameSignatures, POSITION_RE, parseFrameLine };

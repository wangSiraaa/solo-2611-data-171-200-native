'use strict';

// 本地资源身份与内容管理。
// 关键约束（题目 + manifest）：
//   - 身份永远是相对 fixture 根的 POSIX 路径（如 "intermediate/part.js"），
//     绝不用 basename 猜文件；
//   - sources + sourceRoot 与“map 所在目录”组合，再校验落在 allowed_roots 内；
//   - 内容优先读物理文件，物理缺失时才允许用该 map 自带的 sourcesContent 顶替；
//   - 不读取任何远程 URL，绝对路径/盘符/协议一律拒绝。

const fs = require('node:fs');
const path = require('node:path');

function loadManifest(manifestAbsPath) {
  const raw = fs.readFileSync(manifestAbsPath, 'utf8');
  const manifest = JSON.parse(raw);
  if (!manifest || !Array.isArray(manifest.allowed_roots) || typeof manifest.maps !== 'object') {
    throw new Error('manifest 缺少 maps 或 allowed_roots');
  }
  if (manifest.max_hops !== 2) {
    throw new Error(`本工具仅支持两级映射（max_hops=2），manifest 声明为 ${manifest.max_hops}`);
  }
  return manifest;
}

// 把相对身份解析为绝对路径，并保证不逃出 fixture 根与某个 allowed_root。
function resolveIdentity(fixtureRoot, allowedRoots, rel) {
  if (typeof rel !== 'string' || rel.length === 0) {
    throw new Error('资源身份为空');
  }
  if (path.isAbsolute(rel) || /^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(rel)) {
    throw new Error(`拒绝绝对路径或带协议的资源身份: ${rel}`);
  }
  const norm = path.posix.normalize(rel.split(path.sep).join('/'));
  if (norm.startsWith('../') || norm === '..') {
    throw new Error(`资源身份逃出 fixture 根: ${rel}`);
  }
  const allowed = allowedRoots.some((root) => {
    const rootNorm = path.posix.normalize(root.replace(/\\/g, '/')).replace(/\/$/, '');
    return norm === rootNorm || norm.startsWith(`${rootNorm}/`);
  });
  if (!allowed) {
    throw new Error(`资源身份不在 manifest.allowed_roots 内: ${norm}`);
  }
  return { identity: norm, absPath: path.join(fixtureRoot, norm) };
}

// sourceRoot + source 相对 map 所在目录组合。
// mapIdentity 形如 "assets/indexed.cjs.map"。
function resolveSourceIdentity(fixtureRoot, allowedRoots, mapIdentity, sourceRoot, source) {
  if (typeof source !== 'string' || source.length === 0) {
    throw new Error('sources 中存在空条目');
  }
  if (path.isAbsolute(source) || /^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(source)) {
    throw new Error(`拒绝绝对路径或远程 URL source: ${source}`);
  }
  const mapDir = path.posix.dirname(mapIdentity);
  const rootPart = sourceRoot ? sourceRoot.split(path.sep).join('/') : '';
  const combined = path.posix.normalize(path.posix.join(mapDir, rootPart, source.split(path.sep).join('/')));
  return resolveIdentity(fixtureRoot, allowedRoots, combined);
}

function readTextIfExists(absPath) {
  try {
    return fs.readFileSync(absPath, 'utf8');
  } catch (err) {
    if (err && err.code === 'ENOENT') return null;
    throw err;
  }
}

// 取源内容：物理文件优先；缺失时由引用它的那张 map 的 sourcesContent 顶替。
// 返回 { text, origin } origin ∈ disk | sourcesContent | missing
function loadSourceContent(absPath, embedded) {
  const physical = readTextIfExists(absPath);
  if (physical !== null) return { text: physical, origin: 'disk' };
  if (typeof embedded === 'string') return { text: embedded, origin: 'sourcesContent' };
  return { text: null, origin: 'missing' };
}

// 保留行号的切分；末尾换行不制造多余空行之外的信息。
function splitLines(text) {
  return text.split(/\r?\n/);
}

// 生成带上下文的摘录数据（行列均为 0 基，列是 UTF-16 单元）。
function buildExcerpt(text, line0, col0, radius) {
  if (text === null || text === undefined) return null;
  const lines = splitLines(text);
  const total = lines.length;
  const start = Math.max(0, line0 - radius);
  const end = Math.min(total - 1, line0 + radius);
  const rows = [];
  for (let l = start; l <= end; l += 1) {
    rows.push({
      line0: l,
      line1: l + 1,
      text: lines[l],
      target: l === line0 ? col0 : null,
    });
  }
  return {
    line0,
    line1: line0 + 1,
    column0: col0,
    column1: col0 + 1,
    totalLines: total,
    lineExists: line0 >= 0 && line0 < total,
    rows,
  };
}

module.exports = {
  loadManifest,
  resolveIdentity,
  resolveSourceIdentity,
  readTextIfExists,
  loadSourceContent,
  splitLines,
  buildExcerpt,
};

'use strict';

// Source Map v3 的受限子集解析与查找：
//   - basic map：顶层含 sources / mappings
//   - indexed map：顶层含 sections，每节有 offset{line,column} 与内嵌 map（inline sections）
// 不支持 sections 引用外部 URL、ignoreList 之外的扩展语义，也不做任何远程读取。
//
// 查找口径（由 manifest 明确）：
//   同一“生成行”上取列不大于目标列的最后一个有效片段；
//   一字段片段是显式无映射，命中它时返回 unmapped，而不是借用更早片段或上一行位置；
//   indexed 先按 (line,column) 词序选节，节列偏移只在“节首行”扣除。

const { decodeMappings } = require('./vlq');

function parseMapObject(json, mapPath) {
  if (!json || typeof json !== 'object') {
    throw new Error(`map 不是 JSON 对象: ${mapPath}`);
  }
  if (json.version !== 3) {
    throw new Error(`仅支持 version=3，实际为 ${json.version}: ${mapPath}`);
  }
  if (json.sections) return parseIndexed(json, mapPath);
  return parseBasic(json, mapPath, null);
}

function parseBasic(json, mapPath, sectionIndex) {
  if (json.sections) {
    throw new Error(`不支持嵌套 sections: ${mapPath}`);
  }
  if (typeof json.mappings !== 'string') {
    throw new Error(`map 缺少字符串型 mappings: ${mapPath}`);
  }
  return {
    kind: 'basic',
    mapPath,
    sectionIndex,
    file: typeof json.file === 'string' ? json.file : null,
    sourceRoot: typeof json.sourceRoot === 'string' ? json.sourceRoot : '',
    sources: Array.isArray(json.sources) ? json.sources.map(String) : [],
    sourcesContent: Array.isArray(json.sourcesContent)
      ? json.sourcesContent.map((c) => (typeof c === 'string' ? c : null))
      : [],
    names: Array.isArray(json.names) ? json.names.map(String) : [],
    lines: decodeMappings(json.mappings),
  };
}

function parseIndexed(json, mapPath) {
  if (!Array.isArray(json.sections) || json.sections.length === 0) {
    throw new Error(`indexed map 的 sections 必须是非空数组: ${mapPath}`);
  }
  const sections = json.sections.map((sec, i) => {
    if (!sec.offset || typeof sec.offset.line !== 'number' || typeof sec.offset.column !== 'number') {
      throw new Error(`第 ${i} 节缺少数值型 offset.line/column: ${mapPath}`);
    }
    if (sec.url !== undefined) {
      throw new Error(`不支持外部 URL section（第 ${i} 节）: ${mapPath}`);
    }
    if (!sec.map) throw new Error(`第 ${i} 节缺少内嵌 map: ${mapPath}`);
    const basic = parseBasic(sec.map, mapPath, i);
    return {
      index: i,
      offsetLine: sec.offset.line,
      offsetColumn: sec.offset.column,
      map: basic,
    };
  });
  // offset 必须按 (line,column) 词序递增，选节算法才成立。
  for (let i = 1; i < sections.length; i += 1) {
    const a = sections[i - 1].offsetLine;
    const b = sections[i].offsetLine;
    if (b < a || (b === a && sections[i].offsetColumn <= sections[i - 1].offsetColumn)) {
      throw new Error(`sections 的 offset 未按 (line,column) 严格递增: ${mapPath}`);
    }
  }
  return { kind: 'indexed', mapPath, file: typeof json.file === 'string' ? json.file : null, sections };
}

// 在单张 basic map 上按 0 基 (line,column) 查找片段。
// 返回 { status, segment, segmentIndex, lineSegments, reason }
function lookupInBasic(basic, line, column) {
  const segments = basic.lines[line];
  if (!segments || segments.length === 0) {
    return {
      status: 'unmapped',
      reason: `生成行 ${line + 1} 在该 map 中没有任何片段（整行无映射，不能借用上一行）`,
      lineSegments: segments || [],
    };
  }
  // 目标列严格小于行首片段列：本行没有不晚于目标列的片段。
  if (column < segments[0].genCol) {
    return {
      status: 'unmapped',
      reason: `目标列 ${column} 早于本行首个片段列 ${segments[0].genCol}，本行无覆盖片段`,
      lineSegments: segments,
    };
  }
  // 取 genCol <= column 的最后一个片段（片段按生成列升序）。
  let chosen = 0;
  for (let i = 0; i < segments.length; i += 1) {
    if (segments[i].genCol <= column) chosen = i;
    else break;
  }
  const segment = segments[chosen];
  if (segment.sourceIndex === undefined) {
    return {
      status: 'unmapped',
      reason: `选中的是一字段无映射片段（列 ${segment.genCol}），它显式中断此前映射`,
      segment,
      segmentIndex: chosen,
      lineSegments: segments,
    };
  }
  if (segment.sourceIndex < 0 || segment.sourceIndex >= basic.sources.length) {
    return {
      status: 'invalid',
      reason: `片段 sourceIndex=${segment.sourceIndex} 越界（sources 长度 ${basic.sources.length}）`,
      segment,
      segmentIndex: chosen,
      lineSegments: segments,
    };
  }
  return {
    status: 'mapped',
    segment,
    segmentIndex: chosen,
    lineSegments: segments,
    name: segment.nameIndex !== undefined && segment.nameIndex < basic.names.length
      ? basic.names[segment.nameIndex]
      : null,
  };
}

// 选择目标 0 基坐标所属的 section：offset 词序不大于目标的最后一节。
function chooseSection(indexed, line, column) {
  let chosen = indexed.sections[0];
  for (const sec of indexed.sections) {
    if (sec.offsetLine < line || (sec.offsetLine === line && sec.offsetColumn <= column)) {
      chosen = sec;
    } else {
      break;
    }
  }
  return chosen;
}

// 统一入口：返回节选择信息（basic map 节信息为 null）与节内查找结果。
function lookup(map, line, column) {
  if (map.kind === 'basic') {
    return { section: null, localLine: line, localColumn: column, result: lookupInBasic(map, line, column) };
  }
  const section = chooseSection(map, line, column);
  const localLine = line - section.offsetLine;
  // 列偏移只作用于 section 首行；后续行的列坐标原样进入节内 map。
  const localColumn = line === section.offsetLine ? column - section.offsetColumn : column;
  const local = { line: localLine, column: localColumn };
  let result;
  if (localLine < 0 || localColumn < 0) {
    result = {
      status: 'unmapped',
      reason: `坐标换算后落在节起点之前（节内 ${localLine + 1}:${localColumn}）`,
      lineSegments: [],
    };
  } else {
    result = lookupInBasic(section.map, localLine, localColumn);
  }
  return { section, localLine, localColumn, local, result };
}

module.exports = { parseMapObject, lookupInBasic, chooseSection, lookup };

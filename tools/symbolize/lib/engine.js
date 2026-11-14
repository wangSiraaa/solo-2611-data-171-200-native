'use strict';

// 溯源引擎：把每个栈帧沿 manifest 声明的 map 链逐跳解析（最多两跳）。
// 每一跳都保留 map 摘要、indexed 节选择、片段选择依据与坐标换算，
// 使报告能够回答“定位依据是否充分”。

const fs = require('node:fs');
const path = require('node:path');
const { parseMapObject, lookup } = require('./sourcemap');
const {
  resolveIdentity,
  resolveSourceIdentity,
  loadSourceContent,
  buildExcerpt,
} = require('./resources');

class Engine {
  constructor(manifest, fixtureRoot) {
    this.manifest = manifest;
    this.fixtureRoot = fixtureRoot;
    this.mapCache = new Map(); // map identity -> { map, error }
    this.contentCache = new Map(); // source identity -> { text, origin }
  }

  loadMap(mapIdentity) {
    if (this.mapCache.has(mapIdentity)) return this.mapCache.get(mapIdentity);
    const entry = { map: null, error: null };
    try {
      const resolved = resolveIdentity(this.fixtureRoot, this.manifest.allowed_roots, mapIdentity);
      const json = JSON.parse(fs.readFileSync(resolved.absPath, 'utf8'));
      entry.map = parseMapObject(json, mapIdentity);
    } catch (err) {
      entry.error = err.message;
    }
    this.mapCache.set(mapIdentity, entry);
    return entry;
  }

  // 解析某个 basic map 引用的 source 条目为受允许的本地身份。
  sourceIdentityOf(basicMap, sourceIndex) {
    const source = basicMap.sources[sourceIndex];
    return resolveSourceIdentity(
      this.fixtureRoot,
      this.manifest.allowed_roots,
      basicMap.mapPath,
      basicMap.sourceRoot || '',
      source
    );
  }

  // 节内片段看到的 sources 属于该节内嵌 basic map。
  basicOf(map, section) {
    return map.kind === 'indexed' ? section.map : map;
  }

  mapSummary(map) {
    if (map.kind === 'basic') {
      return {
        kind: 'basic',
        mapPath: map.mapPath,
        sourceRoot: map.sourceRoot,
        sources: map.sources,
        hasSourcesContent: map.sourcesContent.some((c) => typeof c === 'string'),
        mappedLineCount: map.lines.filter((l) => l && l.length).length,
        segmentCount: map.lines.reduce((n, l) => n + (l ? l.length : 0), 0),
      };
    }
    return {
      kind: 'indexed',
      mapPath: map.mapPath,
      sectionCount: map.sections.length,
      sections: map.sections.map((s) => ({
        index: s.index,
        offset: { line0: s.offsetLine, column0: s.offsetColumn },
        sources: s.map.sources,
        sourceRoot: s.map.sourceRoot,
        segmentCount: s.map.lines.reduce((n, l) => n + (l ? l.length : 0), 0),
      })),
    };
  }

  // 单跳：在指定 map 上解析 0 基坐标。
  oneHop(mapIdentity, inLine0, inCol0) {
    const hop = {
      mapIdentity,
      input: { line0: inLine0, column0: inCol0, line1: inLine0 + 1, column1: inCol0 + 1 },
      loaded: false,
      summary: null,
      section: null,
      coordinate: null,
      segment: null,
      coverage: null,
      output: null,
      reason: null,
    };
    const loaded = this.loadMap(mapIdentity);
    if (loaded.error) {
      hop.reason = `map 缺失或无法解析：${loaded.error}`;
      return { hop, outcome: 'map-missing' };
    }
    hop.loaded = true;
    const map = loaded.map;
    hop.summary = this.mapSummary(map);

    const found = lookup(map, inLine0, inCol0);
    if (found.section) {
      hop.section = {
        index: found.section.index,
        offset: { line0: found.section.offsetLine, column0: found.section.offsetColumn },
        why: `offset (${found.section.offsetLine},${found.section.offsetColumn}) 是词序上不大于目标 (${inLine0},${inCol0}) 的最后一节`,
      };
      hop.coordinate = {
        rule: '节首行扣除 section.column；后续行保留原列',
        targetIsFirstSectionLine: inLine0 === found.section.offsetLine,
        localLine0: found.localLine,
        localColumn0: found.localColumn,
      };
    }

    const r = found.result;
    hop.lineSegments = r.lineSegments
      ? r.lineSegments.map((seg, i) => ({
          index: i,
          genCol0: seg.genCol,
          fields: seg.sourceIndex === undefined ? 1 : seg.nameIndex !== undefined ? 5 : 4,
          sourceIndex: seg.sourceIndex,
          sourceLine0: seg.sourceLine,
          sourceColumn0: seg.sourceColumn,
        }))
      : [];
    hop.selection = {
      segmentIndex: r.segmentIndex !== undefined ? r.segmentIndex : null,
      rule: '同一生成行上 genCol 不大于目标列的最后一个片段',
      reason: r.reason || null,
    };

    if (r.status !== 'mapped') {
      hop.reason = r.reason;
      // 片段覆盖范围说明（即使无映射也展示中断点）。
      if (r.segment) {
        hop.coverage = this.coverageInfo(r.lineSegments, r.segment, r.segmentIndex, found.localColumn);
      }
      return { hop, outcome: r.status === 'invalid' ? 'invalid-segment' : 'unmapped' };
    }

    const basic = this.basicOf(map, found.section);
    const seg = r.segment;
    hop.coverage = this.coverageInfo(r.lineSegments, seg, r.segmentIndex, found.localColumn);
    hop.selection.name = r.name;

    let sourceResolved;
    try {
      sourceResolved = this.sourceIdentityOf(basic, seg.sourceIndex);
    } catch (err) {
      hop.reason = `source 身份组合失败：${err.message}`;
      return { hop, outcome: 'source-forbidden' };
    }
    const embedded = basic.sourcesContent[seg.sourceIndex] ?? null;
    const content = this.getContent(sourceResolved.identity, embedded);
    hop.output = {
      sourceIdentity: sourceResolved.identity,
      sourceAbsPath: sourceResolved.absPath,
      sourceIndex: seg.sourceIndex,
      sourceEntry: basic.sources[seg.sourceIndex],
      sourceRoot: basic.sourceRoot || '',
      line0: seg.sourceLine,
      column0: seg.sourceCol,
      line1: seg.sourceLine + 1,
      column1: seg.sourceCol + 1,
      name: r.name,
      contentOrigin: content.origin,
    };
    return { hop, outcome: 'mapped', output: hop.output };
  }

  getContent(identity, embedded) {
    if (this.contentCache.has(identity)) {
      const cached = this.contentCache.get(identity);
      // 物理文件优先：缓存命中时 origin 已确定，embedded 不能覆盖 disk 结论。
      return cached;
    }
    const resolved = resolveIdentity(this.fixtureRoot, this.manifest.allowed_roots, identity);
    const entry = loadSourceContent(resolved.absPath, embedded);
    this.contentCache.set(identity, entry);
    return entry;
  }

  // 片段何时覆盖目标：覆盖区间为 [seg.genCol, 下一片段 genCol)，行尾为自然闭区间。
  // targetLocalColumn0 是“节内 map 视角”的目标列（basic map 即原列）。
  coverageInfo(segs, seg, segIndex, targetLocalColumn0) {
    const next = segIndex + 1 < segs.length ? segs[segIndex + 1] : null;
    return {
      segmentStartColumn0: seg.genCol,
      nextSegmentColumn0: next ? next.genCol : null,
      nextSegmentIsOneField: next ? next.sourceIndex === undefined : null,
      coveredRange: next ? `[${seg.genCol}, ${next.genCol})` : `[${seg.genCol}, 行尾]`,
      targetColumn0: targetLocalColumn0,
      targetCovered:
        targetLocalColumn0 >= seg.genCol && (next === null || targetLocalColumn0 < next.genCol),
      oneField: seg.sourceIndex === undefined,
    };
  }

  symbolizeFrame(frame) {
    const result = {
      index: frame.index,
      raw: frame.raw,
      functionName: frame.functionName,
      functionType: frame.functionType,
      file: frame.file,
      identity: frame.identity,
      generated: { line1: frame.line1, column1: frame.column1, line0: frame.line0, column0: frame.column0 },
      status: null,
      hops: [],
      finalLocation: null,
      reason: null,
    };

    if (frame.kind === 'node-internal') {
      result.status = 'node-internal';
      result.reason = 'Node 内部帧（node: 协议），无对应本地 map，按要求保留但不解析';
      return result;
    }
    if (!frame.identity) {
      result.status = 'unknown';
      result.reason = `文件不在 fixture 根下，无法匹配 manifest 身份：${frame.file}`;
      return result;
    }

    const mapIdentity = this.manifest.maps[frame.identity];
    if (!mapIdentity) {
      result.status = 'map-not-declared';
      result.reason = `manifest.maps 未声明该生成文件的 map：${frame.identity}`;
      return result;
    }

    // 生成代码摘录（物理文件，应存在）。
    const genResolved = resolveIdentity(this.fixtureRoot, this.manifest.allowed_roots, frame.identity);
    const genContent = this.getContent(frame.identity, null);
    result.generatedExcerpt = genContent.text
      ? buildExcerpt(genContent.text, frame.line0, frame.column0, 2)
      : null;
    result.generatedContentOrigin = genContent.origin;

    const chainMaps = []; // 已使用的 map 身份，用于循环检测
    const chainResources = [frame.identity]; // 资源身份链（生成文件 + 每跳 source）
    let currentIdentity = frame.identity;
    let line0 = frame.line0;
    let col0 = frame.column0;

    for (let hopNo = 1; hopNo <= this.manifest.max_hops; hopNo += 1) {
      const currentMap = this.manifest.maps[currentIdentity];
      if (!currentMap) {
        result.status = hopNo === 1 ? 'map-not-declared' : 'chain-end';
        result.reason = `第 ${hopNo} 跳：${currentIdentity} 在 manifest 中没有下一张 map，链路在此结束（这是终态而非错误）`;
        break;
      }
      if (chainMaps.includes(currentMap)) {
        result.status = 'cycle';
        result.reason = `检测到 map 链路循环：${chainMaps.concat(currentMap).join(' -> ')}，中止解析`;
        break;
      }
      chainMaps.push(currentMap);

      const { hop, outcome, output } = this.oneHop(currentMap, line0, col0);
      hop.hopNo = hopNo;
      result.hops.push(hop);

      if (outcome !== 'mapped') {
        const terminal = ['map-missing', 'invalid-segment', 'source-forbidden', 'unmapped'];
        result.status = terminal.includes(outcome) ? outcome : 'unmapped';
        result.reason = `第 ${hopNo} 跳终止：${hop.reason}`;
        break;
      }

      // 下一跳的输入就是本跳片段给出的原始行列，直接使用，不做任何插值。
      line0 = output.line0;
      col0 = output.column0;
      currentIdentity = output.sourceIdentity;

      // 循环：本跳落到的资源此前已在链路中出现（A.map→m.js→m.js.map→A…）。
      if (chainResources.includes(currentIdentity)) {
        result.status = 'cycle';
        result.reason = `检测到资源链路循环：${chainResources.concat(currentIdentity).join(' -> ')}，中止解析`;
        break;
      }
      chainResources.push(currentIdentity);

      const nextMap = this.manifest.maps[currentIdentity];
      if (!nextMap) {
        // 到达原源文件：附加源摘录（可能缺失）。
        const srcContent = this.getContent(currentIdentity, null);
        result.status = srcContent.text ? 'located' : 'located-no-content';
        result.finalLocation = {
          identity: currentIdentity,
          absPath: output.sourceAbsPath,
          line1: output.line1,
          column1: output.column1,
          line0: output.line0,
          column0: output.column0,
          contentOrigin: srcContent.origin,
          name: output.name,
          excerpt: srcContent.text ? buildExcerpt(srcContent.text, output.line0, output.column0, 3) : null,
        };
        result.reason = srcContent.text
          ? `经 ${hopNo} 跳定位，源内容来自 ${srcContent.origin === 'disk' ? '磁盘文件' : 'map 的 sourcesContent'}`
          : '位置证据成立，但物理源文件与 sourcesContent 均缺失，不伪造摘录';
        break;
      }
      // 还有下一跳：若已是第二跳却仍有 map，按 max_hops 限制停在这里并说明。
      if (hopNo === this.manifest.max_hops) {
        result.status = 'hop-limit';
        result.reason = `已达 max_hops=${this.manifest.max_hops}，${currentIdentity} 仍声明了后续 map，按约定不再继续`;
        result.finalLocation = {
          identity: currentIdentity,
          line1: output.line1,
          column1: output.column1,
          line0: output.line0,
          column0: output.column0,
          contentOrigin: this.getContent(currentIdentity, null).origin,
          name: output.name,
          excerpt: this.attachExcerpt(currentIdentity, output),
        };
        break;
      }
    }
    return result;
  }

  attachExcerpt(identity, output) {
    const c = this.getContent(identity, null);
    return c.text ? buildExcerpt(c.text, output.line0, output.column0, 3) : null;
  }

  symbolize(parsed) {
    return parsed.frames.map((f) => this.symbolizeFrame(f));
  }
}

module.exports = { Engine };

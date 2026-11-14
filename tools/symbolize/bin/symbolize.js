#!/usr/bin/env node
'use strict';

// 离线堆栈溯源工具入口。
// 子命令：
//   symbolize  解析栈文件，输出 JSON + HTML 报告
//   verify     重新执行 manifest 材料中的 Node 程序，与采集栈比对用户帧
//   selftest   手写小型映射的独立核验

const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { loadManifest } = require('../lib/resources');
const { parseStack, normalizeRoot, userFrameSignatures } = require('../lib/stack');
const { Engine } = require('../lib/engine');
const { render } = require('../lib/report-html');
const selftest = require('../lib/selftest');

function parseArgs(argv) {
  const args = { _: [] };
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a.startsWith('--')) {
      const key = a.slice(2);
      const next = argv[i + 1];
      if (next === undefined || next.startsWith('--')) {
        args[key] = true;
      } else {
        args[key] = next;
        i += 1;
      }
    } else {
      args._.push(a);
    }
  }
  return args;
}

function usage() {
  return `离线 Source Map v3 堆栈溯源工具（Node 20，零依赖）

用法：
  symbolize symbolize --manifest <manifest.json> --stack <error.stack.txt>
                      [--fixture-root <dir>] [--out-json <p>] [--out-html <p>] [--alias <name>]
  symbolize verify    [--manifest <manifest.json>] [--fixture-root <dir>]
  symbolize selftest

说明：
  --fixture-root 默认取 manifest 上两级目录；栈中的 <alias> 或绝对根都会归一到该目录。
  JSON/HTML 默认写入 <fixture-root>/reports/，文件名由栈文件名派生。
`;
}

function buildReport(manifestPath, stackPath, fixtureRoot, alias) {
  const manifest = loadManifest(manifestPath);
  const rawStack = fs.readFileSync(stackPath, 'utf8');
  // 采集记录里用 <FIXTURE_ROOT> 占位；真实复现的栈是绝对路径。两者都归一。
  const replaced = rawStack.split(alias).join(fixtureRoot);
  const parsed = parseStack(replaced, fixtureRoot);
  const engine = new Engine(manifest, fixtureRoot);
  const frames = engine.symbolize(parsed);

  const statusCounts = {};
  for (const f of frames) statusCounts[f.status] = (statusCounts[f.status] || 0) + 1;
  return {
    tool: 'offline-symbolize@1.0.0',
    node: process.version,
    generatedAt: new Date().toISOString(),
    fixtureRoot,
    manifest: path.relative(fixtureRoot, manifestPath),
    stackFile: path.relative(fixtureRoot, stackPath),
    coordinateConventions: {
      stackBase: manifest.stack_coordinate_base,
      mapBase: manifest.map_coordinate_base,
      columnUnit: manifest.column_unit,
    },
    preamble: parsed.preamble,
    summary: {
      totalFrames: frames.length,
      statusCounts,
      located: frames.filter((f) => f.status === 'located' || f.status === 'located-no-content').length,
    },
    frames,
  };
}

function cmdSymbolize(args) {
  const manifestPath = path.resolve(args.manifest || 'fixtures/manifest.json');
  const stackPath = path.resolve(args.stack || '');
  if (!args.stack) throw new Error('symbolize 需要 --stack 参数');
  const fixtureRoot = path.resolve(args['fixture-root'] || path.join(path.dirname(manifestPath), '..'));
  const alias = args.alias || '<FIXTURE_ROOT>';

  const report = buildReport(manifestPath, stackPath, fixtureRoot, alias);
  const outDir = path.join(fixtureRoot, 'reports');
  fs.mkdirSync(outDir, { recursive: true });
  const base = path.basename(stackPath).replace(/\.stack\.txt$|\.txt$/g, '');
  const jsonPath = path.resolve(args['out-json'] || path.join(outDir, `${base}.report.json`));
  const htmlPath = path.resolve(args['out-html'] || path.join(outDir, `${base}.report.html`));
  fs.writeFileSync(jsonPath, JSON.stringify(report, null, 2));
  fs.writeFileSync(htmlPath, render(report));

  const lines = [];
  lines.push(`报告已写入：`);
  lines.push(`  JSON: ${jsonPath}`);
  lines.push(`  HTML: ${htmlPath}`);
  lines.push(`帧统计：${JSON.stringify(report.summary.statusCounts)}`);
  for (const f of report.frames) {
    const loc = f.finalLocation
      ? `${f.finalLocation.identity}:${f.finalLocation.line1}:${f.finalLocation.column1}`
      : '-';
    lines.push(`  #${f.index} [${f.status}] ${f.functionName || '(anon)'} @ ${f.identity || f.file}:${f.generated.line1}:${f.generated.column1} -> ${loc}`);
  }
  process.stdout.write(lines.join('\n') + '\n');
}

function cmdVerify(args) {
  const fixtureRoot = path.resolve(args['fixture-root'] || process.cwd());
  const manifestPath = path.resolve(args.manifest || path.join(fixtureRoot, 'fixtures/manifest.json'));
  const manifest = loadManifest(manifestPath);
  const capture = JSON.parse(fs.readFileSync(path.join(fixtureRoot, 'fixtures/capture.json'), 'utf8'));
  const failures = [];

  for (const relBundle of ['assets/basic.cjs', 'assets/indexed.cjs']) {
    const stackFile = path.join(fixtureRoot, 'fixtures', path.basename(relBundle) + '.stack.txt');
    const expectedText = fs.readFileSync(stackFile, 'utf8');
    const expected = parseStack(expectedText.split(capture.root_alias || '<FIXTURE_ROOT>').join(fixtureRoot), fixtureRoot);

    const run = spawnSync(process.execPath, [path.join(fixtureRoot, relBundle)], {
      cwd: fixtureRoot,
      encoding: 'utf8',
    });
    const freshText = `${run.stdout || ''}${run.stderr || ''}`;
    const fresh = parseStack(freshText, fixtureRoot);

    const expSig = userFrameSignatures(expected.frames);
    const gotSig = userFrameSignatures(fresh.frames);
    const ok = run.status === capture.expected_exit_code && JSON.stringify(expSig) === JSON.stringify(gotSig);
    if (!ok) {
      failures.push({
        bundle: relBundle,
        exitCode: run.status,
        expectedExit: capture.expected_exit_code,
        expectedUserFrames: expSig,
        freshUserFrames: gotSig,
      });
    }
    process.stdout.write(`[${ok ? 'PASS' : 'FAIL'}] ${relBundle} exit=${run.status} 用户帧=${gotSig.length}\n`);
    // 归一化后的完整栈落盘，便于人工比对（内部帧差异可接受）。
    const normPath = path.join(fixtureRoot, 'reports', `${path.basename(relBundle)}.rerun.txt`);
    fs.mkdirSync(path.dirname(normPath), { recursive: true });
    fs.writeFileSync(normPath, normalizeRoot(freshText, fixtureRoot, capture.root_alias || '<FIXTURE_ROOT>'));
  }
  if (failures.length) {
    process.stdout.write(JSON.stringify(failures, null, 2) + '\n');
    process.exitCode = 1;
  } else {
    process.stdout.write('全部用户帧与采集记录一致（node: 内部帧版本差异已按 capture.json 约定忽略）。\n');
  }
}

function cmdSelftest() {
  const results = selftest.run();
  for (const r of results) process.stdout.write(`[PASS] ${r.name}\n`);
  process.stdout.write(`selftest 完成：${results.length} 组独立核验全部通过。\n`);
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const command = args._[0];
  try {
    if (command === 'symbolize') return cmdSymbolize(args);
    if (command === 'verify') return cmdVerify(args);
    if (command === 'selftest') return cmdSelftest();
    process.stdout.write(usage());
    if (command) process.exitCode = 1;
  } catch (err) {
    process.stderr.write(`错误：${err.message}\n`);
    process.exit(2);
  }
}

main();

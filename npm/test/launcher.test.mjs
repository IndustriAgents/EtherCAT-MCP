import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, writeFileSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
const cli = fileURLToPath(new URL('../bin/cli.mjs', import.meta.url));
const pkg = JSON.parse(readFileSync(new URL('../package.json', import.meta.url)));
test('version does not require uv or write to stderr', () => {
  const r = spawnSync(process.execPath, ['--',cli, '--version'], { encoding:'utf8', env:{...process.env, INDUSTRIAGENTS_UV:'/missing/uv'} });
  assert.equal(r.status,0); assert.equal(r.stdout, `${Object.keys(pkg.bin)[0]} ${pkg.version}\n`); assert.equal(r.stderr,'');
});
test('bundled wheel, argument boundaries, stdio and exit status are preserved', () => {
  const dir = mkdtempSync(join(tmpdir(),'mcp-launcher-'));
  try {
    const uv = join(dir,'uv');
    writeFileSync(uv, `#!/usr/bin/env -S node --\nprocess.stdout.write(JSON.stringify(process.argv.slice(2)));process.stderr.write('diagnostic');process.exit(23);\n`, {mode:0o755});
    const r = spawnSync(process.execPath,['--',cli,'--env-file','a file.env'],{encoding:'utf8',env:{...process.env,INDUSTRIAGENTS_UV:uv}});
    assert.equal(r.status,23); assert.equal(r.stderr,'diagnostic');
    const args=JSON.parse(r.stdout); assert.deepEqual(args.slice(0,4),['tool','run','--isolated','--from']);
    assert.ok(args[4].endsWith(`${pkg.version}-py3-none-any.whl`));
    assert.deepEqual(args.slice(5),[Object.keys(pkg.bin)[0],'--env-file','a file.env']);
  } finally { rmSync(dir,{recursive:true,force:true}); }
});
test('missing uv reports an actionable error only on stderr', () => {
  const r=spawnSync(process.execPath,['--',cli],{encoding:'utf8',env:{...process.env,INDUSTRIAGENTS_UV:'/missing/uv'}});
  assert.equal(r.status,1);assert.equal(r.stdout,'');assert.match(r.stderr,/uv is required/);
});

#!/usr/bin/env -S node --
import { spawn } from 'node:child_process';
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
const metadata = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'));
const command = Object.keys(metadata.bin)[0];
if (process.argv.slice(2).length === 1 && process.argv[2] === '--version') {
  process.stdout.write(`${command} ${metadata.version}\n`);
  process.exit(0);
}
const expected = `${metadata.name.replaceAll('-', '_')}-${metadata.version}-py3-none-any.whl`;
if (!existsSync(new URL(`../vendor/${expected}`, import.meta.url))) {
  process.stderr.write(`Missing bundled wheel ${expected}. Reinstall ${metadata.name}.\n`);
  process.exit(1);
}
const wheel = fileURLToPath(new URL(`../vendor/${expected}`, import.meta.url));
const child = spawn(process.env.INDUSTRIAGENTS_UV || 'uv', ['tool', 'run', '--isolated', '--from', wheel, command, ...process.argv.slice(2)], { stdio: 'inherit', windowsHide: true });
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => child.kill(signal));
child.on('error', error => {
  process.stderr.write(error.code === 'ENOENT' ? 'uv is required. Install it from https://docs.astral.sh/uv/getting-started/installation/ and retry.\n' : `Cannot start uv: ${error.message}\n`);
  process.exitCode = 1;
});
child.on('exit', (code, signal) => { process.exitCode = code ?? (signal === 'SIGINT' ? 130 : signal === 'SIGTERM' ? 143 : 1); });

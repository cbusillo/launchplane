import { readFileSync, mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { createRequire, stripTypeScriptTypes } from 'node:module';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
import vm from 'node:vm';

const fixture = dirname(fileURLToPath(import.meta.url));
const { parse } = createRequire(import.meta.url)('./dotenv/main.cjs');
const input = JSON.parse(readFileSync(0, 'utf8'));
const context = vm.createContext({
  parse, join, dirname,
  paths: () => ({ COMPOSE_PATH: input.root }),
  encodeBase64: value => Buffer.from(value).toString('base64'),
});
for (const file of ['prepare-environment.ts', 'create-env-file.ts']) {
  vm.runInContext(stripTypeScriptTypes(readFileSync(join(fixture, file), 'utf8'))
    .replaceAll('export const ', 'const '), context);
}
context.compose = {
  env: input.env, appName: 'synthetic', composePath: 'compose.json',
  environment: { env: '', project: { env: '' } },
};
mkdirSync(join(input.root, 'synthetic', 'code'), { recursive: true });
const command = vm.runInContext('getCreateEnvFileCommand(compose)', context);
execFileSync('sh', ['-c', command]);
process.stdout.write(JSON.stringify({
  generated: readFileSync(join(input.root, 'synthetic', 'code', '.env'), 'utf8'),
  stored: parse(input.env),
}));

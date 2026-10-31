import assert from 'node:assert/strict';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from '../frontend/node_modules/vite/dist/node/index.js';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const temporary = await mkdtemp(path.join(os.tmpdir(), 'keivotos-release-date-'));
try {
  const result = await build({
    root: path.join(root, 'frontend'), configFile: false, logLevel: 'silent',
    build: { write: false, minify: false, lib: { entry: path.join(root, 'frontend/src/lib/product.ts'), formats: ['es'] } },
  });
  const chunk = (Array.isArray(result) ? result[0] : result).output.find(item => item.type === 'chunk');
  const file = path.join(temporary, 'product.mjs');
  await writeFile(file, chunk.code);
  const { formatReleaseDate } = await import(pathToFileURL(file).href);
  const valid = [
    ['2027-01-15T10:20:00+09:00', '2027/01/15'],
    ['2027-01-15T14:59:59Z', '2027/01/15'],
    ['2027-01-15T15:00:00Z', '2027/01/16'],
    ['2026-12-31T15:00:00Z', '2027/01/01'],
    ['2028-02-29T23:30:00-05:00', '2028/03/01'],
    ['2027-01-15T20:43:17.123456-05:00', '2027/01/16'],
    ['2027-01-01T00:30:00+14:00', '2026/12/31'],
  ];
  for (const timezone of ['UTC', 'America/New_York', 'Asia/Seoul']) {
    process.env.TZ = timezone;
    for (const [input, expected] of valid) {
      assert.equal(formatReleaseDate(input), expected, `${timezone}: ${input}`);
      assert.match(formatReleaseDate(input), /^\d{4}\/\d{2}\/\d{2}$/);
    }
  }
  for (const value of ['', 'bad', '2027-01-15', '2027-01-15T10:20:00',
    '2027-02-29T12:00:00Z', '2027-04-31T12:00:00Z', '2027-01-15T24:00:00Z',
    '2027-01-15T12:60:00Z', '2027-01-15T12:00:60Z', '2027-01-15T12:00:00+24:00',
    '0000-01-01T00:00:00Z', '2027-01-15T12:00:00+09:60', '9999-12-31T23:59:59Z']) {
    assert.equal(formatReleaseDate(value), null, value);
  }
  console.log('PASS: publication date only, Seoul boundaries and host timezone independence');
} finally {
  await rm(temporary, { recursive: true, force: true });
}

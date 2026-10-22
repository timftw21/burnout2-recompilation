// Keep the two private build inputs in encrypted Actions environment secrets.
// Split below GitHub's 48 KiB per-secret limit; never upload a game artifact.
import {readFileSync, writeFileSync, mkdirSync} from 'node:fs';
import {gzipSync, gunzipSync} from 'node:zlib';
import {createHash} from 'node:crypto';
import {join, resolve} from 'node:path';

const names = ['default.xbe', 'b2distfx.bin'];
const prefix = 'B2_CI_INPUTS_';
const target = JSON.parse(readFileSync('knowledge/supported_target.json')).targets[0];
function verify(files) {
    const executable = files['default.xbe'];
    if (executable.length !== target.file_size ||
        createHash('sha256').update(executable).digest('hex').toUpperCase() !== target.file_sha256)
        throw new Error('CI executable does not match the supported target.');
    if (files['b2distfx.bin'].length !== 21756)
        throw new Error('CI effects image has the wrong size.');
}

if (process.argv[2] === 'pack' && process.argv.length === 5) {
    const files = Object.fromEntries(names.map(name => [name, readFileSync(join(process.argv[3], name))]));
    verify(files);
    const encoded = gzipSync(JSON.stringify(Object.fromEntries(
        names.map(name => [name, files[name].toString('base64')]))), {level: 9}).toString('base64');
    const chunks = encoded.match(/.{1,40960}/g);
    if (chunks.length > 100) throw new Error('CI inputs exceed the environment secret limit.');
    writeFileSync(process.argv[4], chunks.map((value, index) =>
        `${prefix}${String(index).padStart(2, '0')}=${value}\n`).join(''), {flag: 'wx', mode: 0o600});
    console.log(`Prepared ${chunks.length} encrypted-secret inputs in ${resolve(process.argv[4])}.`);
} else if (process.argv[2] === 'restore' && process.argv.length === 3) {
    const secrets = JSON.parse(readFileSync(0, 'utf8'));
    const keys = Object.keys(secrets).filter(key => /^B2_CI_INPUTS_\d{2}$/.test(key)).sort();
    if (!keys.length) throw new Error('The macos-game environment has no private build inputs.');
    if (keys.some((key, index) => key !== `${prefix}${String(index).padStart(2, '0')}`))
        throw new Error('A CI input secret is missing.');
    const decoded = JSON.parse(gunzipSync(Buffer.from(keys.map(key => secrets[key]).join(''), 'base64'),
        {maxOutputLength: 8 * 1024 * 1024}));
    const files = Object.fromEntries(names.map(name => [name, Buffer.from(decoded[name], 'base64')]));
    verify(files);
    mkdirSync('data/local', {recursive: true});
    for (const name of names) writeFileSync(join('data/local', name), files[name], {flag: 'wx', mode: 0o600});
    console.log('Restored the supported executable and effects image to ignored data/local.');
} else {
    throw new Error('Usage: node cmake/ci-game-inputs.mjs pack INPUT_DIRECTORY OUTPUT.env | restore < secrets.json');
}

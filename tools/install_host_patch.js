'use strict';
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const APP = 'kxrxh_doodle_jump';
const VERSION = '0.14.1';
const OAP = '/var/opt/public/oap';
const FILES = [
    ['doodle-host', 'data/engine/doodle-host'],
    ['app.js', 'source/javascript/app.js'],
    ['app-spec.json', 'source/javascript/kit/app-spec.json'],
    ['application.xml', 'rhmiData/uiDescription/application.xml', 'source/rhmiData/uiDescriptions/ID7/application.xml'],
    ['uiDescription.json', 'rhmiData/uiDescription/uiDescription.json', 'source/rhmiData/uiDescriptions/ID7/uiDescription.json'],
];

function read(file, limit) {
    const fd = fs.openSync(file, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
    try {
        const stat = fs.fstatSync(fd);
        if (!stat.isFile() || stat.size > limit) throw new Error('Invalid file ' + file);
        const data = Buffer.alloc(stat.size);
        let offset = 0;
        while (offset < data.length) {
            const size = fs.readSync(fd, data, offset, data.length - offset, null);
            if (!size) throw new Error('Incomplete read of ' + file);
            offset += size;
        }
        return {data, stat};
    } finally {
        fs.closeSync(fd);
    }
}

function json(file) {
    return JSON.parse(read(file, 262144).data.toString('utf8'));
}

function digest(data) {
    return crypto.createHash('sha256').update(data).digest('hex');
}

function fileDigest(name, data) {
    return digest(name === 'strings.json' ? JSON.stringify(JSON.parse(data.toString('utf8'))) : data);
}

function installedHome() {
    const entry = json(path.join(OAP, 'oap_12201', 'data', 'apps', APP + '.json'));
    const uid = String(entry.userId);
    if (!/^[1-9][0-9]{0,8}$/.test(uid)) throw new Error('Invalid app registration');
    return path.join(OAP, 'oap_' + uid);
}

function replace(target, bytes, owner) {
    const mode = owner.mode & 0o777;
    const directory = fs.mkdtempSync(path.join(path.dirname(target), '.doodle-patch-'));
    const staged = path.join(directory, 'replacement');
    try {
        const fd = fs.openSync(staged, 'wx', mode);
        try {
            fs.writeFileSync(fd, bytes);
            const stat = fs.fstatSync(fd);
            if (stat.uid !== owner.uid || stat.gid !== owner.gid) fs.fchownSync(fd, owner.uid, owner.gid);
            fs.fchmodSync(fd, mode);
            fs.fsyncSync(fd);
        } finally {
            fs.closeSync(fd);
        }
        fs.renameSync(staged, target);
    } finally {
        try { fs.unlinkSync(staged); } catch (error) { if (error.code !== 'ENOENT') throw error; }
        fs.rmdirSync(directory);
    }
}

function prepare(home, targetPath, name, hashes, rollback) {
    const target = path.join(home, targetPath);
    if (fs.realpathSync(target) !== target) throw new Error('Unexpected file location ' + targetPath);
    const backup = target + '.v' + VERSION;
    const limit = 4 << 20;
    const current = read(target, limit);
    const hash = fileDigest(name, current.data);
    const wanted = rollback ? hashes.original : hashes.updated;
    if (hash === wanted) return null;
    if (hash !== hashes.original && hash !== hashes.updated && hash !== hashes.previous)
        throw new Error('Installed file differs from the verified release: ' + targetPath);
    const replacement = read(rollback ? backup : path.join(__dirname, name), limit).data;
    if (fileDigest(name, replacement) !== wanted) throw new Error('Replacement failed SHA-256 verification: ' + targetPath);
    return {name, target, backup, current, replacement, original: hashes.original, needsBackup: hash === hashes.original};
}

function main(args) {
    const rollback = args[0] === '--rollback';
    if (rollback) args.shift();
    if (args.length > 1 || (args[0] && args[0][0] === '-'))
        throw new Error('Usage: node install.js [--rollback] [app-home]');
    const patch = json(path.join(__dirname, 'patch.json'));
    const home = fs.realpathSync(args[0] || installedHome());
    const app = json(path.join(home, 'source', 'package.json'));
    if (app.name !== APP || app.version !== VERSION) throw new Error('Requires an existing Doodle Jump v' + VERSION + ' install');
    const engine = path.join(home, 'data', 'engine');
    if (fs.realpathSync(engine) !== engine || read(path.join(engine, '.sha256'), 64).data.toString('ascii') !== patch.engine_sha256)
        throw new Error('Expected the v' + VERSION + ' engine cache');

    const changes = [];
    const files = FILES.slice();
    for (const directory of ['rhmiData/strings', 'source/rhmiData/variantGroups/Common/texts']) {
        if (!fs.existsSync(path.join(home, directory)) && directory.indexOf('source/') === 0) continue;
        for (const file of fs.readdirSync(path.join(home, directory))) {
            if (/^[a-z]{2}_[A-Z]{2}\.json$/.test(file)) files.push(['strings.json', path.join(directory, file)]);
        }
    }
    if (!files.some(([name, target]) => name === 'strings.json' && target.indexOf('rhmiData/strings/') === 0))
        throw new Error('Expected the installed iDrive 7 string bundle');

    for (const [name, installed, source] of files) {
        const targets = [installed];
        if (source && fs.existsSync(path.join(home, source))) targets.push(source);
        for (const target of targets) {
            const change = prepare(home, target, name, patch.files[name], rollback);
            if (change) changes.push(change);
        }
    }
    if (!rollback) {
        for (const {name, target, backup, original, needsBackup} of changes) {
            if (needsBackup) {
                try { fs.linkSync(target, backup); } catch (error) { if (error.code !== 'EEXIST') throw error; }
            }
            if (fileDigest(name, read(backup, 4 << 20).data) !== original)
                throw new Error('Backup failed SHA-256 verification: ' + target);
        }
    }
    for (const {target, replacement, current} of changes) replace(target, replacement, current.stat);
    console.log(rollback ? 'Original app files restored.' : 'App patch installed. Reopen the game.');
}

try {
    main(process.argv.slice(2));
} catch (error) {
    console.error('App update failed: ' + error.message);
    process.exitCode = 1;
}

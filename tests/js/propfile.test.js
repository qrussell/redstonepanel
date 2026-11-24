// The Files tab's Visual Editor (1.21.7). Run: node --test tests/js
const test = require('node:test');
const assert = require('node:assert');
const PF = require('../../static/js/propfile.js');

const JAVA = '#Minecraft server properties\n#Mon Nov 23 12:00:00 UTC 2026\naccepts-transfers=false\ndifficulty=easy\n' +
    'level-seed=-1234567890123456789\nmotd=A \\u00e9 Server\\: hi\\!\nrcon.password=a\\:b\\!\nserver-port=25565\nresource-pack=\n';
const BEDROCK = 'server-name=Dedicated Server\r\n# Used as the server name\r\n# Allowed values: Any string without semicolon symbol.\r\n\r\n' +
    'gamemode=survival\r\n# Sets the game mode for new players.\r\n\r\nallow-cheats=false\r\nserver-port=19132\r\n';
const map = p => Object.fromEntries(p.entries.map(e => [e.key, e.value]));

test('a Java server.properties is read the way Java reads it', () => {
    const p = PF.parse(JAVA, 'server.properties');
    assert.ok(p.ok && p.java);
    assert.deepStrictEqual(p.entries.map(e => e.key), ['accepts-transfers', 'difficulty', 'level-seed', 'motd', 'rcon.password', 'server-port', 'resource-pack']);
    assert.strictEqual(map(p).motd, 'A é Server: hi!');
    assert.strictEqual(map(p)['rcon.password'], 'a:b!');
    assert.strictEqual(map(p)['resource-pack'], '');
    assert.strictEqual(p.entries[0].line, 2);
});

test('changing one value leaves every other byte alone', () => {
    const p = PF.parse(JAVA, 'server.properties');
    const port = p.entries.find(e => e.key === 'server-port');
    const out = PF.setValue(JAVA, port.line, '25570', 'server.properties');
    assert.strictEqual(out, JAVA.replace('server-port=25565', 'server-port=25570'));
    // setting a value to what it already is changes nothing, escapes and all
    for (const e of p.entries.filter(e => e.key !== 'motd')) assert.strictEqual(PF.setValue(JAVA, e.line, e.value, 'server.properties'), JAVA);
});

test('Java values are escaped as the server writes them', () => {
    const p = PF.parse(JAVA, 'server.properties');
    const line = p.entries.find(e => e.key === 'rcon.password').line;
    const out = PF.setValue(JAVA, line, ' p=1:2#3!\\', 'server.properties');
    assert.ok(out.includes('rcon.password=\\ p\\=1\\:2\\#3\\!\\\\\n'));
    assert.strictEqual(map(PF.parse(out, 'server.properties'))['rcon.password'], ' p=1:2#3!\\');
});

test('Bedrock: plain values, comments and Windows line endings kept', () => {
    const p = PF.parse(BEDROCK, 'server.properties.bak');   // not .properties, so plain key=value
    assert.ok(p.ok && !p.java);
    assert.deepStrictEqual(map(p), { 'server-name': 'Dedicated Server', gamemode: 'survival', 'allow-cheats': 'false', 'server-port': '19132' });
    const out = PF.setValue(BEDROCK, p.entries[1].line, 'creative', 'server.properties.bak');
    assert.strictEqual(out, BEDROCK.replace('gamemode=survival\r\n', 'gamemode=creative\r\n'));
});

test('spacing around = and at the line end is kept', () => {
    const text = '  view-distance =  10  \n; a comment\n// another\nname = My Server # note\n';
    const p = PF.parse(text, 'plugin.cfg');
    assert.deepStrictEqual(map(p), { 'view-distance': '10', name: 'My Server # note' });
    assert.strictEqual(PF.setValue(text, 0, '12', 'plugin.cfg'), text.replace('=  10  ', '=  12  '));
    assert.strictEqual(PF.setValue(text, 3, 'a\nb', 'plugin.cfg').split('\n')[3], 'name = a b');
    assert.strictEqual(PF.setValue(text, 1, 'x', 'plugin.cfg'), text);      // a comment line is never written
    assert.strictEqual(PF.setValue(text, 99, 'x', 'plugin.cfg'), text);
});

test('sections, and the same name in two sections', () => {
    const text = '[general]\nenabled=true\n\n[database]\nenabled=false\nhost=localhost\n';
    const p = PF.parse(text, 'config.ini');
    assert.deepStrictEqual(p.sections, [{ line: 0, name: 'general' }, { line: 3, name: 'database' }]);
    assert.deepStrictEqual(p.entries.map(e => `${e.section}.${e.key}=${e.value}`), ['general.enabled=true', 'database.enabled=false', 'database.host=localhost']);
    assert.strictEqual(PF.setValue(text, 4, 'true', 'config.ini'), text.replace('enabled=false', 'enabled=true'));
});

test('small files count: eula.txt', () => {
    const p = PF.parse('#By changing the setting below to TRUE you agree\neula=false\n', 'eula.txt');
    assert.ok(p.ok);
    assert.deepStrictEqual(map(p), { eula: 'false' });
});

test('anything else is not offered, with the line that does not fit', () => {
    assert.deepStrictEqual(PF.parse('a=1\nthis is a sentence\n', 'notes.txt'), { ok: false, line: 2, reason: 'it has no key=value' });
    assert.strictEqual(PF.parse('my key=1\n', 'x.cfg').line, 1);
    assert.strictEqual(PF.parse('# only comments\n\n', 'x.cfg').ok, false);
    assert.strictEqual(PF.parse('', 'new.properties').ok, false);
    assert.strictEqual(PF.parse('a=1\\\n  b\n', 'x.properties').reason, 'its value continues on the next line');
    assert.strictEqual(PF.parse('key: value\n', 'x.properties').ok, false);
    assert.strictEqual(PF.parse('[general]\na=1\n', 'x.properties').ok, false);   // Java has no sections
    for (const n of ['config.yml', 'ops.json', 'forge.toml', 'latest.log', 'start.sh', 'plugins/X/config.YML']) {
        assert.strictEqual(PF.parse('a=1\n', n).ok, false, n);
    }
    assert.strictEqual(PF.parse(Array.from({ length: 2001 }, (_, i) => `k${i}=1`).join('\n'), 'big.cfg').ok, false);
});

test('each value gets the control Configuration uses', () => {
    assert.deepStrictEqual(PF.widget('pvp', 'true'), { type: 'bool', options: ['true', 'false'] });
    assert.deepStrictEqual(PF.widget('eula', 'TRUE'), { type: 'bool', options: ['TRUE', 'FALSE'] });
    assert.deepStrictEqual(PF.widget('x', 'False'), { type: 'bool', options: ['True', 'False'] });
    assert.deepStrictEqual(PF.widget('difficulty', 'easy').options, ['peaceful', 'easy', 'normal', 'hard']);
    assert.strictEqual(PF.widget('difficulty', '2').type, 'number');        // an old numeric value stays editable
    assert.strictEqual(PF.widget('gamemode', 'Survival').type, 'text');
    assert.strictEqual(PF.widget('server-port', '25565').type, 'number');
    assert.strictEqual(PF.widget('scale', '-0.5').type, 'number');
    for (const v of ['', '1e5', '0x10', '1.2.3', '192.168.1.1', ' 5']) assert.strictEqual(PF.widget('k', v).type, 'text', v);
});

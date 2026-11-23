// Live Chat's suggestions and command form (1.21.4). Run: node --test tests/js
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const CC = require('../../static/js/chat-commands.js');

const catalog = JSON.parse(fs.readFileSync(path.join(__dirname, '../../static/commands.json'), 'utf8'));
const env = (over = {}) => ({ catalog, edition: 'java', minecraft: '1.21.11', online: ['Steve', 'Alex', 'stevie99'],
                              known: ['Notch', 'Stella'], commands: [], gamerules: [], ...over });
const labels = s => s.items.map(i => i.label);
const type = (text, e) => CC.suggest(text, text.length, e || env());
const active = s => s.hint.parts.filter(p => p.active).map(p => p.text);

test('a slash lists commands, and letters narrow them', () => {
    assert.ok(labels(type('/')).includes('/gamemode'));
    assert.deepStrictEqual(labels(type('/ga')), ['/gamemode', '/gamerule']);
    assert.deepStrictEqual(labels(type('/GA')), ['/gamemode', '/gamerule']);
    assert.strictEqual(type('/ga').items[0].detail, "Change a player's game mode");
});

test('each command is listed once, with its second words', () => {
    const s = type('/ti');
    assert.deepStrictEqual(labels(s), ['/time']);
    assert.match(s.items[0].detail, /set, add/);
    assert.deepStrictEqual(labels(type('/time ')), ['set', 'add']);
    assert.deepStrictEqual(labels(type('/time s')), ['set']);
});

test('only the commands of the server\'s edition', () => {
    assert.ok(labels(type('/')).includes('/whitelist'));
    assert.ok(!labels(type('/')).includes('/allowlist'));
    const bedrock = labels(type('/', env({ edition: 'bedrock' })));
    assert.ok(bedrock.includes('/allowlist') && !bedrock.includes('/whitelist') && !bedrock.includes('/ban'));
});

test('after the command: the values it takes, then players', () => {
    let s = type('/gamemode ');
    assert.deepStrictEqual(labels(s), ['survival', 'creative', 'adventure', 'spectator']);
    assert.deepStrictEqual(active(s), ['<mode>']);
    s = type('/gamemode creative ');
    assert.deepStrictEqual(labels(s), ['@a', '@r', 'Steve', 'Alex', 'stevie99', 'Notch', 'Stella']);
    assert.deepStrictEqual(active(s), ['<player>']);
    s = type('/gamemode creative st');
    assert.deepStrictEqual(labels(s), ['Steve', 'stevie99', 'Stella']);
    assert.deepStrictEqual(s.items.map(i => i.detail), ['online', 'online', 'seen before']);
    assert.strictEqual(type('/gamemode creative Steve ').hint.done, true);
});

test('kick offers only players who are online', () => {
    assert.deepStrictEqual(labels(type('/kick ')), ['Steve', 'Alex', 'stevie99']);
    assert.deepStrictEqual(labels(type('/kick Steve being rude')), []);
    assert.deepStrictEqual(active(type('/kick Steve being rude')), ['[reason]']);
});

test('tp goes to a player or to three numbers', () => {
    let s = type('/tp Steve ');
    assert.deepStrictEqual(labels(s), ['Steve', 'Alex', 'stevie99', 'Notch', 'Stella']);
    assert.deepStrictEqual(active(s), ['<player or x y z>']);
    s = type('/tp Steve 100 64 ');
    assert.deepStrictEqual(labels(s), []);
    assert.deepStrictEqual(active(s), ['<player or x y z>']);
    assert.strictEqual(type('/tp Steve 100 64 -20 ').hint.done, true);
    assert.strictEqual(type('/tp Steve Alex ').hint.done, true);
});

test('game rules: the server\'s own, else by Minecraft version', () => {
    assert.ok(labels(type('/gamerule k')).includes('keep_inventory'));
    assert.ok(labels(type('/gamerule k', env({ minecraft: '1.21.10' }))).includes('keepInventory'));
    assert.ok(labels(type('/gamerule k', env({ minecraft: '26.3' }))).includes('keep_inventory'));
    assert.ok(labels(type('/gamerule k', env({ minecraft: '1.20.4' }))).includes('keepInventory'));
    assert.deepStrictEqual(labels(type('/gamerule ', env({ gamerules: ['modRule', 'pvp'] }))), ['modRule', 'pvp']);
    assert.deepStrictEqual(labels(type('/gamerule keep_inventory ')), ['true', 'false']);
});

test('commands only the server knows are listed with its usage', () => {
    const e = env({ commands: [{ name: 'tick', usage: '(query|rate)' }, { name: 'gamemode', usage: '<gamemode>' }] });
    const s = type('/ti', e);
    assert.deepStrictEqual(labels(s), ['/time', '/tick']);
    assert.strictEqual(s.items[1].detail, '(query|rate)');
    assert.strictEqual(labels(type('/gamem', e)).length, 1);
    const u = type('/tick ra', e);
    assert.strictEqual(u.hint.parts[0].text, '/tick (query|rate)');
    assert.deepStrictEqual(labels(type('/tick St', e)), ['Steve', 'stevie99', 'Stella']);
});

test('picking a suggestion replaces the word being typed', () => {
    let text = '/gamem';
    let done = CC.apply(text, type(text), type(text).items[0]);
    assert.deepStrictEqual(done, { text: '/gamemode ', caret: 10 });
    text = '/tp st Alex';
    const s = CC.suggest(text, 6, env());
    assert.deepStrictEqual(labels(s), ['Steve', 'stevie99', 'Stella']);
    assert.deepStrictEqual(CC.apply(text, s, s.items[0]), { text: '/tp Steve Alex', caret: 10 });
});

test('Bedrock names with spaces are quoted in commands', () => {
    const e = env({ edition: 'bedrock', online: ['Steve Smith', 'Alex'] });
    let s = type('/kick ', e);
    assert.strictEqual(s.items[0].insert, '"Steve Smith"');
    s = type('/kick "Ste', e);
    assert.deepStrictEqual(labels(s), ['Steve Smith']);
    assert.strictEqual(CC.apply('/kick "Ste', s, s.items[0]).text, '/kick "Steve Smith" ');
    s = type('/tp "Steve Smith" ', e);
    assert.deepStrictEqual(active(s), ['<player or x y z>']);
});

test('a plain message completes online players only', () => {
    let s = type('hello St');
    assert.deepStrictEqual(labels(s), ['Steve', 'stevie99']);
    assert.strictEqual(CC.apply('hello St', s, s.items[0]).text, 'hello Steve ');
    assert.deepStrictEqual(labels(type('hello S')), []);
    assert.deepStrictEqual(labels(type('hello No')), []);          // Notch is not online
    assert.deepStrictEqual(labels(type('hi Alex')), []);           // already whole
    assert.strictEqual(type('hello').hint, null);
});

test('the form builds the command and says what is missing', () => {
    const cmd = name => catalog.commands.find(c => c.name === name && c.editions.includes('java'));
    assert.deepStrictEqual(CC.build(cmd('gamemode'), { mode: 'creative', player: 'Steve' }),
                           { command: '/gamemode creative Steve', missing: [], error: null });
    assert.deepStrictEqual(CC.build(cmd('gamemode'), { mode: 'creative' }).missing, ['player']);
    assert.strictEqual(CC.build(cmd('tp'), { player: 'Steve', destination: '100 64 -20' }).command, '/tp Steve 100 64 -20');
    assert.match(CC.build(cmd('tp'), { player: 'Steve', destination: '100 64' }).error, /three numbers/);
    assert.strictEqual(CC.build(cmd('kick'), { player: 'Steve', reason: '  being   rude ' }).command, '/kick Steve being rude');
    assert.strictEqual(CC.build(cmd('kick'), { player: 'Steve' }).command, '/kick Steve');
    assert.strictEqual(CC.build(cmd('give'), { player: '@a', item: 'diamond', count: '5' }).command, '/give @a diamond 5');
    assert.match(CC.build(cmd('give'), { player: '@a', item: 'two words' }).error, /space/);
    assert.match(CC.build(cmd('effect give'), { player: 'Steve', effect: 'speed', level: '2' }).error, /seconds/);
    assert.strictEqual(CC.build(cmd('list'), {}).command, '/list');
    const kickB = catalog.commands.find(c => c.name === 'kick');
    assert.strictEqual(CC.build(kickB, { player: 'Steve Smith' }).command, '/kick "Steve Smith"');
});

test('usage lines', () => {
    const cmd = catalog.commands.find(c => c.name === 'tp');
    assert.strictEqual(CC.usage(cmd), '/tp <player> <player or x y z>');
    assert.strictEqual(CC.usage(catalog.commands.find(c => c.name === 'kick')), '/kick <player> [reason]');
});

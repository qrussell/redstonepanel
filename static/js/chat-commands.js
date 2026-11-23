// Live Chat's command help (1.21.4): suggestions while typing, like the game's own chat, and
// the pieces the Commands list needs to build a command from a form.
//
// Everything here is plain data in, plain data out (no page, no network), so it can be tested
// with Node (tests/js/chat-commands.test.js). app.js draws the list and the form.
//
// The commands come from static/commands.json. An "env" describes one server:
//   { catalog, edition: 'java'|'bedrock', minecraft: '1.21.11'|null,
//     online: [names], known: [names], commands: [{name, usage}], gamerules: [names] }
// online/known/commands/gamerules come from GET /api/servers/<name>/chat/assist.
const ChatCommands = {
    MAX_ITEMS: 30,
    SELECTORS: [['@a', 'everyone online'], ['@r', 'a random player']],

    // --- reading what was typed ---

    // Words with their places in the text. A quoted Bedrock name ("Steve Smith") is one word.
    tokenize(text) {
        const tokens = [];
        let i = 0;
        while (i < text.length) {
            if (text[i] === ' ') { i++; continue; }
            const start = i;
            if (text[i] === '"') {
                i++;
                while (i < text.length && text[i] !== '"') i++;
                if (i < text.length) i++;
            } else {
                while (i < text.length && text[i] !== ' ') i++;
            }
            tokens.push({ text: text.slice(start, i), start, end: i });
        }
        return tokens;
    },

    unquote(word) {
        return word.replace(/^"/, '').replace(/"$/, '');
    },

    // A name as it must be typed: Bedrock gamertags with spaces need quotes.
    quoteName(name, isCommand) {
        return isCommand && /\s/.test(name) ? `"${name}"` : name;
    },

    // --- the command list for one server ---

    forEdition(env) {
        return ((env.catalog && env.catalog.commands) || []).filter(c => c.editions.includes(env.edition));
    },

    // Is this Minecraft version 1.21.11 or newer (26.x counts)? Game rules were renamed there.
    newGameRules(minecraft) {
        const m = /^(\d+)\.(\d+)(?:\.(\d+))?/.exec(minecraft || '');
        if (!m) return true;                       // unknown: assume a current server
        const [a, b, c] = [Number(m[1]), Number(m[2]), Number(m[3] || 0)];
        if (a !== 1) return a > 1;
        return b > 21 || (b === 21 && c >= 11);
    },

    listFor(arg, env) {
        if (arg.choices) return arg.choices;
        const lists = (env.catalog && env.catalog.lists) || {};
        if (arg.list === 'gamerules') {
            if (env.gamerules && env.gamerules.length) return env.gamerules;   // the server's own
            if (env.edition === 'bedrock') return lists.gamerules_bedrock || [];
            return (this.newGameRules(env.minecraft) ? lists.gamerules_java : lists.gamerules_java_old) || [];
        }
        return lists[arg.list] || [];
    },

    argLabel(arg) {
        const inner = arg.type === 'position' ? 'x y z' : arg.type === 'destination' ? 'player or x y z' : arg.name;
        return arg.optional ? `[${inner}]` : `<${inner}>`;
    },

    usage(cmd) {
        return ['/' + cmd.name].concat(cmd.args.map(a => this.argLabel(a))).join(' ');
    },

    looksLikeCoordinate(word) {
        return /^[~^]?-?\d*\.?\d*$/.test(word) && word !== '';
    },

    // One entry per word the command still expects, given the words typed after its name:
    // a position is three words, a destination is one (a player) or three (x y z).
    slots(cmd, words, partial) {
        const out = [];
        let w = 0;
        for (const arg of cmd.args) {
            if (arg.type === 'text') { out.push({ arg, part: null, greedy: true }); break; }
            let coords = arg.type === 'position';
            if (arg.type === 'destination') {
                const seen = w < words.length ? words[w] : (w === words.length ? partial : '');
                coords = this.looksLikeCoordinate(seen || '');
            }
            if (coords) {
                ['x', 'y', 'z'].forEach(part => out.push({ arg, part }));
                w += 3;
            } else {
                out.push({ arg, part: null });
                w += 1;
            }
        }
        return out;
    },

    // --- suggestions ---

    startsWith(candidate, typed) {
        return candidate.toLowerCase().startsWith(typed.toLowerCase());
    },

    playerItems(env, typed, isCommand, opts = {}) {
        const t = this.unquote(typed);
        const items = [];
        if (opts.selectors && (t === '' || t.startsWith('@'))) {
            this.SELECTORS.forEach(([sel, what]) => {
                if (this.startsWith(sel, t)) items.push({ label: sel, insert: sel, detail: what, kind: 'selector' });
            });
        }
        (env.online || []).forEach(n => {
            if (this.startsWith(n, t)) items.push({ label: n, insert: this.quoteName(n, isCommand), detail: 'online', kind: 'player' });
        });
        if (!opts.onlineOnly) {
            (env.known || []).forEach(n => {
                if (this.startsWith(n, t)) items.push({ label: n, insert: this.quoteName(n, isCommand), detail: 'seen before', kind: 'player' });
            });
        }
        return items;
    },

    slotItems(slot, env, typed) {
        const arg = slot.arg;
        switch (arg.type) {
            case 'player': return this.playerItems(env, typed, true, { onlineOnly: !!arg.online });
            case 'target': return this.playerItems(env, typed, true, { selectors: true });
            case 'destination': return slot.part ? [] : this.playerItems(env, typed, true);
            case 'choice':
            case 'suggest':
                return this.listFor(arg, env).filter(c => this.startsWith(c, typed))
                    .map(c => ({ label: c, insert: c, detail: arg.name, kind: 'value' }));
            default: return [];
        }
    },

    // What to show for `text` with the cursor at `caret`.
    // Returns { items: [{label, insert, detail, kind}], replace: {start, end}, hint: {parts: [{text, active}]}|null }
    suggest(text, caret, env) {
        if (caret === undefined || caret === null) caret = text.length;
        const before = text.slice(0, caret);
        const none = { items: [], replace: { start: caret, end: caret }, hint: null };
        if (!before.startsWith('/')) return this.suggestInMessage(before, caret, env, none);

        const tokens = this.tokenize(before.slice(1)).map(t => ({ text: t.text, start: t.start + 1, end: t.end + 1 }));
        const typing = tokens.length && tokens[tokens.length - 1].end === caret ? tokens.pop() : null;
        const partial = typing ? typing.text : '';
        const replace = typing ? { start: typing.start, end: caret } : { start: caret, end: caret };
        const words = tokens.map(t => t.text);
        const lower = words.map(w => w.toLowerCase());
        const all = this.forEdition(env);

        // Catalogue commands whose name agrees with every finished word so far
        const agree = all.filter(c => {
            const name = c.name.split(' ');
            return name.every((n, i) => i >= lower.length || n === lower[i]);
        });
        const complete = agree.filter(c => c.name.split(' ').length <= lower.length)
            .sort((a, b) => b.name.split(' ').length - a.name.split(' ').length)[0];
        const longer = agree.filter(c => c.name.split(' ').length > lower.length);

        // Still on the command's name (or the next word of a two-word one, like "time set")
        if (!complete || longer.length) {
            const items = [];
            const seen = new Set();
            longer.forEach(c => {
                const name = c.name.split(' ');
                const word = name[lower.length];
                if (!this.startsWith(word, partial)) return;
                if (seen.has(word)) {
                    const it = items.find(i => i.insert === word);
                    if (it && it.more) it.detail = it.more;
                    return;
                }
                seen.add(word);
                const last = name.length === lower.length + 1;
                const family = longer.filter(o => o.name.split(' ')[lower.length] === word)
                    .map(o => o.name.split(' ')[lower.length + 1]).filter(Boolean);
                items.push({ label: (lower.length ? '' : '/') + word, insert: word, kind: 'command',
                             detail: last ? c.about : `${word} ${[...new Set(family)].join(', ')}…`,
                             more: `${word} ${[...new Set(family)].join(', ')}…` });
            });
            if (!lower.length) {
                // Commands this server has that the list above doesn't describe (mods, the rest of vanilla)
                (env.commands || []).forEach(c => {
                    const n = c.name.toLowerCase();
                    if (seen.has(n) || !this.startsWith(n, partial)) return;
                    seen.add(n);
                    items.push({ label: '/' + c.name, insert: c.name, detail: c.usage || '', kind: 'server-command' });
                });
            }
            items.forEach(i => delete i.more);
            if (items.length || !complete) {
                if (!items.length && lower.length) return this.unknownCommand(words, partial, replace, env, none);
                return { items: items.slice(0, this.MAX_ITEMS), replace, hint: null };
            }
        }

        // On the command's arguments
        const nameLen = complete.name.split(' ').length;
        const argWords = words.slice(nameLen);
        const slots = this.slots(complete, argWords, partial);
        const at = Math.min(argWords.length, slots.length);
        const slot = slots[argWords.length] || (slots.length && slots[slots.length - 1].greedy ? slots[slots.length - 1] : null);
        const parts = [{ text: '/' + complete.name, active: false }];
        complete.args.forEach(a => parts.push({ text: this.argLabel(a), active: !!slot && slot.arg === a }));
        const hint = { parts, about: complete.about, done: !slot && at >= slots.length };
        const items = slot && !slot.greedy ? this.slotItems(slot, env, partial) : [];
        return { items: items.slice(0, this.MAX_ITEMS), replace, hint };
    },

    // A command the list doesn't describe: show the server's own usage line, and still offer
    // player names, since most commands that take a word take a player.
    unknownCommand(words, partial, replace, env, none) {
        const known = (env.commands || []).find(c => c.name.toLowerCase() === words[0].toLowerCase());
        const hint = known && known.usage ? { parts: [{ text: `/${known.name} ${known.usage}`, active: false }], about: '' } : null;
        const items = partial ? this.playerItems(env, partial, true, { selectors: true }) : [];
        return { items: items.slice(0, this.MAX_ITEMS), replace, hint };
    },

    // A plain message: offer online players once two letters of a name are typed (Tab fills it in)
    suggestInMessage(before, caret, env, none) {
        const m = /(^|\s)(@?)([A-Za-z0-9_]{2,})$/.exec(before);
        if (!m) return none;
        const items = (env.online || []).filter(n => this.startsWith(n, m[3]) && n.toLowerCase() !== m[3].toLowerCase())
            .map(n => ({ label: n, insert: n, detail: 'online', kind: 'player' }));
        if (!items.length) return none;
        return { items: items.slice(0, this.MAX_ITEMS), replace: { start: caret - m[3].length - m[2].length, end: caret }, hint: null };
    },

    // Puts a picked suggestion into the text. Returns { text, caret }.
    apply(text, suggestion, item) {
        const head = text.slice(0, suggestion.replace.start) + item.insert;
        let tail = text.slice(suggestion.replace.end);
        if (!tail.startsWith(' ')) tail = ' ' + tail;
        return { text: head + tail, caret: head.length + 1 };
    },

    // --- the Commands form ---

    // The command line for a form's values, or what is still missing.
    // values: { argName: 'text' } with a position as 'x y z'. Returns { command, missing: [names], error }
    build(cmd, values) {
        const words = [cmd.name];
        const missing = [];
        let gap = null, error = null;
        cmd.args.forEach(arg => {
            let v = String(values[arg.name] ?? '').trim().replace(/\s+/g, ' ');
            if (arg.type === 'position' || (arg.type === 'destination' && this.looksLikeCoordinate(v.split(' ')[0] || ''))) {
                const xyz = v.split(' ').filter(Boolean);
                if (xyz.length && (xyz.length !== 3 || !xyz.every(p => this.looksLikeCoordinate(p)))) {
                    error = `${arg.name} needs three numbers: x y z.`;
                    return;
                }
            } else if (v && ['player', 'target', 'destination'].includes(arg.type)) {
                v = this.unquote(v);
                v = this.quoteName(v, true);
            } else if (v && arg.type !== 'text' && /\s/.test(v)) {
                error = `${arg.name} can't contain a space.`;
                return;
            }
            if (!v) {
                if (!arg.optional) missing.push(arg.name);
                else if (gap === null) gap = arg.name;
                return;
            }
            if (gap !== null && !error) error = `Fill in ${gap} too, or leave ${arg.name} empty.`;
            words.push(v);
        });
        return { command: '/' + words.join(' '), missing, error };
    },
};

if (typeof module !== 'undefined') module.exports = ChatCommands;

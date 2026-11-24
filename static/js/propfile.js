// The Files tab's Visual Editor (1.21.7): decides whether a text file is a list of key=value
// settings, reads its entries, and changes one value in the text without touching anything else
// (comments, blank lines, order, spacing and line endings stay exactly as they were).
//
// Everything here is plain data in, plain data out (no page, no network), so it can be tested
// with Node (tests/js/propfile.test.js). app.js draws the form; the text stays the one copy of
// the file, which is what keeps the Visual Editor and Raw Text in step.
//
// Files ending in .properties are read the way Java reads them (escapes such as \: and é,
// as core/properties.py does for server.properties). Anything else is plain key=value, with
// [section] headings allowed, which covers Bedrock's server.properties, eula.txt and most
// .cfg/.conf/.ini files.
const PropFile = {
    MAX_ENTRIES: 2000,
    // Formats with their own structure: a form of flat text boxes would let people break them.
    OTHER_FORMATS: ['json', 'json5', 'yml', 'yaml', 'toml', 'xml', 'html', 'htm', 'md', 'log', 'csv', 'tsv',
                    'sh', 'bash', 'bat', 'cmd', 'ps1', 'py', 'js', 'ts', 'java', 'kts', 'gradle', 'sk', 'zs',
                    'mcfunction', 'mcmeta', 'nbt', 'snbt', 'sql', 'service', 'timer'],
    ENUMS: {
        'difficulty': ['peaceful', 'easy', 'normal', 'hard'],
        'gamemode': ['survival', 'creative', 'adventure', 'spectator'],
    },
    KEY: /^[A-Za-z0-9_][A-Za-z0-9_.\-]*$/,

    extension(name) {
        const base = String(name || '').split('/').pop();
        const dot = base.lastIndexOf('.');
        return dot > 0 ? base.slice(dot + 1).toLowerCase() : '';
    },

    isJava(name) { return this.extension(name) === 'properties'; },

    _oddTrailingBackslashes(s) {
        let n = 0;
        while (n < s.length && s[s.length - 1 - n] === '\\') n++;
        return n % 2 === 1;
    },

    // One line -> null (blank or comment), {section}, {key, head, raw, tail} or {bad: reason}.
    // head + raw + tail is the line again, so a new value replaces raw and nothing else.
    _line(line, java) {
        let tail = '';
        if (line.endsWith('\r')) { tail = '\r'; line = line.slice(0, -1); }
        const lead = line.match(/^[ \t\f]*/)[0];
        const rest = line.slice(lead.length);
        if (!rest) return null;
        if (rest[0] === '#' || (java ? rest[0] === '!' : rest[0] === ';') || (!java && rest.startsWith('//'))) return null;
        if (!java) {
            const sec = rest.match(/^\[([^\]\r\n]+)\][ \t]*$/);
            if (sec) return { section: sec[1].trim() };
        }
        const eq = rest.indexOf('=');
        if (eq < 1) return { bad: 'it has no key=value' };
        const key = rest.slice(0, eq).replace(/[ \t]+$/, '');
        if (!this.KEY.test(key)) return { bad: 'the name before = has spaces or symbols' };
        let after = rest.slice(eq + 1);
        const gap = after.match(/^[ \t\f]*/)[0];
        after = after.slice(gap.length);
        if (java) {
            // Java keeps spaces at the end of a value, and a trailing backslash joins the next line.
            if (this._oddTrailingBackslashes(after)) return { bad: 'its value continues on the next line' };
        } else {
            const end = after.match(/[ \t]*$/)[0];
            if (end) { after = after.slice(0, after.length - end.length); tail = end + tail; }
        }
        return { key, head: lead + rest.slice(0, eq + 1) + gap, raw: after, tail };
    },

    unescape(s) {
        const map = { t: '\t', n: '\n', r: '\r', f: '\f' };
        let out = '';
        for (let i = 0; i < s.length; i++) {
            const c = s[i];
            if (c !== '\\' || i + 1 >= s.length) { out += c; continue; }
            const d = s[i + 1];
            if (d === 'u' && /^[0-9a-fA-F]{4}$/.test(s.slice(i + 2, i + 6))) {
                out += String.fromCharCode(parseInt(s.slice(i + 2, i + 6), 16));
                i += 5;
                continue;
            }
            out += map[d] !== undefined ? map[d] : d;
            i++;
        }
        return out;
    },

    // As Java's Properties.store() writes a value (and core/properties.py's format_line).
    escape(s) {
        const map = { '\\': '\\\\', '\t': '\\t', '\n': '\\n', '\r': '\\r', '\f': '\\f', '=': '\\=', ':': '\\:', '#': '\\#', '!': '\\!' };
        let out = '';
        for (let i = 0; i < s.length; i++) {
            const c = s[i];
            out += (c === ' ' && i === 0) ? '\\ ' : (map[c] !== undefined ? map[c] : c);
        }
        return out;
    },

    // Is this file a list of key=value settings?
    //   {ok: true, java, entries: [{line, key, value, section}], sections: [{line, name}]}
    //   {ok: false, line, reason}   line is 1-based, or 0 when it isn't about one line
    // Every line must be blank, a comment, a [section] heading or key=value: one line of
    // anything else and the form is not offered, so it can never hide or damage part of a file.
    parse(text, name) {
        const ext = this.extension(name);
        if (this.OTHER_FORMATS.includes(ext)) return { ok: false, line: 0, reason: `.${ext} files have their own format` };
        const java = ext === 'properties';
        const lines = String(text ?? '').split('\n');
        const entries = [], sections = [];
        let section = '';
        for (let i = 0; i < lines.length; i++) {
            const r = this._line(lines[i], java);
            if (!r) continue;
            if (r.bad) return { ok: false, line: i + 1, reason: r.bad };
            if (r.section !== undefined) { section = r.section; sections.push({ line: i, name: section }); continue; }
            entries.push({ line: i, key: r.key, value: java ? this.unescape(r.raw) : r.raw, section });
            if (entries.length > this.MAX_ENTRIES) return { ok: false, line: 0, reason: `it has more than ${this.MAX_ENTRIES} settings` };
        }
        if (!entries.length) return { ok: false, line: 0, reason: 'it has no key=value settings' };
        return { ok: true, java, entries, sections };
    },

    // The text with the value on one line (0-based, from parse) replaced. Nothing else changes.
    setValue(text, line, value, name) {
        const java = this.isJava(name);
        const lines = String(text).split('\n');
        const r = line >= 0 && line < lines.length ? this._line(lines[line], java) : null;
        if (!r || r.key === undefined) return text;
        value = String(value ?? '');
        if (java) value = this.escape(value);
        else value = value.replace(/[\r\n]+/g, ' ').trim();
        lines[line] = r.head + value + r.tail;
        return lines.join('\n');
    },

    // How to show one value, the same way Configuration's Visual Editor does:
    //   {type: 'bool', options: [true, false] spelled as the file spells them}
    //   {type: 'enum', options}, {type: 'number'} or {type: 'text'}
    widget(key, value) {
        if (/^(true|false)$/i.test(value)) {
            const style = value === value.toUpperCase() ? s => s.toUpperCase()
                : value[0] === value[0].toUpperCase() ? s => s[0].toUpperCase() + s.slice(1) : s => s;
            return { type: 'bool', options: [style('true'), style('false')] };
        }
        const en = this.ENUMS[key];
        if (en && en.includes(value)) return { type: 'enum', options: en };
        if (/^-?\d+(\.\d+)?$/.test(value)) return { type: 'number' };
        return { type: 'text' };
    },
};

if (typeof module !== 'undefined') module.exports = PropFile;

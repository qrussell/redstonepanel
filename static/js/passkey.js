// 1.24: passkeys (Windows Hello) on Windows panels. Used by the sign-in page and the account dialog.
// The server sends binary WebAuthn fields as base64url; the browser API wants ArrayBuffers.
(function () {
    function toBuf(s) {
        const b = atob(s.replace(/-/g, '+').replace(/_/g, '/') + '==='.slice((s.length + 3) % 4));
        const out = new Uint8Array(b.length);
        for (let i = 0; i < b.length; i++) out[i] = b.charCodeAt(i);
        return out.buffer;
    }
    function toB64u(buf) {
        const bytes = new Uint8Array(buf);
        let s = '';
        for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
        return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    }
    async function detail(res) {
        try { return (await res.json()).detail || `HTTP ${res.status}`; } catch (e) { return `HTTP ${res.status}`; }
    }
    function browserCan() {
        return !!(window.PublicKeyCredential && navigator.credentials && window.isSecureContext);
    }
    // What the browser said when the person closed the Windows Hello window, or it timed out
    function cancelled(e) {
        return e && (e.name === 'NotAllowedError' || e.name === 'AbortError');
    }

    async function signIn() {
        let res = await fetch('/api/auth/passkey/options', { method: 'POST' });
        if (!res.ok) throw new Error(await detail(res));
        const o = await res.json();
        const cred = await navigator.credentials.get({
            publicKey: {
                challenge: toBuf(o.challenge), rpId: o.rpId, timeout: o.timeout,
                userVerification: o.userVerification, allowCredentials: []
            }
        });
        const r = cred.response;
        res = await fetch('/api/auth/passkey/login', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                id: toB64u(cred.rawId), client_data: toB64u(r.clientDataJSON),
                authenticator_data: toB64u(r.authenticatorData), signature: toB64u(r.signature),
                user_handle: r.userHandle ? toB64u(r.userHandle) : null
            })
        });
        if (!res.ok) throw new Error(await detail(res));
        return res.json();
    }

    async function add(password, name) {
        let res = await fetch('/api/auth/passkeys/options', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password })
        });
        if (!res.ok) throw new Error(await detail(res));
        const o = await res.json();
        const cred = await navigator.credentials.create({
            publicKey: {
                challenge: toBuf(o.challenge), rp: o.rp,
                user: { id: toBuf(o.user.id), name: o.user.name, displayName: o.user.displayName },
                pubKeyCredParams: o.pubKeyCredParams, timeout: o.timeout, attestation: o.attestation,
                authenticatorSelection: o.authenticatorSelection,
                excludeCredentials: o.excludeCredentials.map(c => ({ type: c.type, id: toBuf(c.id) }))
            }
        });
        res = await fetch('/api/auth/passkeys', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                client_data: toB64u(cred.response.clientDataJSON),
                attestation_object: toB64u(cred.response.attestationObject), name: name || ''
            })
        });
        if (!res.ok) throw new Error(await detail(res));
        return res.json();
    }

    window.rpPasskey = { browserCan, cancelled, signIn, add, toBuf, toB64u };
})();

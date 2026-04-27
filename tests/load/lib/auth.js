// NEXYA Load Tests — Helper Auth (login + register + token cache).
//
// Utilisé par tous les scénarios qui ont besoin d'un user authentifié.
// Cache le token par VU (Virtual User) pour éviter de re-login à chaque
// itération (sinon on charge surtout le rate limiter, pas la cible).

import http from "k6/http";
import { check, fail } from "k6";

const BASE = __ENV.BASE_URL || "http://localhost:8000";

// Comptes seed dev (cf. scripts/seed_dev.py)
export const SEED_FREE = { email: "free@nexya.ai", password: "DemoFree2026!" };
export const SEED_PRO = { email: "pro@nexya.ai", password: "DemoPro2026!" };

// Cache token par VU — `__VU` est l'identifiant k6 du Virtual User.
let _tokenCache = {};

export function loginCached(creds) {
    const key = `${__VU}:${creds.email}`;
    if (_tokenCache[key]) {
        return _tokenCache[key];
    }
    const token = login(creds);
    _tokenCache[key] = token;
    return token;
}

export function login(creds) {
    const res = http.post(
        `${BASE}/auth/login`,
        JSON.stringify({ email: creds.email, password: creds.password }),
        {
            headers: { "Content-Type": "application/json" },
            tags: { endpoint: "auth_login" },
        }
    );
    if (res.status !== 200) {
        fail(`login failed: status=${res.status} body=${res.body}`);
    }
    const body = res.json();
    if (!body || !body.success || !body.data || !body.data.access_token) {
        fail(`login: réponse malformée body=${JSON.stringify(body).slice(0, 200)}`);
    }
    return body.data.access_token;
}

export function register(email, password) {
    const res = http.post(
        `${BASE}/auth/register`,
        JSON.stringify({
            email,
            password,
            display_name: "LoadTest User",
        }),
        {
            headers: {
                "Content-Type": "application/json",
                "X-Device-Id": `loadtest-${__VU}-${__ITER}`,
            },
            tags: { endpoint: "auth_register" },
        }
    );
    return res;
}

export function authHeaders(token) {
    return { Authorization: `Bearer ${token}`, "Content-Type": "application/json" };
}

export function randomEmail() {
    const ts = Date.now();
    return `loadtest_${__VU}_${ts}_${Math.random().toString(36).slice(2, 8)}@nexya.ai`;
}

export function randomStrongPassword() {
    // Respecte la politique register (≥ 12, maj/min/chiffre/spécial)
    return `LoadTest${__VU}_${Date.now()}!Aa1`;
}

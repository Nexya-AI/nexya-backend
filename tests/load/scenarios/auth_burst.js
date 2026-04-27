// NEXYA Load Test — Auth burst (register + login parallèles).
//
// Scénario : 50 RPS pendant 60s, mix 50% register / 50% login.
// Cible SLO : p95 login < 500ms, p95 register < 800ms (bcrypt + DB INSERT
// lourds), error rate < 1%, pas de 5xx.

import { check } from "k6";
import http from "k6/http";
import { authHeaders, login, randomEmail, randomStrongPassword, register, SEED_FREE } from "../lib/auth.js";
import { tAuthLoginMs, tAuthRegisterMs } from "../lib/metrics.js";

const BASE = __ENV.BASE_URL || "http://localhost:8000";

export const options = {
    scenarios: {
        auth_burst: {
            executor: "constant-arrival-rate",
            rate: 50,
            timeUnit: "1s",
            duration: "60s",
            preAllocatedVUs: 50,
            maxVUs: 100,
        },
    },
    thresholds: {
        "http_req_failed{endpoint:auth_login}": ["rate<0.01"],
        "auth_login_ms": ["p(95)<500"],
        "auth_register_ms": ["p(95)<800"],
    },
};

export default function () {
    // 50/50 register vs login. Le seed user `free@nexya.ai` est partagé →
    // login concurrent stress le rate-limit-aware login path + bcrypt verify.
    const isRegister = Math.random() < 0.5;
    if (isRegister) {
        const email = randomEmail();
        const password = randomStrongPassword();
        const start = Date.now();
        const res = register(email, password);
        tAuthRegisterMs.add(Date.now() - start);
        check(res, {
            "register status 200/422": (r) => [200, 422].includes(r.status),
        });
    } else {
        const start = Date.now();
        const token = login(SEED_FREE);
        tAuthLoginMs.add(Date.now() - start);
        check({ token }, {
            "login token issued": (o) => Boolean(o.token),
        });
    }
}

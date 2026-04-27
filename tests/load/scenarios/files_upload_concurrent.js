// NEXYA Load Test — Upload concurrent multipart 1 MB.
//
// Scénario : 20 VUs pendant 2 min, chaque itération upload un PDF
// random ~1 MB sur `/files/upload`. Stress MinIO + virus scanner mock
// + extraction texte async + dédup SHA-256.
//
// Cible SLO : p95 upload total < 3000ms, error rate < 1%.

import { check } from "k6";
import http from "k6/http";
import { authHeaders, loginCached, SEED_FREE } from "../lib/auth.js";
import { cUploadKo, cUploadOk, rUploadSuccess, tUploadTotalMs } from "../lib/metrics.js";

const BASE = __ENV.BASE_URL || "http://localhost:8000";
const ONE_MB = 1024 * 1024;

// PDF minimaliste valide (header + objets minimaux). Padding aléatoire
// pour défaire la dédup SHA-256 entre itérations.
const PDF_HEADER = "%PDF-1.4\n%\xc3\xa4\xc3\xb6\n";
const PDF_FOOTER = "\n%%EOF\n";

function buildRandomPdf(sizeBytes) {
    const padSize = sizeBytes - PDF_HEADER.length - PDF_FOOTER.length;
    let pad = "";
    while (pad.length < padSize) {
        pad += Math.random().toString(36).slice(2);
    }
    return PDF_HEADER + pad.slice(0, padSize) + PDF_FOOTER;
}

export const options = {
    scenarios: {
        upload_burst: {
            executor: "constant-vus",
            vus: 20,
            duration: "2m",
        },
    },
    thresholds: {
        "upload_total_ms": ["p(95)<3000"],
        "upload_success_rate": ["rate>0.99"],
        "http_req_failed{endpoint:files_upload}": ["rate<0.01"],
    },
};

export default function () {
    const token = loginCached(SEED_FREE);
    const fileBody = buildRandomPdf(ONE_MB);
    const start = Date.now();
    const res = http.post(
        `${BASE}/files/upload`,
        {
            file: http.file(fileBody, `loadtest-${__VU}-${__ITER}.pdf`, "application/pdf"),
        },
        {
            headers: { Authorization: `Bearer ${token}` },
            tags: { endpoint: "files_upload" },
            timeout: "10s",
        }
    );
    const elapsed = Date.now() - start;
    tUploadTotalMs.add(elapsed);

    const ok = res.status === 201 || res.status === 200;
    rUploadSuccess.add(ok ? 1 : 0);
    if (ok) cUploadOk.add(1);
    else cUploadKo.add(1);

    check(res, {
        "upload status 200/201": (r) => [200, 201].includes(r.status),
    });
}

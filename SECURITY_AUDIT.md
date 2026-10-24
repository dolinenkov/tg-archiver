# Security Audit and Remediation Plan

Date: 2026-10-24. Reviewed HEAD: `519a873`, together with the uncommitted application changes present during review.

## Executive assessment

No checked-in credentials were identified in the available Git history or the reviewed working-tree source. The most serious application risks are unauthenticated archive/setup access and missing Host validation. Defaults bind to loopback, and Compose publishes only to loopback; these reduce exposure but do not authenticate local clients.

This is a source review with targeted synthetic exploit tests and dependency-advisory checks, not a guarantee that every vulnerability or obfuscated secret has been found. No application security fixes were applied. The owner's configuration-removal commit and ignore-file edit were preserved.

## Git history and secrets

- Examined all six commits reachable through available local branches, remote-tracking refs, and reflogs: 74 Git objects, including 54 distinct historical file blobs. Inspected commit metadata/messages as well. `git fsck --full --no-reflogs --unreachable` reported no unreachable objects.
- Scanned historical content and tracked/untracked source files with redacted checks for private keys, Telegram bot tokens, GitHub tokens, AWS keys, Google API keys, Slack/Stripe tokens, JWTs, credential-bearing URLs, credential assignments, 32-character hexadecimal values, and long quoted base64 values. No matches requiring secret disclosure were found. This used targeted scripts, not Gitleaks or an exhaustive entropy detector.
- `config.toml` entered history in `55064cc8396f`. Its bytes exactly match that commit's `config.example.toml`; it contains archive/media settings and no Telegram API credentials. `519a873c4e4c` removes it from tracking. The local file still exists and the owner's new `.gitignore` rule ignores it.
- No session database, archive database, private key, or `.env` file was identified among tracked historical blobs. Only permission metadata was inspected for the live archive/session: data directory `0700`, database and session `0600`. Live session contents were not read.
- Remote refs here are local snapshots. No fetch or remote-host audit was performed; the owner's push failed with an SSH timeout. Remote-only branches, forks, PRs, CI artifacts, backups, and previously deleted remote history are outside this evidence.

**Conclusion:** there is no evidence requiring credential rotation or history rewriting from the content reviewed. Removing a file from tracking does not erase its historical versions; here the historical configuration is credential-free. If an actual Telegram session is ever exposed, revoke it through an official Telegram client; possession of session material grants account access. See [Telethon session security](https://docs.telethon.dev/en/stable/concepts/sessions.html).

## Findings

Severity reflects impact and prerequisites, not a claim that the default listener is internet-accessible. “Confirmed” means the server behavior was reproduced with temporary databases and fake Telegram boundaries.

### A1 — High: web access has no authentication

**Evidence:** `tg_archiver/web.py:376` constructs the app with only the response-header middleware. All chats, historical messages, search results, profiles, transcripts, attachments, status, and Settings are anonymous. `setup_web.py:71` requires a CSRF token, but anonymous visitors can obtain it from Settings. An anonymous Settings GET returned 200 and a subsequent action returned 303.

**Impact:** any client able to reach the listener can read private archive data and interfere with login/import operations. Loopback includes other processes/users on the same host. A non-loopback configuration or incorrectly secured proxy makes the impact remote. This confirms archive/setup compromise, not arbitrary Telegram message sending or code execution.

**Fix:** authenticate every sensitive route with an application login independent of Telegram authorization. Use a random bootstrap credential stored with private permissions, server-managed sessions, HttpOnly/SameSite cookies, expiration, logout, and session-bound CSRF. Require authenticated administration for setup actions. Keep `/health` minimal if intentionally public. Add access-control tests for every route, including direct attachment/transcription URLs.

### A2 — High, conditional: arbitrary Host acceptance weakens loopback isolation

**Evidence:** no Host allowlist exists in `web.py`. `setup_web.py:76` compares Origin against a URL built from the supplied Host. A request with `Host: attacker.invalid:8080` retrieved Settings; a POST with that Host, matching Origin, and the retrieved token was accepted.

**Impact:** the application lacks a server-side defense against DNS-rebinding access. An attacker-controlled origin that can reach the listener under its hostname can read data and pass the current Origin check. Browser/network local-access restrictions affect exploitability; the test established server acceptance, not an end-to-end browser DNS-rebinding exploit. [Chrome's local-network protections](https://developer.chrome.com/blog/local-network-access) provide additional browser defenses, not an application authentication boundary.

**Fix:** validate Host against explicit approved hostnames/ports before routing. Default to the supported loopback authorities; configure explicit names for proxies. Validate Origin against configured external origins rather than reflecting Host. Reject untrusted forwarded headers and test IPv6, malformed authorities, and proxy deployment. Implement A1 as well.

### A3 — Medium: web import can consume unbounded resources and pause collection

**Evidence:** `setup_web.py:106` pauses collection before resolving/previewing a supplied server path. `importer.py:99` reads the entire file into memory and builds a second in-memory import plan. There is no import-root restriction, regular-file requirement, input-byte/message budget, or operation deadline. Cleanup waits for the operation to finish. The HTTP form-body limit does not limit the server-side file being read.

**Impact:** a reachable Settings client can select a very large readable file or a blocking special file, exhausting memory or keeping collection paused. A malicious export can also cause excessive allocation. This is not a demonstrated arbitrary-file exfiltration: export validation and attachment containment do block ordinary traversal.

**Fix:** restrict web imports to an explicit administrator-configured directory, open and validate a regular file, cap bytes/message counts/nesting, and stream large exports. Validate before pausing collection where possible. Run parsing in a cancellable worker process with a deadline; cancelling `asyncio.to_thread` alone does not stop its filesystem read. Bound shutdown waits and always resume collection after rejected/expired operations.

### A4 — Medium: attachment size limit is bypassed for unknown-size media

**Evidence:** `config.py:29` accepts size zero under a positive limit. `collector.py:475` checks that stored metadata, then downloads without a streaming byte ceiling. Its post-download size check only runs when the stored size is nonzero. A synthetic unknown-size attachment downloaded 32 bytes under an 8-byte limit and was recorded as complete.

**Impact:** unknown-size media can exceed an operator's configured limit. Default `max_file_bytes=0` additionally allows unlimited per-file size; no archive-wide storage budget/free-space reserve exists. A sender able to supply archived media can consume disk and stop collection. The metadata-zero path was reproduced with a fake API, not a real Telegram upload.

**Fix:** revalidate current media size before downloading and enforce actual streamed bytes regardless of metadata. Abort and remove oversized partial files. Add free-space reserves and configurable total-storage/download budgets; preserve captured data rather than deleting it automatically. Test zero/incorrect sizes and partial-file cleanup.

### A5 — Medium: expensive reads and revision rendering have no work budget

**Evidence:** `web.py:148` loads every revision and performs attachment queries per revision, even for collapsed history. Conversation batches cap message count, not revision count or response bytes. Search permits FTS expressions and arbitrary offsets without query time/concurrency budgets; page rendering performs additional archive-wide sidebar queries.

**Impact:** repeated queries or a message with many captured edits can monopolize SQLite, CPU, and rendering memory. A Telegram sender can increase revision volume; a reachable web client can repeatedly request expensive views. This is a source-confirmed resource-amplification risk; no production-scale denial-of-service load test was run.

**Fix:** fetch revision bodies on demand with bounded cursor pagination, batch attachment queries, and impose response/query/concurrency budgets. Use keyset pagination for deep lists, bounded search complexity, and appropriate indexes. Apply admission/rate limits to costly endpoints and test with large synthetic histories. These measures follow [OWASP resource-exhaustion guidance](https://cheatsheetseries.owasp.org/cheatsheets/Denial_of_Service_Cheat_Sheet.html).

### A6 — Low: malformed numeric/date inputs cause unhandled errors

**Evidence:** `/chat/9223372036854775808`, `/?offset=9223372036854775808`, and `/search?q=hello&to=9999-12-31` each returned 500 in probes. `web.py:86` checks only an integer's lower bound; some route IDs bypass even that helper. `date_filter` overflows when advancing the maximum date. Requests generate server tracebacks. A malformed request did not stop the process.

**Fix:** centralize signed-64-bit ID validation, bounded pagination, and safe date arithmetic; return controlled 400/404 responses. Validate CSRF token encoding before `compare_digest`, which rejects non-ASCII strings. Add regression tests and sanitize expected error logging.

## Additional hardening gaps

These are preventive controls or conditional deployment risks, not independently demonstrated remote exploits.

- **Secret-commit prevention:** the owner has removed/ignored `config.toml`, but `.gitignore` still permits sessions outside `data/`, SQLite archives elsewhere, and `.env.production`. Add appropriate session/database sidecar patterns and `.env.*` with explicit example-file exceptions. Install a redacting pre-commit scanner and an enforced CI history/diff scan. Ignore rules alone do not prevent forced or previously tracked commits.
- **Remote transport:** `__main__.py:124` starts HTTP without TLS. Never send Telegram codes/passwords over a directly exposed HTTP listener. If remote access is supported, require an authenticated TLS reverse proxy/tunnel, restrict backend reachability, and configure trusted origins explicitly. Current loopback defaults are a positive control.
- **Dependency/build reproducibility:** Python dependencies use broad ranges without a lock/hash manifest; Docker uses mutable base tags and runs as root. Lock tested Python dependencies with hashes, automate advisory updates, pin/update image digests, run as a dedicated unprivileged UID, drop capabilities, and use a read-only root filesystem with only the archive volume writable. No current dependency CVE was identified by the scans below; these changes reduce future supply-chain and post-compromise risk.
- **Filesystem defense:** private CLI umask protects newly created files but does not repair an existing permissive data directory. Download write paths, unlike serving paths, do not consistently reject pre-existing symlinks; exploiting this requires local write access. Validate existing directory/file ownership and permissions, reject unsafe symlink destinations using safe file-opening primitives, and protect Windows ACLs and backups. Plaintext archive/session storage is documented; disk/backup encryption remains a deployment responsibility.
- **Error headers:** `web.py:78` applies security headers only after a handler returns normally. A probed 404 lacked CSP and `Cache-Control: no-store`; exceptions bypass the middleware's assignments. Normalize HTTP/error responses before attaching headers. No sensitive error-body disclosure or error-page XSS was demonstrated.

## Verification and limitations

- All 73 existing unit/integration tests passed, using temporary databases and simulated Telegram boundaries. Existing tests cover HTML escaping, CSRF rejection, SQL parameterization, path containment, and account ownership. New audit probes exposed cases those tests do not cover.
- `npm audit --json`: zero reported advisories for the lockfile inventory (61 dependencies, including development/optional dependencies).
- `pip-audit --path .venv/lib/python3.12/site-packages`: zero reported advisories for 19 public installed packages. The local `tg-archiver` package was skipped by the advisory service and reviewed as source here. Scanned direct versions: Telethon 1.45.0, SQLAlchemy 2.1.5, aiosqlite 0.22.1, aiohttp 3.14.5, Jinja2 3.1.6. Results are advisory-service snapshots, not proof that packages are vulnerability-free or safe to replace with any permitted version.
- Positive controls include parameterized SQL, Jinja autoescaping, escaped/link-scheme-limited message rendering, text-only live transcript updates, strict success-response CSP, download MIME allowlists plus `nosniff`, resolved read-path containment, import traversal/symlink rejection, private Unix creation permissions, exception-text minimization, and a data-directory process lock.
- No live Telegram account, production listener, session contents, browser DNS infrastructure, Windows deployment, or remote Git hosting was tested. Docker was unavailable, so container OS packages/images were not scanned. No confirmed SQL injection, stored XSS, ordinary attachment-read traversal, SSRF, or remote code execution was found in the reviewed paths; this is not an exhaustive absence claim.

## Prioritized implementation plan

| Priority | Work package | Completion criteria |
| --- | --- | --- |
| P0 | Access boundary: A1/A2 and remote transport | Anonymous requests cannot read archive/setup APIs; unknown Host/Origin combinations are rejected; explicit proxy/TLS configuration works; health remains minimal. |
| P0 | Secret hygiene | Finish the owner's `.gitignore` change, add session/database/environment patterns and redacting CI/pre-commit scanning. Keep the credential-free example. No history rewrite is warranted by current evidence. |
| P1 | Import safety: A3 | Oversized, special, out-of-root, deeply nested, or timed-out imports are rejected without leaving collection paused; workers/shutdown remain bounded. |
| P1 | Storage enforcement: A4 | Actual bytes never exceed configured limits unnoticed; low-disk admission stops downloads safely; partial files are cleaned; existing captures remain intact. |
| P1 | Web resource controls: A5/A6 | Revision views and searches have bounded work; malformed IDs/dates return 4xx; large-history and concurrency regression tests pass. |
| P2 | Deployment/build hardening | Locked Python dependencies and update automation; non-root, restricted container; ownership/symlink checks; uniform error headers; image scan once Docker is available. |

Deliver these as small, independently reviewable changes with targeted regression tests. Re-run full tests, dependency/secret scans, and access-control probes against the final deployment before considering the audit findings closed.

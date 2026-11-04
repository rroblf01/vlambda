module vlambda_native

import vcraft
import db.sqlite
import net.http
import time
import os
import strings
import crypto.sha256
import crypto.subtle
import rand
import json2
import encoding.base64

// vcraft.from_py_string/from_py_bytes reuse the Python object's buffer instead
// of copying it. Reading the result after decref touches freed memory, which
// shows up as truncated or corrupted responses under load. Copy while the
// object is still alive.
fn py_string_copy(obj vcraft.PyObj, name string, fallback string) string {
	s := vcraft.from_py_string(obj, name) or { return fallback }
	return s.clone()
}

fn py_bytes_copy(obj vcraft.PyObj, name string, fallback string) string {
	s := vcraft.from_py_bytes(obj, name) or { return fallback }
	return s.clone()
}

// ---- Global state ----

__global (
	vlambda_db_path = './vlambda.db'
	vlambda_db_error = ''
	vlambda_version = 'unknown'
	vlambda_gc_counter = u64(0)
	vlambda_gc_every = 1000
	vlambda_executor = voidptr(0)
	vlambda_auth_verifier = voidptr(0)
	vlambda_auth_register = voidptr(0)
	vlambda_auth_login = voidptr(0)
	vlambda_validate_code = voidptr(0)
	vlambda_ui_html = voidptr(0)
	vlambda_favicon = voidptr(0)
	vlambda_create_task = voidptr(0)
	vlambda_get_task = voidptr(0)
	vlambda_get_asset = voidptr(0)
	vlambda_list_aliases = voidptr(0)
	vlambda_list_versions = voidptr(0)
	vlambda_save_project_json = voidptr(0)
	vlambda_get_project_json = voidptr(0)
	vlambda_get_dlq = voidptr(0)
	vlambda_record_metric = voidptr(0)
	vlambda_query_metrics = voidptr(0)
	vlambda_query_summary = voidptr(0)
	vlambda_query_aggregate = voidptr(0)
	vlambda_query_logs = voidptr(0)
	vlambda_delete_metrics = voidptr(0)
	vlambda_delete_logs = voidptr(0)
	vlambda_export_metrics = voidptr(0)
	vlambda_pool_stats = voidptr(0)
	vlambda_query_stats = voidptr(0)
	vlambda_run_project = voidptr(0)
	vlambda_create_project = voidptr(0)
	vlambda_delete_project = voidptr(0)
	vlambda_schedule_delete = voidptr(0)
	vlambda_export_lambda = voidptr(0)
	vlambda_import_lambda = voidptr(0)
	vlambda_duplicate_lambda = voidptr(0)
	vlambda_admin_create_user = voidptr(0)
	// Config
	vlambda_max_body_size = 1048576  // 1MB
	vlambda_rate_limit = 100          // req/s per IP
	vlambda_invoke_timeout = 30       // seconds
	vlambda_max_memory_mb = 0         // 0 = unlimited
	vlambda_metrics_enabled = true
	vlambda_metrics_sample = 1
	vlambda_registration_enabled = true
	vlambda_metric_counter = u64(0)
	vlambda_request_counter = u64(0)
	vlambda_cors_origin = '*'        // CORS origin
	vlambda_cert_path = ''
	vlambda_key_path = ''
	// Metrics
	vlambda_req_total = u64(0)
	vlambda_req_errors = u64(0)
	vlambda_lambdas_created = u64(0)
	vlambda_latency_sum = f64(0)
	vlambda_latency_count = u64(0)
	// Per-lambda metrics: slug -> count
	vlambda_lambda_invocations = map[string]u64{}
	vlambda_lambda_errors = map[string]u64{}
	vlambda_lambda_latency = map[string]f64{}
	// Rate limiter state
	vlambda_rate_map = map[string]int{}
	vlambda_rate_window = i64(0)
	vlambda_slug_counter = u64(0)
	// In-memory code cache: slug -> code|method
	vlambda_code_cache = map[string]string{}
	vlambda_missing_cache = map[string]bool{}
	vlambda_project_cache = map[string]string{}
	vlambda_project_missing = map[string]bool{}
	// Shutdown
	vlambda_overloaded = false
	vlambda_resetting = false
	vlambda_draining = false
	vlambda_admin_reset = voidptr(0)
	vlambda_install_packages = voidptr(0)
	vlambda_list_packages = voidptr(0)
	vlambda_list_env = voidptr(0)
	vlambda_save_env = voidptr(0)
	vlambda_kv = voidptr(0)
	vlambda_egress = voidptr(0)
	vlambda_schedules = voidptr(0)
	vlambda_meta = voidptr(0)
	vlambda_openapi = voidptr(0)
	vlambda_shutdown = voidptr(0)
	// Rate limiter (token bucket per IP)
	vlambda_rate_buckets = map[string]TokenBucket{}
)

// ---- SQLite helpers ----

fn init_db(db_path string) !sqlite.DB {
	mut db := sqlite.connect(db_path)!
	if os.getenv('VLAMBDA_SQLITE_JOURNAL').to_lower() == 'delete' {
		db.exec('PRAGMA journal_mode=DELETE;')!
	} else {
		db.exec('PRAGMA journal_mode=WAL;')!
	}
	db.exec('PRAGMA busy_timeout=5000;')!
	version := db.q_int('PRAGMA user_version') or { 0 }
	if version > 1 {
		return error('database schema version ${version} is newer than this vlambda build')
	}
	if version == 0 {
		existing := db.q_int('SELECT count(*) FROM sqlite_master WHERE type = "table" AND name IN ("users", "lambdas")') or { 0 }
		if existing > 0 {
			return error('legacy vlambda database detected; 1.0 requires a fresh database')
		}
	}
	db.exec('CREATE TABLE IF NOT EXISTS users (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		username TEXT UNIQUE NOT NULL,
		password_hash TEXT NOT NULL,
		auth_epoch TEXT NOT NULL DEFAULT ""
	);')!
	db.exec('CREATE TABLE IF NOT EXISTS lambdas (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		slug TEXT UNIQUE NOT NULL,
		name TEXT NOT NULL DEFAULT "",
		code TEXT NOT NULL,
		method TEXT NOT NULL DEFAULT "GET",
		owner_id INTEGER NOT NULL DEFAULT 0,
		created_at TEXT NOT NULL,
		kind TEXT NOT NULL DEFAULT "code",
		entrypoint TEXT NOT NULL DEFAULT "",
		project_dir TEXT NOT NULL DEFAULT "",
		timeout INTEGER NOT NULL DEFAULT 0,
		memory_mb INTEGER NOT NULL DEFAULT 0,
		version INTEGER NOT NULL DEFAULT 1,
		summary TEXT NOT NULL DEFAULT "",
		description TEXT NOT NULL DEFAULT "",
		tags TEXT NOT NULL DEFAULT ""
	);')!
	db.exec('CREATE TABLE IF NOT EXISTS lambda_versions (id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT NOT NULL, version INTEGER NOT NULL, code TEXT NOT NULL, method TEXT NOT NULL DEFAULT "GET", entrypoint TEXT NOT NULL DEFAULT "", project_dir TEXT NOT NULL DEFAULT "", kind TEXT NOT NULL DEFAULT "code", created_at TEXT NOT NULL);')!
	db.exec('CREATE TABLE IF NOT EXISTS lambda_aliases (alias TEXT PRIMARY KEY, slug TEXT NOT NULL, version INTEGER NOT NULL, owner_id INTEGER NOT NULL);')!
	db.exec('CREATE TABLE IF NOT EXISTS lambda_env (slug TEXT NOT NULL, key TEXT NOT NULL, is_secret INTEGER NOT NULL DEFAULT 0, value TEXT NOT NULL, PRIMARY KEY (slug, key));')!
	db.exec('CREATE TABLE IF NOT EXISTS lambda_kv (slug TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, expires_at REAL, updated_at REAL NOT NULL, PRIMARY KEY (slug, key));')!
	db.exec('CREATE INDEX IF NOT EXISTS idx_lambda_kv_slug ON lambda_kv(slug);')!
	db.exec('CREATE TABLE IF NOT EXISTS lambda_egress (slug TEXT NOT NULL, pattern TEXT NOT NULL, PRIMARY KEY (slug, pattern));')!
	db.exec('CREATE INDEX IF NOT EXISTS idx_lambda_egress_slug ON lambda_egress(slug);')!
	db.exec('CREATE TABLE IF NOT EXISTS schedules (id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT NOT NULL, owner_id INTEGER NOT NULL, name TEXT NOT NULL, cron TEXT NOT NULL, timezone TEXT NOT NULL DEFAULT "UTC", method TEXT NOT NULL DEFAULT "POST", path TEXT NOT NULL DEFAULT "/schedule", payload TEXT NOT NULL DEFAULT "", enabled INTEGER NOT NULL DEFAULT 1, running INTEGER NOT NULL DEFAULT 0, run_started REAL, next_run REAL, last_run REAL, last_status TEXT, created_at REAL NOT NULL, UNIQUE(slug, name));')!
	db.exec('CREATE INDEX IF NOT EXISTS idx_schedules_due ON schedules(enabled, next_run);')!
	db.exec('CREATE TABLE IF NOT EXISTS schedule_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, schedule_id INTEGER NOT NULL, slug TEXT NOT NULL, request_id TEXT, started REAL NOT NULL, finished REAL, status TEXT NOT NULL, error TEXT);')!
	db.exec('CREATE INDEX IF NOT EXISTS idx_schedule_runs_schedule ON schedule_runs(schedule_id, id);')!
	db.exec('CREATE TABLE IF NOT EXISTS scheduler_lease (id INTEGER PRIMARY KEY CHECK (id = 1), owner TEXT NOT NULL, expires REAL NOT NULL);')!
	db.exec('CREATE TABLE IF NOT EXISTS api_tokens (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		user_id INTEGER NOT NULL,
		name TEXT NOT NULL DEFAULT "",
		token_hash TEXT UNIQUE NOT NULL,
		scopes TEXT NOT NULL DEFAULT "read,write",
		created_at TEXT NOT NULL,
		last_used_at TEXT NOT NULL DEFAULT "",
		revoked INTEGER NOT NULL DEFAULT 0
	);')!
	db.exec('PRAGMA user_version = 1;')!
	return db
}

// db_open opens an existing database for a helper query. Schema creation is a
// startup concern: running init_db's DDL on every request is expensive and
// allocates per call.
fn db_open() !sqlite.DB {
	mut db := sqlite.connect(vlambda_db_path)!
	db.exec('PRAGMA busy_timeout=5000;')!
	return db
}

// ---- Slug generation ----

fn make_slug(code string) string {
	// Simple slug: hash of code + global counter for uniqueness
	vlambda_slug_counter++
	mut h := u64(5381)
	for c in code {
		h = ((h << 5) + h) + u64(c)
	}
	h = h ^ (vlambda_slug_counter * 2654435761) ^ rand.u64()
	return 'l${h:016x}'
}

// ---- CORS / static file helpers ----

fn add_cors_headers(mut hdr http.Header) {
	hdr.add_custom('Access-Control-Allow-Origin', vlambda_cors_origin) or {}
	hdr.add_custom('Access-Control-Allow-Methods', 'GET, POST, PUT, PATCH, DELETE, OPTIONS') or {}
	hdr.add_custom('Access-Control-Allow-Headers', 'Content-Type, Authorization') or {}
	hdr.add_custom('Access-Control-Expose-Headers', 'X-Request-Id') or {}
	if vlambda_cors_origin != '*' {
		hdr.add_custom('Vary', 'Origin') or {}
	}
}

fn add_security_headers(mut hdr http.Header) {
	hdr.add_custom('X-Content-Type-Options', 'nosniff') or {}
	hdr.add_custom('X-Frame-Options', 'DENY') or {}
	hdr.add_custom('Referrer-Policy', 'no-referrer') or {}
	hdr.add_custom('Permissions-Policy', 'camera=(), microphone=(), geolocation=()') or {}
	hdr.add_custom('Content-Security-Policy', "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'") or {}
}

fn cors_response(status int, body string, content_type string) http.Response {
	mut hdr := http.new_header(http.HeaderConfig{ key: .content_type, value: content_type })
	add_cors_headers(mut hdr)
	return http.Response{
		status_code: status
		body: body
		header: hdr
	}
}

fn cors_json(status int, body string) http.Response {
	return cors_response(status, body, 'application/json')
}

fn normalize_method(value string) string {
	m := value.trim_space().to_upper()
	return match m {
		'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS' { m }
		else { 'GET' }
	}
}

fn has_scope(scopes string, want string) bool {
	for scope in scopes.split(',') {
		if scope.trim_space() == want {
			return true
		}
	}
	return false
}

fn hex_digit(c u8) int {
	if c >= `0` && c <= `9` {
		return int(c - `0`)
	}
	if c >= `a` && c <= `f` {
		return int(c - `a`) + 10
	}
	if c >= `A` && c <= `F` {
		return int(c - `A`) + 10
	}
	return -1
}


fn is_admin_request(req http.Request) bool {
	token := req.header.get_custom('X-Admin-Token', http.HeaderQueryConfig{}) or { '' }
	expected := os.getenv('VLAMBDA_ADMIN_TOKEN')
	if expected == '' {
		return false
	}
	return subtle.constant_time_compare(token.bytes(), expected.bytes()) == 1
}

fn request_owner(req http.Request, auth_uid int) int {
	if is_admin_request(req) { return 0 }
	return auth_uid
}

fn decode_query_value(value string) string {
	mut out := ''
	mut i := 0
	for i < value.len {
		c := value[i]
		if c == `+` {
			out += ' '
		} else if c == `%` && i + 2 < value.len {
			hi := hex_digit(value[i + 1])
			lo := hex_digit(value[i + 2])
			if hi >= 0 && lo >= 0 {
				out += u8(hi * 16 + lo).ascii_str()
				i += 2
			} else {
				out += u8(c).ascii_str()
			}
		} else {
			out += u8(c).ascii_str()
		}
		i++
	}
	return out
}

fn json_str(value string) string {
	return json2.encode(value, json2.EncoderOptions{})
}

fn content_type_for(path string) string {
	ext := path.all_after_last('.').to_lower()
	return match ext {
		'html', 'htm' { 'text/html; charset=utf-8' }
		'css' { 'text/css; charset=utf-8' }
		'js', 'mjs' { 'text/javascript; charset=utf-8' }
		'json' { 'application/json; charset=utf-8' }
		'xml' { 'application/xml; charset=utf-8' }
		'txt', 'md' { 'text/plain; charset=utf-8' }
		'svg' { 'image/svg+xml' }
		'png' { 'image/png' }
		'jpg', 'jpeg' { 'image/jpeg' }
		'gif' { 'image/gif' }
		'webp' { 'image/webp' }
		'ico' { 'image/x-icon' }
		'woff' { 'font/woff' }
		'woff2' { 'font/woff2' }
		'ttf' { 'font/ttf' }
		'wasm' { 'application/wasm' }
		'pdf' { 'application/pdf' }
		else { 'application/octet-stream' }
	}
}

fn is_safe_static_path(sub string) bool {
	if sub == '' || sub.starts_with('/') || sub.contains('\\') {
		return false
	}
	low := sub.to_lower()
	if low.contains('%2e') || low.contains('%2f') || low.contains('%5c') || low.contains('\x00') {
		return false
	}
	if low.ends_with('.py') || low.ends_with('.pyc') || low.ends_with('.pyo') {
		return false
	}
	if low == 'requirements.txt' || low == 'pyproject.toml' || low == 'pipfile' || low == 'poetry.lock' {
		return false
	}
	for part in sub.split('/') {
		if part == '' || part == '.' || part == '..' {
			return false
		}
		if part.starts_with('.') {
			return false
		}
		if part == 'vendor' || part == '__pycache__' || part == '.git' {
			return false
		}
	}
	return true
}

fn serve_project_file(project_dir string, sub string) ?http.Response {
	if !is_safe_static_path(sub) {
		return none
	}
	full := os.join_path(project_dir, sub)
	if !os.exists(full) || os.is_dir(full) {
		return none
	}
	data := os.read_bytes(full) or { return none }
	return http.Response{
		status_code: 200
		body: data.bytestr()
		header: http.new_header(http.HeaderConfig{
			key: .content_type
			value: content_type_for(sub)
		})
	}
}

struct LambdaResponseHeader {
	name  string
	value string
}

struct LambdaResponse {
	status   int
	headers  []LambdaResponseHeader
	body     string
	body_b64 string
}

fn request_meta_json(req http.Request) string {
	mut headers := []string{}
	for key in req.header.keys() {
		lowered := key.to_lower()
		if lowered in ['x-admin-token', 'authorization', 'proxy-authorization', 'cookie', 'x-api-key'] {
			continue
		}
		if value := req.header.get_custom(key, http.HeaderQueryConfig{}) {
			headers << json_str(key) + ':' + json_str(value)
		}
	}
	scheme := req.header.get_custom('X-Forwarded-Proto', http.HeaderQueryConfig{}) or { 'http' }
	return '{"headers":{' + headers.join(',') + '},"remote_ip":' + json_str(req.remote_ip()) + ',"host":' + json_str(req.host) + ',"scheme":' + json_str(scheme) + '}'
}

fn error_envelope(message string) string {
	body := '{"error":' + json_str(message) + '}'
	return '{"status":500,"headers":[{"name":"Content-Type","value":"application/json"}],"body":' + json_str(body) + ',"body_b64":""}'
}

fn build_lambda_response(raw string) http.Response {
	mut lr := json2.decode[LambdaResponse](raw, json2.DecoderOptions{}) or {
		return http.Response{
			status_code: 200
			body: raw
			header: http.new_header(http.HeaderConfig{ key: .content_type, value: lambda_content_type(raw) })
		}
	}
	body := if lr.body_b64 != '' { base64.decode_str(lr.body_b64) } else { lr.body }
	status := if lr.status > 0 { lr.status } else { 200 }
	mut hdr := http.new_header(http.HeaderConfig{ key: .content_type, value: lambda_content_type(body) })
	for item in lr.headers {
		if item.name.to_lower() == 'content-type' {
			hdr.set_custom(item.name, item.value) or {}
		} else {
			hdr.add_custom(item.name, item.value) or {}
		}
	}
	return http.Response{
		status_code: status
		body: body
		header: hdr
	}
}

struct TokenBucket {
mut:
	tokens f64
	last   f64
}

struct TokenCreateRequest {
	name   string
	scopes []string
}

struct TokenCreateRequestString {
	name   string
	scopes string
}


fn max_lambdas_per_user() int {
	raw := os.getenv('VLAMBDA_MAX_LAMBDAS')
	if raw == '' { return 20 }
	return raw.int()
}

// ---- HTTP Handler ----

struct VlambdaHandler {
mut:
	db_path string
}

pub fn (mut h VlambdaHandler) handle(req http.Request) http.Response {
	// V's collector is thread-local; collect on the request thread so the
	// per-request bridge allocations do not accumulate on worker threads.
	vlambda_gc_counter++
	if vlambda_gc_every > 0 && vlambda_gc_counter % u64(vlambda_gc_every) == 0 {
		gc_collect()
	}
	// Don't count health/metrics/static/UI requests in total
	if req.url != '/health' && req.url != '/metrics' && req.url != '/metrics/json' && req.url != '/favicon.ico' && req.url != '/' && !req.url.starts_with('/ui') && !req.url.starts_with('/vlambda') {
		vlambda_req_total++
	}
	start_time := time.now()
	// Rate limiting: token bucket per IP (skip health/metrics endpoints).
	if vlambda_rate_limit > 0 && req.url != '/health' && req.url != '/metrics' && req.url != '/metrics/json' && req.url != '/favicon.ico' {
		ip := req.remote_addr.all_before(':')
		now := f64(time.now().unix())
		mut bucket := vlambda_rate_buckets[ip] or { TokenBucket{ tokens: f64(vlambda_rate_limit), last: now } }
		elapsed := now - bucket.last
		if elapsed > 0 {
			bucket.tokens += elapsed * f64(vlambda_rate_limit)
			if bucket.tokens > f64(vlambda_rate_limit) {
				bucket.tokens = f64(vlambda_rate_limit)
			}
		}
		bucket.last = now
		if bucket.tokens < 1 {
			vlambda_rate_buckets[ip] = bucket
			return http.Response{
				status_code: 429
				body: '{"error": "rate limit exceeded"}'
				header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
			}
		}
		bucket.tokens -= 1
		vlambda_rate_buckets[ip] = bucket
		if vlambda_rate_buckets.len > 10000 {
			vlambda_rate_buckets = map[string]TokenBucket{}
		}
	}

	// Reject oversized bodies before any route buffers or parses them.
	if req.data.len > vlambda_max_body_size * 100 {
		return http.Response{
			status_code: 413
			body: '{"error": "request body too large"}'
			header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
		}
	}

	// A reset is in progress: reject new work briefly.
	if vlambda_resetting {
		return http.Response{
			status_code: 503
			body: '{"error": "reset in progress"}'
			header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
		}
	}

	// Memory pressure (set by Python monitor)
	if vlambda_overloaded {
		return http.Response{
			status_code: 503
			body: '{"error": "server overloaded, try again later"}'
			header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
		}
	}

	// Memory check
	if vlambda_max_memory_mb > 0 {
		// Enforcement is done by the Python monitor thread.
	}



	// Route: / (serve UI dashboard)
	if req.url == '/' || req.url == '' {
		mut hdr := http.new_header(http.HeaderConfig{ key: .content_type, value: 'text/html' })
		add_security_headers(mut hdr)
		return http.Response{
			status_code: 200
			body: get_ui_html()
			header: hdr
		}
	}

	// Extract auth token from header. Long-lived API tokens start with "vl_"
	// and are looked up by hash; otherwise the value is a JWT.
	mut auth_token := ''
	if auth_hdr := req.header.get(.authorization) {
		if auth_hdr.starts_with('Bearer ') {
			auth_token = auth_hdr[7..]
		}
	}
	mut auth_uid := -1
	mut auth_scopes := ''
	if auth_token.len > 0 {
		if auth_token.starts_with('vl_') {
			token_info := db_verify_api_token(auth_token)
			if token_info != '' {
				auth_uid = token_info.all_before('|').int()
				auth_scopes = token_info.all_after('|')
			}
		} else {
			auth_uid = auth_verify_token(auth_token)
			if auth_uid >= 0 {
				auth_scopes = 'read,write,admin'
			}
		}
	}
	// Scope enforcement for API tokens. JWTs get every scope.
	if auth_uid >= 0 && auth_scopes != '' {
		if req.url.starts_with('/admin/') && !has_scope(auth_scopes, 'admin') {
			return http.Response{
				status_code: 403
				body: '{"error": "admin scope required"}'
				header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
			}
		}
		is_write := req.method == .post || req.method == .put || req.method == .patch
			|| req.method == .delete
		if is_write && !has_scope(auth_scopes, 'write') && !req.url.starts_with('/tokens') {
			return http.Response{
				status_code: 403
				body: '{"error": "write scope required"}'
				header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
			}
		}
	}

	// Route: /async/<slug> (fire-and-forget, owner only)
	if req.url.starts_with('/async/') {
		if auth_uid < 0 {
			return http.Response{ status_code: 401, body: '{"error": "unauthorized"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		mut url_path := req.url
		qi := url_path.index('?') or { url_path.len }
		url_path = url_path[..qi]
		slug := url_path[7..]
		code_method := db_get_lambda_code(slug)
		if code_method == '' {
			return http.Response{
				status_code: 404
				body: '{"error": "lambda not found"}'
				header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
			}
		}
		if db_get_project(slug) != '' {
			return http.Response{ status_code: 400, body: '{"error": "async is only supported for code lambdas"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		owner := db_get_lambda_owner(slug)
		if owner != auth_uid {
			return http.Response{ status_code: 403, body: '{"error": "forbidden"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		if req.data.len > vlambda_max_body_size {
			return http.Response{ status_code: 413, body: '{"error": "request body too large"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		parts := code_method.split('|')
		code := if parts.len > 0 { parts[0] } else { '' }
		method := if parts.len > 1 { parts[1] } else { 'GET' }
		timeout := if parts.len > 2 { parts[2].int() } else { 0 }
		// memory quota stored but global enforcement only for now.
		mut q := ''
		if qi2 := req.url.index('?') {
			q = req.url[qi2+1..]
		}
		task_id := create_task_async(code, method, req.url, q, req.data, auth_uid, slug, request_meta_json(req))
		return http.Response{
			status_code: 202
			body: task_id
			header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
		}
	}

	// Route: /task/<id> (owner only; admin token sees everything)
	if req.url.starts_with('/task/') {
		if auth_uid < 0 {
			return http.Response{ status_code: 401, body: '{"error": "unauthorized"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		mut task_id := req.url[6..]
		if qi := task_id.index('?') {
			task_id = task_id[..qi]
		}
		owner_id := request_owner(req, auth_uid)
		result := get_task_status(task_id, owner_id)
		return http.Response{
			status_code: 200
			body: result
			header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
		}
	}

	// Route: /tokens (GET = list, POST = create)
	if req.url == '/tokens' || req.url.starts_with('/tokens?') {
		if auth_uid < 0 {
			return http.Response{ status_code: 401, body: '{"error": "unauthorized"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		if req.method == .post {
			// Creating tokens requires the admin scope when using a PAT.
			if auth_scopes != '' && !has_scope(auth_scopes, 'admin') {
				return http.Response{ status_code: 403, body: '{"error": "admin scope required"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
			}
			mut token_name := ''
			mut token_scopes := 'read,write'
			if req.data.len > 0 {
				parsed := json2.decode[TokenCreateRequest](req.data, json2.DecoderOptions{}) or {
					TokenCreateRequest{}
				}
				token_name = parsed.name
				if parsed.scopes.len > 0 {
					token_scopes = parsed.scopes.join(',')
				} else {
					// Also accept "scopes": "read,write".
					str_parsed := json2.decode[TokenCreateRequestString](req.data, json2.DecoderOptions{}) or {
						TokenCreateRequestString{}
					}
					if str_parsed.scopes != '' && !str_parsed.scopes.starts_with('[') {
						token_scopes = str_parsed.scopes
					}
				}
			}
			if req.url.contains('?') {
				qs := req.url.all_after('?')
				for part in qs.split('&') {
					if part.starts_with('name=') {
						token_name = decode_query_value(part.all_after('='))
					}
					if part.starts_with('scopes=') {
						token_scopes = decode_query_value(part.all_after('='))
					}
				}
			}
			mut valid_scopes := []string{}
			for scope in token_scopes.split(',') {
				s := scope.trim_space()
				if s == 'read' || s == 'write' || s == 'admin' {
					if s !in valid_scopes {
						valid_scopes << s
					}
				}
			}
			if valid_scopes.len == 0 {
				return http.Response{ status_code: 400, body: '{"error": "no valid scopes"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
			}
			result := db_create_api_token(auth_uid, token_name, valid_scopes.join(','))
			if result.starts_with('{"error"') {
				return http.Response{ status_code: 500, body: result, header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
			}
			return http.Response{ status_code: 201, body: result, header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		if req.method == .get {
			body := '{"tokens": ' + db_list_api_tokens(auth_uid) + '}'
			return http.Response{ status_code: 200, body: body, header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		return http.Response{ status_code: 405, body: '{"error": "method not allowed"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
	}

	// Route: /tokens/<id> (DELETE = revoke)
	if req.url.starts_with('/tokens/') {
		if auth_uid < 0 {
			return http.Response{ status_code: 401, body: '{"error": "unauthorized"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		if auth_scopes != '' && !has_scope(auth_scopes, 'admin') {
			return http.Response{ status_code: 403, body: '{"error": "admin scope required"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		if req.method == .delete {
			mut url_path := req.url
			qi := url_path.index('?') or { url_path.len }
			url_path = url_path[..qi]
			id := url_path[8..].int()
			if id <= 0 {
				return http.Response{ status_code: 400, body: '{"error": "invalid token id"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
			}
			if db_revoke_api_token(auth_uid, id) {
				return http.Response{ status_code: 200, body: '{"revoked": true}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
			}
			return http.Response{ status_code: 404, body: '{"error": "token not found"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		return http.Response{ status_code: 405, body: '{"error": "method not allowed"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
	}

	// Route: /dlq
	if req.url == '/dlq' {
		if auth_uid < 0 {
			return http.Response{
				status_code: 401
				body: '{"error": "unauthorized"}'
				header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
			}
		}
		if vlambda_get_dlq == unsafe { nil } {
			return http.Response{ status_code: 500, body: '{"error": "not configured"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		owner_id := request_owner(req, auth_uid)
		mut body := call_py_strs(vlambda_get_dlq, [owner_id.str()])
		if body == '' {
			body = '[]'
		}
		return http.Response{ status_code: 200, body: body, header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
	}

	// Route: /openapi.json (owner scoped unless public)
	if req.url == '/openapi.json' {
		public := os.getenv('VLAMBDA_OPENAPI_PUBLIC') in ['1', 'true', 'yes', 'on']
		if auth_uid < 0 && !public {
			return http.Response{ status_code: 401, body: '{"error": "unauthorized"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		owner_id := if public && auth_uid < 0 { 0 } else { request_owner(req, auth_uid) }
		scheme := req.header.get_custom('X-Forwarded-Proto', http.HeaderQueryConfig{}) or { 'http' }
		base_url := scheme + '://' + req.host
		mut body := '{"error": "not configured"}'
		if vlambda_openapi != unsafe { nil } {
			state := vcraft.gil_ensure()
			args := [vcraft.to_py_string(owner_id.str()).ptr, vcraft.to_py_string(base_url).ptr]
			r := vcraft.call(vlambda_openapi, args)
			for a in args { vcraft.borrow(a).decref() }
			if r.is_null() {
				vcraft.clear_error()
			} else if r.type_name() == 'str' {
				ss := py_string_copy(r, 'result', '{"error": "openapi failed"}')
				r.decref()
				body = ss
			} else {
				r.decref()
			}
			vcraft.gil_release(state)
		}
		status := if body.starts_with('{"error"') { 500 } else { 200 }
		return http.Response{ status_code: status, body: body, header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
	}

	// Route: /config (public runtime flags for the UI)
	if req.url == '/config' && req.method == .get {
		reg := if vlambda_registration_enabled { 'true' } else { 'false' }
		return http.Response{
			status_code: 200
			body: '{"registration": ${reg}}'
			header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
		}
	}

	// Route: /health
	if req.url == '/health' {
		return http.Response{
			status_code: 200
			body: '{"status": "ok", "version": "${vlambda_version}"}'
			header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
		}
	}

	// Route: /ready
	if req.url == '/ready' {
		if vlambda_draining || vlambda_executor == unsafe { nil } {
			return http.Response{
				status_code: 503
				body: '{"status": "not ready"}'
				header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
			}
		}
		return http.Response{
			status_code: 200
			body: '{"status": "ready", "version": "${vlambda_version}"}'
			header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
		}
	}

	// Route: /shutdown (POST, admin token)
	if req.url == '/shutdown' && req.method == .post {
		if !is_admin_request(req) {
			return http.Response{
				status_code: 403
				body: '{"error": "forbidden"}'
				header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
			}
		}
		vlambda_draining = true
		if vlambda_shutdown != unsafe { nil } {
			state := vcraft.gil_ensure()
			args := []voidptr{}
			r := vcraft.call(vlambda_shutdown, args)
			if !r.is_null() { r.decref() }
			vcraft.gil_release(state)
		}
		spawn fn () {
			time.sleep(150 * time.millisecond)
			C.exit(0)
		}()
		return http.Response{
			status_code: 200
			body: '{"status": "shutting down"}'
			header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
		}
	}

	// Route: /ui (serve HTML dashboard)
	if req.url == '/ui' || req.url == '/' {
		mut hdr := http.new_header(http.HeaderConfig{ key: .content_type, value: 'text/html' })
		add_security_headers(mut hdr)
		return http.Response{
			status_code: 200
			body: get_ui_html()
			header: hdr
		}
	}

	// Route: /assets/<name>
	if req.url.starts_with('/assets/') {
		mut name := req.url[8..]
		qi := name.index('?') or { name.len }
		name = name[..qi]
		body := get_asset(name)
		if body == '' {
			return http.Response{
				status_code: 404
				body: 'asset not found'
				header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'text/plain' })
			}
		}
		mut ctype := 'application/octet-stream'
		if name.ends_with('.js') { ctype = 'application/javascript' }
		if name.ends_with('.css') { ctype = 'text/css' }
		if name.ends_with('.html') { ctype = 'text/html' }
		if name.ends_with('.ico') { ctype = 'image/x-icon' }
		mut hdr := http.new_header(http.HeaderConfig{ key: .content_type, value: ctype })
		add_security_headers(mut hdr)
		return http.Response{
			status_code: 200
			body: body
			header: hdr
		}
	}

	// Route: /favicon.ico
	if req.url == '/favicon.ico' {
		// The favicon belongs to the vlambda UI only. Browsers request it
		// automatically for every page, including lambda invocations at /f/<slug>,
		// so skip it when the request comes from a lambda page.
		if ref := req.header.get(.referer) {
			if ref.contains('/f/') {
				return http.Response{
					status_code: 404
					body: ''
					header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'text/plain' })
				}
			}
		}
		icon := get_favicon()
		return http.Response{
			status_code: 200
			body: icon
			header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'image/x-icon' })
		}
	}

	// Route: /logs/history (persisted lambda logs)
	if req.url.starts_with('/logs/history') {
		if auth_uid < 0 {
			return http.Response{
				status_code: 401
				body: '{"error": "unauthorized"}'
				header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
			}
		}
		if req.method != .get && req.method != .delete {
			return http.Response{ status_code: 405, body: '{"error": "method not allowed"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		mut days := if req.method == .delete { 0 } else { 1 }
		mut slug := ''
		if req.url.contains('?') {
			qs := req.url.all_after('?')
			for part in qs.split('&') {
				if part.starts_with('days=') { days = part.all_after('=').int() }
				if part.starts_with('slug=') { slug = decode_query_value(part.all_after('=')) }
			}
		}
		owner_id := request_owner(req, auth_uid)
		if req.method == .delete {
			mut body := call_py_strs(vlambda_delete_logs, [days.str(), slug, owner_id.str()])
			if body == '' {
				body = '{"deleted": 0}'
			}
			return http.Response{ status_code: 200, body: body, header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		mut body := call_py_strs(vlambda_query_logs, [days.str(), slug, owner_id.str()])
		if body == '' {
			body = '{"days": ${days}, "count": 0, "items": []}'
		}
		return http.Response{ status_code: 200, body: body, header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
	}

	// Route: /metrics/json (JSON format for UI charts, owner scoped)
	if req.url == '/metrics/json' {
		if auth_uid < 0 {
			return http.Response{
				status_code: 401
				body: '{"error": "unauthorized"}'
				header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
			}
		}
		owner_id := request_owner(req, auth_uid)
		mut allowed := map[string]bool{}
		if owner_id != 0 {
			for line in db_list_lambdas(owner_id).split('\n') {
				if line != '' {
					allowed[line.all_before('|')] = true
				}
			}
		}
		mut total_req := u64(0)
		mut total_err := u64(0)
		mut total_lat := f64(0)
		mut items := []string{}
		for slug, count in vlambda_lambda_invocations {
			if owner_id != 0 && slug !in allowed { continue }
			errs := vlambda_lambda_errors[slug] or { u64(0) }
			lat := vlambda_lambda_latency[slug] or { f64(0) }
			avg_lat := if count > 0 { lat / f64(count) } else { 0.0 }
			total_req += count
			total_err += errs
			total_lat += lat
			items << '{"slug": "${slug}", "invocations": ${count}, "errors": ${errs}, "latency_avg_ms": ${avg_lat}}'
		}
		avg := if total_req > 0 { total_lat / f64(total_req) } else { 0.0 }
		body := '{"requests_total": ${total_req}, "errors_total": ${total_err}, "lambdas_created": ${vlambda_lambdas_created}, "latency_avg_ms": ${avg}, "latency_count": ${total_req}, "per_lambda": [${items.join(',')}]}'
		return http.Response{
			status_code: 200
			body: body
			header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
		}
	}

	// Route: /metrics (public aggregate counters; no per-lambda labels)
	if req.url == '/metrics' {
		avg := if vlambda_latency_count > 0 { vlambda_latency_sum / f64(vlambda_latency_count) } else { 0.0 }
		body := 'vlambda_requests_total ${vlambda_req_total}\nvlambda_errors_total ${vlambda_req_errors}\nvlambda_lambdas_created_total ${vlambda_lambdas_created}\nvlambda_latency_avg_ms ${avg}\nvlambda_latency_count ${vlambda_latency_count}\n'
		return http.Response{
			status_code: 200
			body: body
			header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'text/plain' })
		}
	}


	// Route: /metrics/history (persisted metrics from separate DB)
	if req.url.starts_with('/metrics/history') {
		if auth_uid < 0 {
			return http.Response{
				status_code: 401
				body: '{"error": "unauthorized"}'
				header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
			}
		}
		if req.method != .get && req.method != .delete {
			return http.Response{ status_code: 405, body: '{"error": "method not allowed"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		mut days := if req.method == .delete { 0 } else { 7 }
		mut slug := ''
		if req.url.contains('?') {
			qs := req.url.all_after('?')
			for part in qs.split('&') {
				if part.starts_with('days=') {
					days = part.all_after('=').int()
				}
				if part.starts_with('slug=') {
					slug = decode_query_value(part.all_after('='))
				}
			}
		}
		owner_id := request_owner(req, auth_uid)
		if req.method == .delete {
			mut body := call_py_strs(vlambda_delete_metrics, [days.str(), slug, owner_id.str()])
			if body == '' {
				body = '{"deleted": 0}'
			}
			return http.Response{ status_code: 200, body: body, header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		mut body := call_py_strs(vlambda_query_metrics, [days.str(), owner_id.str()])
		if body == '' {
			body = '{"days": ${days}, "items": []}'
		}
		return http.Response{
			status_code: 200
			body: body
			header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
		}
	}


	// Route: /metrics/stats (aggregated persisted metrics)
	if req.url.starts_with('/metrics/stats') {
		if auth_uid < 0 {
			return http.Response{
				status_code: 401
				body: '{"error": "unauthorized"}'
				header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
			}
		}
		mut days := 7
		mut slug := ''
		if req.url.contains('?') {
			qs := req.url.all_after('?')
			for part in qs.split('&') {
				if part.starts_with('days=') { days = part.all_after('=').int() }
				if part.starts_with('slug=') { slug = decode_query_value(part.all_after('=')) }
			}
		}
		owner_id := request_owner(req, auth_uid)
		mut body := call_py_strs(vlambda_query_stats, [days.str(), slug, owner_id.str()])
		if body == '' {
			body = '{"days": ${days}, "count": 0, "avg_ms": 0, "p50_ms": 0, "p95_ms": 0, "p99_ms": 0}'
		}
		return http.Response{
			status_code: 200
			body: body
			header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
		}
	}


	// Route: /metrics/aggregate (persisted per-lambda aggregates)
	if req.url.starts_with('/metrics/aggregate') {
		if auth_uid < 0 {
			return http.Response{
				status_code: 401
				body: '{"error": "unauthorized"}'
				header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
			}
		}
		mut days := 7
		mut slug := ''
		if req.url.contains('?') {
			qs := req.url.all_after('?')
			for part in qs.split('&') {
				if part.starts_with('days=') { days = part.all_after('=').int() }
				if part.starts_with('slug=') { slug = decode_query_value(part.all_after('=')) }
			}
		}
		owner_id := request_owner(req, auth_uid)
		mut body := call_py_strs(vlambda_query_aggregate, [days.str(), slug, owner_id.str()])
		if body == '' {
			body = '{"days": ${days}, "requests_total": 0, "errors_total": 0, "cold_starts": 0, "per_lambda": []}'
		}
		return http.Response{ status_code: 200, body: body, header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
	}

	// Route: /metrics/summary (aggregated persisted metrics)
	if req.url.starts_with('/metrics/summary') {
		if auth_uid < 0 {
			return http.Response{
				status_code: 401
				body: '{"error": "unauthorized"}'
				header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
			}
		}
		mut days := 7
		mut slug := ''
		if req.url.contains('?') {
			qs := req.url.all_after('?')
			for part in qs.split('&') {
				if part.starts_with('days=') { days = part.all_after('=').int() }
				if part.starts_with('slug=') { slug = decode_query_value(part.all_after('=')) }
			}
		}
		owner_id := request_owner(req, auth_uid)
		mut body := call_py_strs(vlambda_query_summary, [days.str(), slug, owner_id.str()])
		if body == '' {
			body = '{"days": ${days}, "items": []}'
		}
		return http.Response{
			status_code: 200
			body: body
			header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
		}
	}

	// Route: /metrics/export (CSV)
	if req.url.starts_with('/metrics/export') {
		if auth_uid < 0 {
			return http.Response{
				status_code: 401
				body: '{"error": "unauthorized"}'
				header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
			}
		}
		mut days := 7
		mut slug := ''
		if req.url.contains('?') {
			qs := req.url.all_after('?')
			for part in qs.split('&') {
				if part.starts_with('days=') { days = part.all_after('=').int() }
				if part.starts_with('slug=') { slug = decode_query_value(part.all_after('=')) }
			}
		}
		owner_id := request_owner(req, auth_uid)
		body := call_py_strs(vlambda_export_metrics, [days.str(), slug, owner_id.str()])
		return http.Response{
			status_code: 200
			body: body
			header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'text/csv' })
		}
	}

	// Route: /pool/stats
	if req.url == '/pool/stats' {
		if auth_uid < 0 {
			return http.Response{
				status_code: 401
				body: '{"error": "unauthorized"}'
				header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
			}
		}
		mut body := call_py_strs(vlambda_pool_stats, [])
		if body == '' {
			body = '{}'
		}
		return http.Response{
			status_code: 200
			body: body
			header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
		}
	}

	// Route: /admin/reset (POST, token protected)
	if req.url == '/admin/reset' && req.method == .post {
		if !is_admin_request(req) {
			return http.Response{ status_code: 403, body: '{"error": "forbidden"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		token := req.header.get_custom('X-Admin-Token', http.HeaderQueryConfig{}) or { '' }
		vlambda_resetting = true
		mut body := call_py_strs(vlambda_admin_reset, [token])
		if body == '' {
			body = '{}'
		}
		vlambda_resetting = false
	vlambda_draining = false
		return http.Response{
			status_code: 200
			body: body
			header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
		}
	}

	// Route: /admin/users (POST, token protected)
	if req.url == '/admin/users' && req.method == .post {
		if !is_admin_request(req) {
			return http.Response{ status_code: 403, body: '{"error": "forbidden"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		token := req.header.get_custom('X-Admin-Token', http.HeaderQueryConfig{}) or { '' }
		parts := req.data.split(':')
		if parts.len < 2 {
			return http.Response{
				status_code: 400
				body: '{"error": "expected username:password"}'
				header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
			}
		}
		mut body := call_py_strs(vlambda_admin_create_user, [token, parts[0], parts[1]])
		if body == '' {
			body = '{"error": "failed"}'
		}
		status := if body.contains('"user_id"') {
			201
		} else if body.contains('user already exists') {
			409
		} else if body.contains('password does not meet policy') {
			400
		} else {
			403
		}
		return http.Response{
			status_code: status
			body: body
			header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
		}
	}

	// Route: /register (POST)
	if req.url == '/register' && req.method == .post {
		if !vlambda_registration_enabled {
			return http.Response{
				status_code: 403
				body: '{"error": "registration is disabled"}'
				header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
			}
		}
		// Parse username:password from body
		parts := req.data.split(':')
		if parts.len < 2 {
			return http.Response{
				status_code: 400
				body: '{"error": "expected username:password"}'
				header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
			}
		}
		uid := auth_register_user(parts[0], parts[1], req.remote_addr.all_before(':'))
		if uid == -2 {
			return http.Response{
				status_code: 429
				body: '{"error": "registration rate limit exceeded"}'
				header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
			}
		}
		if uid == -3 {
			return http.Response{
				status_code: 400
				body: '{"error": "password does not meet policy"}'
				header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
			}
		}
		if uid < 0 {
			return http.Response{
				status_code: 409
				body: '{"error": "user already exists"}'
				header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
			}
		}
		return http.Response{
			status_code: 201
			body: '{"user_id": ${uid}}'
			header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
		}
	}

	// Route: /login (POST)
	if req.url == '/login' && req.method == .post {
		parts := req.data.split(':')
		if parts.len < 2 {
			return http.Response{
				status_code: 400
				body: '{"error": "expected username:password"}'
				header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
			}
		}
		token := auth_login_user(parts[0], parts[1], req.remote_addr.all_before(':'))
		if token == '' {
			return http.Response{
				status_code: 401
				body: '{"error": "invalid credentials"}'
				header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
			}
		}
		return http.Response{
			status_code: 200
			body: '{"token": "${token}"}'
			header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
		}
	}

	// Route: /project/<slug> (GET = read files, PUT = save files)
	if req.url.starts_with('/project/') {
		if auth_uid < 0 {
			return http.Response{ status_code: 401, body: '{"error": "unauthorized"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		mut url_path := req.url
		qi := url_path.index('?') or { url_path.len }
		url_path = url_path[..qi]
		if url_path.len < 10 {
			return http.Response{ status_code: 400, body: '{"error": "missing slug"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		slug := url_path[9..].all_before('/')
		owner := db_get_lambda_owner(slug)
		if owner < 0 {
			return http.Response{ status_code: 404, body: '{"error": "project not found"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		if owner != auth_uid {
			return http.Response{ status_code: 403, body: '{"error": "forbidden"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		project_info := db_get_project(slug)
		if project_info == '' {
			return http.Response{ status_code: 404, body: '{"error": "project not found"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		project_dir := project_info.all_before('|')
		rest_info := project_info.all_after('|')
		entrypoint := rest_info.all_before('|')
		method := rest_info.all_after('|')
		if req.method == .get {
			mut body := call_py_strs(vlambda_get_project_json, [project_dir, entrypoint, slug, method])
			if body == '' {
				body = '{}'
			}
			return http.Response{ status_code: 200, body: body, header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		if req.method == .put {
			if req.data.len > vlambda_max_body_size * 2 {
				return http.Response{ status_code: 413, body: '{"error": "project too large"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
			}
			request_body := req.data
			mut body := call_py_strs(vlambda_save_project_json, [project_dir, request_body, entrypoint])
			if body == '' {
				body = '{}'
			}
			return http.Response{ status_code: 200, body: body, header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		return http.Response{ status_code: 405, body: '{"error": "method not allowed"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
	}

	// Route: /vlambda (POST = create, GET = list)
	if req.url == '/vlambda' || req.url.starts_with('/vlambda?') {
		if req.method == .post {
			if auth_uid < 0 {
				return http.Response{
					status_code: 401
					body: '{"error": "unauthorized"}'
					header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
				}
			}
			// Project upload (multi-file zip)
			if req.url.contains('project=1') || req.url.contains('project=true') {
				if req.data.len > vlambda_max_body_size * 100 {
					return http.Response{ status_code: 413, body: '{"error": "project too large"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
				}
				mut entrypoint := 'main.py'
				mut name := ''
				mut method := 'GET'
				if req.url.contains('?') {
					qs := req.url.all_after('?')
					for part in qs.split('&') {
						if part.starts_with('entrypoint=') || part.starts_with('entry=') {
							entrypoint = decode_query_value(part.all_after('='))
						}
						if part.starts_with('name=') {
							name = decode_query_value(part.all_after('=')).trim_space()
						}
						if part.starts_with('method=') {
							method = decode_query_value(part.all_after('='))
						}
					}
				}
				method = normalize_method(method)
				if vlambda_create_project == unsafe { nil } {
					return http.Response{
						status_code: 500
						body: '{"error": "project creation not configured"}'
						header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
					}
				}
				mut result := '{"error": "project creation failed"}'
				state := vcraft.gil_ensure()
				args := [vcraft.to_py_string(req.data).ptr, vcraft.to_py_string(entrypoint).ptr, vcraft.to_py_string(name).ptr, vcraft.to_py_string(method).ptr, vcraft.to_py_int(auth_uid).ptr]
				r := vcraft.call(vlambda_create_project, args)
				for a in args { vcraft.borrow(a).decref() }
				if !r.is_null() {
					if r.type_name() == 'str' {
						result = py_string_copy(r, 'result', '{"error": "bad result"}')
					}
					r.decref()
				} else {
					vcraft.clear_error()
				}
				vcraft.gil_release(state)
				return http.Response{
					status_code: 201
					body: result
					header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
				}
			}
			// Create lambda
			// Body: {"code": "...", "method": "GET"}
			// For now, read code from body directly
			code := req.data
			if code.len == 0 {
				return http.Response{
					status_code: 400
					body: '{"error": "missing code in body"}'
					header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
				}
			}
			if code.len > vlambda_max_body_size {
				return http.Response{
					status_code: 413
					body: '{"error": "request body too large"}'
					header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
				}
			}
			// Parse method and name from query string
			mut method := 'GET'
			mut name := ''
			mut timeout := 0
			mut memory_mb := 0
			if req.url.contains('?') {
				qs := req.url.all_after('?')
				for part in qs.split('&') {
					if part.starts_with('method=') {
						method = decode_query_value(part.all_after('='))
					}
					if part.starts_with('name=') {
						name = decode_query_value(part.all_after('=')).trim_space()
					}
					if part.starts_with('timeout=') {
						timeout = part.all_after('=').int()
					}
					if part.starts_with('memory_mb=') {
						memory_mb = part.all_after('=').int()
					}
				}
			}
			method = normalize_method(method)
			// Validate code syntax
			vr := validate_code(code)
			if vr != '' {
				return http.Response{
					status_code: 400
					body: '{"error": "${vr}"}'
					header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
				}
			}
			// Validate Python code compiles (basic check: must contain 'def handler')
			if !code.contains('def handler') {
				return http.Response{
					status_code: 400
					body: '{"error": "code must define a handler function"}'
					header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
				}
			}
			if db_count_lambdas(auth_uid) >= max_lambdas_per_user() {
				return http.Response{
					status_code: 429
					body: '{"error": "lambda quota exceeded"}'
					header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
				}
			}
			slug := make_slug(code)
			ok := db_create_lambda(slug, name, code, method, auth_uid, timeout, memory_mb)
			if ok {
				prewarm_lambda(code)
				vlambda_lambdas_created++
			}
			if !ok {
				return http.Response{
					status_code: 500
					body: '{"error": "failed to store lambda"}'
					header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
				}
			}
			return http.Response{
				status_code: 201
				body: '{"slug": "${slug}", "method": "${method}"}'
				header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
			}
		}
		if req.method == .get {
			if auth_uid < 0 {
				return http.Response{
					status_code: 401
					body: '{"error": "unauthorized"}'
					header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
				}
			}
			// Parse limit/offset from query string
			mut limit := 50
			mut offset := 0
			mut as_json := false
			if req.url.contains('?') {
				qs := req.url.all_after('?')
				for part in qs.split('&') {
					if part.starts_with('limit=') {
						limit = part.all_after('=').int()
					}
					if part.starts_with('offset=') {
						offset = part.all_after('=').int()
					}
					if part == 'format=json' {
						as_json = true
					}
				}
			}
			count := db_count_lambdas(auth_uid)
			if as_json {
				items := db_list_lambdas_json(auth_uid, limit, offset)
				return http.Response{
					status_code: 200
					body: '{"lambdas": ${items}, "count": ${count}, "limit": ${limit}, "offset": ${offset}}'
					header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
				}
			}
			lambdas := db_list_lambdas_page(auth_uid, limit, offset)
			return http.Response{
				status_code: 200
				body: '{"lambdas": ${json_str(lambdas)}, "count": ${count}, "limit": ${limit}, "offset": ${offset}}'
				header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
			}
		}
		return http.Response{
			status_code: 405
			body: 'method not allowed'
			header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'text/plain' })
		}
	}

	// Route: /f/<slug>/<path> (invoke lambda or serve project files)
	if req.url.starts_with('/f/') {
		// strip query string
		mut url_path := req.url
		qi := url_path.index('?') or { url_path.len }
		url_path = url_path[..qi]
		rest := url_path[3..]
		slug := rest.all_before('/')
		mut sub := ''
		if rest.contains('/') {
			sub = rest.all_after('/')
		}
		if req.data.len > vlambda_max_body_size {
			return cors_json(413, '{"error": "request body too large"}')
		}
		// CORS preflight never reaches the lambda.
		if req.method == .options {
			return cors_response(204, '', 'text/plain')
		}
		mut alias := ''
		if req.url.contains('?') {
			qs := req.url.all_after('?')
			for part in qs.split('&') {
				if part.starts_with('alias=') {
					alias = decode_query_value(part.all_after('='))
				}
			}
		}
		code_method := if alias != '' {
			resolved := db_resolve_alias(slug + ':' + alias)
			if resolved == '' { '' } else {
				aparts := resolved.split('|')
				aslug := if aparts.len > 0 { aparts[0] } else { '' }
				aver := if aparts.len > 1 { aparts[1].int() } else { 0 }
				if aslug != slug { '' } else { db_get_version_code(slug, aver) }
			}
		} else { db_get_lambda_code(slug) }
		parts := code_method.split('|')
		code := if parts.len > 0 { parts[0] } else { '' }
		method := if parts.len > 1 { parts[1] } else { 'GET' }
		timeout := if parts.len > 2 { parts[2].int() } else { 0 }
		memory_mb := if parts.len > 3 { parts[3].int() } else { 0 }
		version := if parts.len > 4 { parts[4] } else { '0' }
		kind := if parts.len > 5 { parts[5] } else { 'code' }
		// Only projects need the project lookup.
		project_info := if kind == 'project' || code == '' { db_get_project(slug) } else { '' }
		if code == '' && project_info == '' {
			vlambda_req_errors++
			return cors_json(404, '{"error": "lambda not found"}')
		}
		if project_info != '' {
			pparts := project_info.split('|')
			project_dir := if pparts.len > 0 { pparts[0] } else { '' }
			entrypoint := if pparts.len > 1 { pparts[1] } else { 'main.py' }
			project_method := if pparts.len > 2 { pparts[2] } else { 'GET' }
			// Serve project files: /f/<slug>/<path>, and index.html for the root.
			if req.method == .get || req.method == .head {
				if sub != '' {
					if !is_safe_static_path(sub) {
						return cors_json(400, '{"error": "invalid path"}')
					}
					if file_resp := serve_project_file(project_dir, sub) {
						mut resp := file_resp
						add_cors_headers(mut resp.header)
						return resp
					}
				} else if rest.ends_with('/') {
					if file_resp := serve_project_file(project_dir, 'index.html') {
						mut resp := file_resp
						add_cors_headers(mut resp.header)
						return resp
					}
				}
			}
			if project_method != req.method.str() {
				return cors_json(405, '{"error": "method not allowed, expected ${project_method}"}')
			}
			mut q := ''
			if qi2 := req.url.index('?') {
				q = req.url[qi2+1..]
			}
			vlambda_request_counter++
			request_id := 'req-${vlambda_request_counter}'
			result := run_project(project_dir, entrypoint, project_method, req.url, q, req.data, request_id, slug, request_meta_json(req))
			elapsed := time.since(start_time).microseconds()
			vlambda_latency_sum += f64(elapsed) / 1000.0
			vlambda_latency_count++
			vlambda_lambda_invocations[slug] = (vlambda_lambda_invocations[slug] or { u64(0) }) + 1
			vlambda_lambda_latency[slug] = (vlambda_lambda_latency[slug] or { f64(0) }) + f64(elapsed) / 1000.0
			if result.contains('error') {
				vlambda_lambda_errors[slug] = (vlambda_lambda_errors[slug] or { u64(0) }) + 1
			}
			mut resp := build_lambda_response(result)
			add_cors_headers(mut resp.header)
			resp.header.add_custom('X-Request-Id', request_id) or {}
			return resp
		}
		// Check method
		if method != req.method.str() {
			return cors_json(405, '{"error": "method not allowed, expected ${method}"}')
		}
		// Run lambda via Python bridge
		// extract query string from url
		mut q := ''
		if qi2 := req.url.index('?') {
			q = req.url[qi2+1..]
		}
		vlambda_request_counter++
		request_id := 'req-${vlambda_request_counter}'
		code_key := slug + ':' + version
		result := run_lambda(request_id, slug, code, method, req.url, q, req.data, timeout, memory_mb, request_meta_json(req), code_key)
		elapsed := time.since(start_time).microseconds()
		vlambda_latency_sum += f64(elapsed) / 1000.0
		vlambda_latency_count++
		// Per-lambda metrics (optional)
		if vlambda_metrics_enabled {
			vlambda_lambda_invocations[slug] = (vlambda_lambda_invocations[slug] or { u64(0) }) + 1
			vlambda_lambda_latency[slug] = (vlambda_lambda_latency[slug] or { f64(0) }) + f64(elapsed) / 1000.0
			if result.contains('error') {
				vlambda_lambda_errors[slug] = (vlambda_lambda_errors[slug] or { u64(0) }) + 1
			}
		}
		mut resp := build_lambda_response(result)
		add_cors_headers(mut resp.header)
		resp.header.add_custom('X-Request-Id', request_id) or {}
		return resp
	}


	// Route: /vlambda/<slug>/env (GET list, PUT save)
	if req.url.starts_with('/vlambda/') && req.url.contains('/env') {
		if auth_uid < 0 {
			return http.Response{ status_code: 401, body: '{"error": "unauthorized"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		mut url_path := req.url
		qi := url_path.index('?') or { url_path.len }
		url_path = url_path[..qi]
		slug := url_path[9..].all_before('/env')
		owner := db_get_lambda_owner(slug)
		if owner < 0 {
			return http.Response{ status_code: 404, body: '{"error": "lambda not found"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		if owner != auth_uid {
			return http.Response{ status_code: 403, body: '{"error": "forbidden"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		if req.method == .get {
			mut body := call_py_strs(vlambda_list_env, [slug, auth_uid.str()])
			if body == '' {
				body = '{"env": []}'
			}
			return http.Response{ status_code: 200, body: body, header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		if req.method == .put {
			if req.data.len > vlambda_max_body_size {
				return http.Response{ status_code: 413, body: '{"error": "environment too large"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
			}
			request_body := req.data
			mut body := call_py_strs(vlambda_save_env, [slug, auth_uid.str(), request_body])
			if body == '' {
				body = '{"error": "save failed"}'
			}
			status := if body.starts_with('{"env"') { 200 } else { 400 }
			return http.Response{ status_code: status, body: body, header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		return http.Response{ status_code: 405, body: '{"error": "method not allowed"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
	}

	// Route: /vlambda/<slug>/kv (GET list, DELETE clear)
	// Route: /vlambda/<slug>/kv/<key> (GET, PUT, DELETE)
	if req.url.starts_with('/vlambda/') && req.url.contains('/kv') {
		if auth_uid < 0 {
			return http.Response{ status_code: 401, body: '{"error": "unauthorized"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		mut q := ''
		if req.url.contains('?') {
			q = req.url.all_after('?')
		}
		mut url_path := req.url
		qi := url_path.index('?') or { url_path.len }
		url_path = url_path[..qi]
		slug := url_path[9..].all_before('/kv')
		mut key := url_path.all_after('/kv')
		if key.starts_with('/') {
			key = key[1..]
		}
		if key.contains('/') {
			return http.Response{ status_code: 400, body: '{"error": "invalid key"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		key = decode_query_value(key)
		owner := db_get_lambda_owner(slug)
		if owner < 0 {
			return http.Response{ status_code: 404, body: '{"error": "lambda not found"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		if owner != auth_uid {
			return http.Response{ status_code: 403, body: '{"error": "forbidden"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		if req.method != .get && req.method != .put && req.method != .delete {
			return http.Response{ status_code: 405, body: '{"error": "method not allowed"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		request_body := req.data
		if req.method == .put && request_body.len > vlambda_max_body_size {
			return http.Response{ status_code: 413, body: '{"error": "request body too large"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		mut body := call_py_strs(vlambda_kv, [slug, auth_uid.str(), req.method.str(), key, request_body, q])
		if body == '' {
			body = '{"error": "kv failed"}'
		}
		status := if body.starts_with('{"error"') { 400 } else { 200 }
		return http.Response{ status_code: status, body: body, header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
	}

	// Route: /vlambda/<slug>/egress (GET list, PUT replace)
	if req.url.starts_with('/vlambda/') && req.url.contains('/egress') {
		if auth_uid < 0 {
			return http.Response{ status_code: 401, body: '{"error": "unauthorized"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		mut url_path := req.url
		qi := url_path.index('?') or { url_path.len }
		url_path = url_path[..qi]
		slug := url_path[9..].all_before('/egress')
		owner := db_get_lambda_owner(slug)
		if owner < 0 {
			return http.Response{ status_code: 404, body: '{"error": "lambda not found"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		if owner != auth_uid {
			return http.Response{ status_code: 403, body: '{"error": "forbidden"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		if req.method != .get && req.method != .put {
			return http.Response{ status_code: 405, body: '{"error": "method not allowed"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		request_body := req.data
		if req.method == .put && request_body.len > vlambda_max_body_size {
			return http.Response{ status_code: 413, body: '{"error": "request body too large"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		mut body := call_py_strs(vlambda_egress, [slug, auth_uid.str(), req.method.str(), request_body])
		if body == '' {
			body = '{"error": "egress failed"}'
		}
		status := if body.starts_with('{"hosts"') { 200 } else { 400 }
		return http.Response{ status_code: status, body: body, header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
	}

	// Route: /vlambda/<slug>/schedules[/<id>[/run|/runs]]
	if req.url.starts_with('/vlambda/') && req.url.contains('/schedules') {
		if auth_uid < 0 {
			return http.Response{ status_code: 401, body: '{"error": "unauthorized"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		mut url_path := req.url
		qi := url_path.index('?') or { url_path.len }
		url_path = url_path[..qi]
		slug := url_path[9..].all_before('/schedules')
		rest := url_path.all_after('/schedules').trim_left('/')
		mut schedule_id := ''
		mut action := ''
		if rest != '' {
			parts := rest.split('/')
			schedule_id = parts[0]
			if parts.len > 1 {
				action = parts[1]
			}
		}
		owner := db_get_lambda_owner(slug)
		if owner < 0 {
			return http.Response{ status_code: 404, body: '{"error": "lambda not found"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		if owner != auth_uid {
			return http.Response{ status_code: 403, body: '{"error": "forbidden"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		if action == 'run' && req.method != .post {
			return http.Response{ status_code: 405, body: '{"error": "method not allowed"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		if action == 'runs' && req.method != .get {
			return http.Response{ status_code: 405, body: '{"error": "method not allowed"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		if action == '' && schedule_id != '' && req.method != .put && req.method != .delete {
			return http.Response{ status_code: 405, body: '{"error": "method not allowed"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		request_body := req.data
		if request_body.len > vlambda_max_body_size {
			return http.Response{ status_code: 413, body: '{"error": "request body too large"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		mut body := call_py_strs(vlambda_schedules, [slug, auth_uid.str(), action, req.method.str(), schedule_id, request_body])
		if body == '' {
			body = '{"error": "schedules failed"}'
		}
		status := if body.starts_with('{"error"') { 400 } else { 200 }
		return http.Response{ status_code: status, body: body, header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
	}

	// Route: /vlambda/<slug>/meta (GET/PUT OpenAPI metadata)
	if req.url.starts_with('/vlambda/') && req.url.contains('/meta') {
		if auth_uid < 0 {
			return http.Response{ status_code: 401, body: '{"error": "unauthorized"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		mut url_path := req.url
		qi := url_path.index('?') or { url_path.len }
		url_path = url_path[..qi]
		slug := url_path[9..].all_before('/meta')
		owner := db_get_lambda_owner(slug)
		if owner < 0 {
			return http.Response{ status_code: 404, body: '{"error": "lambda not found"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		if owner != auth_uid {
			return http.Response{ status_code: 403, body: '{"error": "forbidden"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		if req.method != .get && req.method != .put {
			return http.Response{ status_code: 405, body: '{"error": "method not allowed"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		request_body := req.data
		if req.method == .put && request_body.len > vlambda_max_body_size {
			return http.Response{ status_code: 413, body: '{"error": "request body too large"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		mut body := call_py_strs(vlambda_meta, [slug, auth_uid.str(), req.method.str(), request_body])
		if body == '' {
			body = '{"error": "meta failed"}'
		}
		status := if body.starts_with('{"summary"') { 200 } else { 400 }
		return http.Response{ status_code: status, body: body, header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
	}

	// Route: /vlambda/<slug>/export (GET = download lambda as JSON)
	if req.url.starts_with('/vlambda/') && req.url.contains('/export') {
		if auth_uid < 0 {
			return http.Response{ status_code: 401, body: '{"error": "unauthorized"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		if req.method != .get {
			return http.Response{ status_code: 405, body: '{"error": "method not allowed"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		mut url_path := req.url
		qi := url_path.index('?') or { url_path.len }
		url_path = url_path[..qi]
		slug := url_path[9..].all_before('/export')
		if slug == '' {
			return http.Response{ status_code: 400, body: '{"error": "missing slug"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		body := call_py_two_str(vlambda_export_lambda, slug, auth_uid.str())
		if body == '' {
			return http.Response{ status_code: 404, body: '{"error": "lambda not found"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		if body.starts_with('{"error"') {
			return http.Response{ status_code: 500, body: body, header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		return http.Response{ status_code: 200, body: body, header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
	}

	// Route: /vlambda/import (POST = create from exported JSON)
	if req.url == '/vlambda/import' && req.method == .post {
		if auth_uid < 0 {
			return http.Response{ status_code: 401, body: '{"error": "unauthorized"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		if req.data.len == 0 {
			return http.Response{ status_code: 400, body: '{"error": "missing json body"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		if req.data.len > vlambda_max_body_size * 100 {
			return http.Response{ status_code: 413, body: '{"error": "import too large"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		body := call_py_two_str(vlambda_import_lambda, req.data, auth_uid.str())
		if body == '' {
			return http.Response{ status_code: 500, body: '{"error": "import failed"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		status := if body.starts_with('{"error"') { 400 } else { 201 }
		return http.Response{ status_code: status, body: body, header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
	}

	// Route: /vlambda/<slug>/duplicate (POST = copy lambda)
	if req.url.starts_with('/vlambda/') && req.url.contains('/duplicate') {
		if auth_uid < 0 {
			return http.Response{ status_code: 401, body: '{"error": "unauthorized"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		if req.method != .post {
			return http.Response{ status_code: 405, body: '{"error": "method not allowed"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		mut url_path := req.url
		qi := url_path.index('?') or { url_path.len }
		url_path = url_path[..qi]
		slug := url_path[9..].all_before('/duplicate')
		if slug == '' {
			return http.Response{ status_code: 400, body: '{"error": "missing slug"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		body := call_py_two_str(vlambda_duplicate_lambda, slug, auth_uid.str())
		if body == '' {
			return http.Response{ status_code: 404, body: '{"error": "lambda not found"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		if body.starts_with('{"error"') {
			return http.Response{ status_code: 400, body: body, header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		return http.Response{ status_code: 201, body: body, header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
	}

	// Route: /vlambda/<slug>/packages (GET list, POST install)
	if req.url.starts_with('/vlambda/') && req.url.contains('/packages') {
		if auth_uid < 0 {
			return http.Response{ status_code: 401, body: '{"error": "unauthorized"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		mut url_path := req.url
		qi := url_path.index('?') or { url_path.len }
		url_path = url_path[..qi]
		slug := url_path[9..].all_before('/packages')
		owner := db_get_lambda_owner(slug)
		if owner < 0 {
			return http.Response{ status_code: 404, body: '{"error": "lambda not found"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		if owner != auth_uid {
			return http.Response{ status_code: 403, body: '{"error": "forbidden"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		project_info := db_get_project(slug)
		kind := if project_info != '' { 'project' } else { 'code' }
		project_dir := if project_info != '' { project_info.all_before('|') } else { '' }
		if req.method == .get {
			mut body := call_py_strs(vlambda_list_packages, [slug, kind, project_dir])
			if body == '' {
				body = '{"packages": []}'
			}
			return http.Response{ status_code: 200, body: body, header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		if req.method == .post {
			request_body := req.data
			mut body := call_py_strs(vlambda_install_packages, [slug, kind, project_dir, request_body])
			if body == '' {
				body = '{"error": "install failed"}'
			}
			return http.Response{ status_code: 200, body: body, header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		return http.Response{ status_code: 405, body: '{"error": "method not allowed"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
	}

	// Route: /vlambda/<slug>/versions
	if req.url.starts_with('/vlambda/') && req.url.contains('/versions') {
		mut url_path := req.url
		qi := url_path.index('?') or { url_path.len }
		url_path = url_path[..qi]
		slug := url_path[9..url_path.index('/versions') or { url_path.len }]
		if auth_uid < 0 {
			return http.Response{ status_code: 401, body: '{"error": "unauthorized"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		owner := db_get_lambda_owner(slug)
		if owner < 0 {
			return http.Response{ status_code: 404, body: '{"error": "lambda not found"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		if owner != auth_uid {
			return http.Response{ status_code: 403, body: '{"error": "forbidden"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		mut body := call_py_strs(vlambda_list_versions, [slug])
		if body == '' {
			body = '{"versions": []}'
		}
		return http.Response{ status_code: 200, body: body, header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
	}

	// Route: /vlambda/<slug>/aliases
	if req.url.starts_with('/vlambda/') && req.url.contains('/aliases') {
		mut url_path := req.url
		qi := url_path.index('?') or { url_path.len }
		url_path = url_path[..qi]
		slug := url_path[9..url_path.index('/aliases') or { url_path.len }]
		if auth_uid < 0 {
			return http.Response{ status_code: 401, body: '{"error": "unauthorized"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		owner := db_get_lambda_owner(slug)
		if owner < 0 {
			return http.Response{ status_code: 404, body: '{"error": "lambda not found"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		if owner != auth_uid {
			return http.Response{ status_code: 403, body: '{"error": "forbidden"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		mut body := call_py_strs(vlambda_list_aliases, [slug])
		if body == '' {
			body = '{"aliases": []}'
		}
		return http.Response{ status_code: 200, body: body, header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
	}

	// Route: /vlambda/<slug>/version/<n> (GET = read version code)
	if req.url.starts_with('/vlambda/') && req.url.contains('/version/') {
		mut url_path := req.url
		qi := url_path.index('?') or { url_path.len }
		url_path = url_path[..qi]
		slug := url_path[9..].all_before('/version/')
		ver_str := url_path.all_after('/version/')
		version := ver_str.int()
		if auth_uid < 0 {
			return http.Response{ status_code: 401, body: '{"error": "unauthorized"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		owner := db_get_lambda_owner(slug)
		if owner < 0 {
			return http.Response{ status_code: 404, body: '{"error": "lambda not found"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		if owner != auth_uid {
			return http.Response{ status_code: 403, body: '{"error": "forbidden"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		code_info := db_get_version_code(slug, version)
		if code_info == '' {
			return http.Response{ status_code: 404, body: '{"error": "version not found"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		code := code_info.all_before('|')
		return http.Response{ status_code: 200, body: code, header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'text/plain' }) }
	}

	// Route: /vlambda/<slug>/rollback?version=N (POST = restore version)
	if req.url.starts_with('/vlambda/') && req.url.contains('/rollback') {
		if req.method == .post {
			if auth_uid < 0 {
				return http.Response{ status_code: 401, body: '{"error": "unauthorized"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
			}
			mut url_path := req.url
			qi := url_path.index('?') or { url_path.len }
			url_path = url_path[..qi]
			slug := url_path[9..].all_before('/rollback')
			if auth_uid < 0 {
				return http.Response{ status_code: 401, body: '{"error": "unauthorized"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
			}
			owner := db_get_lambda_owner(slug)
			if owner < 0 {
				return http.Response{ status_code: 404, body: '{"error": "lambda not found"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
			}
			if owner != auth_uid {
				return http.Response{ status_code: 403, body: '{"error": "forbidden"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
			}
			mut version := 0
			if req.url.contains('?') {
				qs := req.url.all_after('?')
				for part in qs.split('&') {
					if part.starts_with('version=') { version = part.all_after('=').int() }
				}
			}
			code_info := db_get_version_code(slug, version)
			if code_info == '' {
				return http.Response{ status_code: 404, body: '{"error": "version not found"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
			}
			code := code_info.all_before('|')
			rest_info := code_info.all_after('|')
			method := rest_info.all_before('|')
			timeout := 0
			memory_mb := 0
			name := db_get_lambda_name(slug)
			ok := db_update_lambda(slug, name, code, method, auth_uid, timeout, memory_mb)
			if ok {
				return http.Response{ status_code: 200, body: '{"rolled_back": true, "version": ${version}}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
			}
			return http.Response{ status_code: 500, body: '{"error": "rollback failed"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
	}

	// Route: /vlambda/<id>/alias (POST = create alias)
	if req.url.starts_with('/vlambda/') && req.url.contains('/alias') {
		if req.method == .post {
			if auth_uid < 0 {
				return http.Response{ status_code: 401, body: '{"error": "unauthorized"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
			}
			mut url_path := req.url
			qi := url_path.index('?') or { url_path.len }
			url_path = url_path[..qi]
			slug := url_path[9..url_path.index('/alias') or { url_path.len }]
			mut alias := ''
			mut version := 0
			if req.url.contains('?') {
				qs := req.url.all_after('?')
				for part in qs.split('&') {
					if part.starts_with('name=') || part.starts_with('alias=') {
						alias = decode_query_value(part.all_after('='))
					}
					if part.starts_with('version=') {
						version = part.all_after('=').int()
					}
				}
			}
			if alias == '' {
				return http.Response{ status_code: 400, body: '{"error": "missing alias name"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
			}
			ok := db_create_alias(alias, slug, version, auth_uid)
			if ok {
				return http.Response{ status_code: 201, body: '{"alias": "${alias}", "slug": "${slug}", "version": ${version}}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
			}
			return http.Response{ status_code: 400, body: '{"error": "failed to create alias"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
	}

	// Route: /vlambda/<id> (DELETE)
	if req.url.starts_with('/vlambda/') {
		mut url_path2 := req.url
		qi2 := url_path2.index('?') or { url_path2.len }
		url_path2 = url_path2[..qi2]
		slug := url_path2[9..]
		if req.method == .get {
			if auth_uid < 0 {
				return http.Response{
					status_code: 401
					body: '{"error": "unauthorized"}'
					header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
				}
			}
			owner := db_get_lambda_owner(slug)
			if owner >= 0 && owner != auth_uid {
				return http.Response{
					status_code: 403
					body: '{"error": "forbidden"}'
					header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
				}
			}
			code_method := db_get_lambda_code(slug)
			if code_method == '' {
				return http.Response{
					status_code: 404
					body: '{"error": "lambda not found"}'
					header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
				}
			}
			parts := code_method.split('|')
			code := if parts.len > 0 { parts[0] } else { '' }
			return http.Response{
				status_code: 200
				body: code
				header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'text/plain' })
			}
		}
		if req.method == .delete {
			owner := db_get_lambda_owner(slug)
			if owner >= 0 && owner != auth_uid {
				return http.Response{
					status_code: 403
					body: '{"error": "forbidden"}'
					header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
				}
			}
			// Project files are removed by the Python orphan GC thread,
			// which avoids calling into Python while the V GC may be running.
			ok := db_delete_lambda(slug, auth_uid)
			if ok {
				return http.Response{
					status_code: 200
					body: '{"deleted": true}'
					header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
				}
			}
			return http.Response{
				status_code: 500
				body: '{"error": "delete failed"}'
				header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
			}
		}
		if req.method == .put {
			if auth_uid < 0 {
				return http.Response{
					status_code: 401
					body: '{"error": "unauthorized"}'
					header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
				}
			}
			code := req.data
			if code.len == 0 {
				return http.Response{
					status_code: 400
					body: '{"error": "missing code in body"}'
					header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
				}
			}
			mut method := 'GET'
			mut name := db_get_lambda_name(slug)
			mut timeout := 0
			mut memory_mb := 0
			if req.url.contains('?') {
				qs := req.url.all_after('?')
				for part in qs.split('&') {
					if part.starts_with('method=') {
						method = decode_query_value(part.all_after('='))
					}
					if part.starts_with('name=') {
						decoded := decode_query_value(part.all_after('=')).trim_space()
						if decoded != '' {
							name = decoded
						}
					}
					if part.starts_with('timeout=') {
						timeout = part.all_after('=').int()
					}
					if part.starts_with('memory_mb=') {
						memory_mb = part.all_after('=').int()
					}
				}
			}
			method = normalize_method(method)
			ok := db_update_lambda(slug, name, code, method, auth_uid, timeout, memory_mb)
			if ok {
				prewarm_lambda(code)
				return http.Response{
					status_code: 200
					body: '{"updated": true}'
					header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
				}
			}
			return http.Response{
				status_code: 500
				body: '{"error": "update failed"}'
				header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
			}
		}
	}

	return http.Response{
		status_code: 404
		body: 'asset not found'
		header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'text/plain' })
	}
}

// ---- vcraft extension functions (Python bridge) ----

@[vc_fn]
pub fn set_version(version string) {
	vlambda_version = version
}

@[vc_fn]
pub fn db_init(db_path string) bool {
	vlambda_db_path = db_path
	vlambda_db_error = ''
	mut db := init_db(db_path) or {
		vlambda_db_error = err.msg()
		return false
	}
	db.close() or {}
	return true
}

@[vc_fn]
pub fn db_init_error() string {
	return vlambda_db_error
}

@[vc_fn]
pub fn register_executor(cb voidptr) {
	vlambda_executor = cb
}

@[vc_fn]
pub fn register_pool(cb voidptr) {
	vlambda_executor = cb
}

@[vc_fn]
pub fn register_auth_verifier(cb voidptr) {
	vlambda_auth_verifier = cb
}

@[vc_fn]
pub fn auth_verify_token(token string) int {
	if vlambda_auth_verifier == unsafe { nil } { return -1 }
	state := vcraft.gil_ensure()
	defer { vcraft.gil_release(state) }
	args := [vcraft.to_py_string(token).ptr]
	r := vcraft.call(vlambda_auth_verifier, args)
	vcraft.borrow(args[0]).decref()
	if r.is_null() {
		vcraft.clear_error()
		return -1
	}
	uid := int(vcraft.from_py_int(r, 'uid') or { -1 })
	r.decref()
	return uid
}

@[vc_fn]
pub fn db_create_lambda(slug string, name string, code string, method string, owner_id int, timeout int, memory_mb int) bool {
	mut db := db_open() or { return false }
	defer { db.close() or {} }
	db.exec_param_many('INSERT INTO lambdas (slug, name, code, method, owner_id, created_at, timeout, memory_mb, version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1);',
		[slug, name, code, method, owner_id.str(), '2026-01-01T00:00:00Z', timeout.str(), memory_mb.str()]) or { return false }
	db.exec_param_many('INSERT INTO lambda_versions (slug, version, code, method, entrypoint, project_dir, kind, created_at) VALUES (?, 1, ?, ?, "", "", "code", ?);',
		[slug, code, method, '2026-01-01T00:00:00Z']) or {}
	invalidate_lambda_caches(slug)
	return true
}

@[vc_fn]
pub fn db_update_lambda(slug string, name string, code string, method string, owner_id int, timeout int, memory_mb int) bool {
	mut db := db_open() or { return false }
	defer { db.close() or {} }
	// Bump version and store the previous version.
	mut version := 1
	current_rows := db.exec_param_many('SELECT version FROM lambdas WHERE slug = ? AND owner_id = ?;', [slug, owner_id.str()]) or { [] }
	if current_rows.len > 0 {
		version = (current_rows[0].vals[0] or { '1' }).int() + 1
	}
	db.exec_param_many('INSERT INTO lambda_versions (slug, version, code, method, entrypoint, project_dir, kind, created_at) SELECT slug, version, code, method, entrypoint, project_dir, kind, created_at FROM lambdas WHERE slug = ? AND owner_id = ? AND NOT EXISTS (SELECT 1 FROM lambda_versions WHERE slug = ? AND version = (SELECT version FROM lambdas WHERE slug = ? AND owner_id = ?));', [slug, owner_id.str(), slug, slug, owner_id.str()]) or {}
	db.exec_param_many('UPDATE lambdas SET name = ?, code = ?, method = ?, timeout = ?, memory_mb = ?, version = ? WHERE slug = ? AND owner_id = ?;', [name, code, method, timeout.str(), memory_mb.str(), version.str(), slug, owner_id.str()]) or { return false }
	// Invalidate cache
	invalidate_lambda_caches(slug)
	return true
}


@[vc_fn]
pub fn prewarm_lambda(code string) {
	if vlambda_executor == unsafe { nil } { return }
	state := vcraft.gil_ensure()
	mut req_items := map[string]vcraft.PyObj{}
	req_items['method'] = vcraft.to_py_string('GET')
	req_items['path'] = vcraft.to_py_string('/')
	req_items['query_string'] = vcraft.to_py_string('')
	req_items['body'] = vcraft.to_py_bytes('')
	req_obj := vcraft.to_py_obj_dict(req_items)
	args := [vcraft.to_py_string(code).ptr, req_obj.ptr]
	r := vcraft.call(vlambda_executor, args)
	if !r.is_null() { r.decref() }
	req_obj.decref()
	vcraft.borrow(args[0]).decref()
	vcraft.gil_release(state)
}

fn invalidate_lambda_caches(slug string) {
	vlambda_code_cache.delete(slug)
	vlambda_missing_cache.delete(slug)
	vlambda_project_cache.delete(slug)
	vlambda_project_missing.delete(slug)
}

@[vc_fn]
pub fn db_get_lambda_code(slug string) string {
	// Check in-memory cache first
	if cached := vlambda_code_cache[slug] {
		return cached
	}
	mut db := db_open() or { return '' }
	defer { db.close() or {} }
	if vlambda_missing_cache[slug] {
		return ''
	}
	rows := db.exec_param("SELECT code, method, timeout, memory_mb, version, kind FROM lambdas WHERE slug = ? AND kind = 'code';", slug) or { return '' }
	if rows.len == 0 {
		vlambda_missing_cache[slug] = true
		if vlambda_missing_cache.len > 10000 {
			vlambda_missing_cache = map[string]bool{}
		}
		return ''
	}
	code := rows[0].vals[0] or { return '' }
	method := rows[0].vals[1] or { return '' }
	timeout := rows[0].vals[2] or { '0' }
	memory_mb := rows[0].vals[3] or { '0' }
	version := rows[0].vals[4] or { '0' }
	kind := rows[0].vals[5] or { 'code' }
	result := code + '|' + method + '|' + timeout + '|' + memory_mb + '|' + version + '|' + kind
	vlambda_code_cache[slug] = result
	return result
}

@[vc_fn]
pub fn db_get_lambda_name(slug string) string {
	mut db := db_open() or { return '' }
	defer { db.close() or {} }
	rows := db.exec_param('SELECT name FROM lambdas WHERE slug = ?;', slug) or { return '' }
	if rows.len == 0 { return '' }
	return rows[0].vals[0] or { return '' }
}

@[vc_fn]
pub fn db_create_project(slug string, name string, method string, entrypoint string, project_dir string, owner_id int) bool {
	if db_count_lambdas(owner_id) >= max_lambdas_per_user() { return false }
	mut db := db_open() or { return false }
	defer { db.close() or {} }
	db.exec_param_many('INSERT INTO lambdas (slug, name, code, method, owner_id, created_at, kind, entrypoint, project_dir, timeout, memory_mb, version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 1);',
		[slug, name, '', method, owner_id.str(), '2026-01-01T00:00:00Z', 'project', entrypoint, project_dir]) or { return false }
	db.exec_param_many('INSERT INTO lambda_versions (slug, version, code, method, entrypoint, project_dir, kind, created_at) VALUES (?, 1, ?, ?, ?, ?, "project", ?);',
		[slug, '', method, entrypoint, project_dir, '2026-01-01T00:00:00Z']) or {}
	invalidate_lambda_caches(slug)
	return true
}

@[vc_fn]
pub fn db_get_project(slug string) string {
	if cached := vlambda_project_cache[slug] {
		return cached
	}
	if vlambda_project_missing[slug] {
		return ''
	}
	mut db := db_open() or { return '' }
	defer { db.close() or {} }
	rows := db.exec_param('SELECT project_dir, entrypoint, method FROM lambdas WHERE slug = ? AND kind = "project";', slug) or { return '' }
	if rows.len == 0 {
		vlambda_project_missing[slug] = true
		if vlambda_project_missing.len > 10000 {
			vlambda_project_missing = map[string]bool{}
		}
		return ''
	}
	project_dir := rows[0].vals[0] or { return '' }
	entrypoint := rows[0].vals[1] or { return '' }
	method := rows[0].vals[2] or { return '' }
	result := project_dir + '|' + entrypoint + '|' + method
	vlambda_project_cache[slug] = result
	return result
}

@[vc_fn]
pub fn db_get_lambda_owner(slug string) int {
	mut db := db_open() or { return -1 }
	defer { db.close() or {} }
	rows := db.exec_param('SELECT owner_id FROM lambdas WHERE slug = ?;', slug) or { return -1 }
	if rows.len == 0 { return -1 }
	n := rows[0].vals[0] or { return -1 }
	return n.int()
}

@[vc_fn]
pub fn db_create_alias(alias string, slug string, version int, owner_id int) bool {
	mut db := db_open() or { return false }
	defer { db.close() or {} }
	// Only the owner may create an alias for a lambda.
	owner := db.exec_param('SELECT owner_id FROM lambdas WHERE slug = ?;', slug) or { return false }
	if owner.len == 0 { return false }
	o := owner[0].vals[0] or { return false }
	if o.int() != owner_id { return false }
	ver := if version > 0 { version } else {
		r := db.exec_param('SELECT version FROM lambdas WHERE slug = ?;', slug) or { return false }
		if r.len == 0 { return false }
		(r[0].vals[0] or { '1' }).int()
	}
	db.exec_param_many('INSERT OR REPLACE INTO lambda_aliases (alias, slug, version, owner_id) VALUES (?, ?, ?, ?);',
		[slug + ':' + alias, slug, ver.str(), owner_id.str()]) or { return false }
	return true
}

@[vc_fn]
pub fn db_resolve_alias(alias string) string {
	mut db := db_open() or { return '' }
	defer { db.close() or {} }
	rows := db.exec_param('SELECT slug, version FROM lambda_aliases WHERE alias = ?;', alias) or { return '' }
	if rows.len == 0 { return '' }
	slug := rows[0].vals[0] or { return '' }
	version := rows[0].vals[1] or { return '' }
	return slug + '|' + version
}

@[vc_fn]
pub fn db_get_version_code(slug string, version int) string {
	mut db := db_open() or { return '' }
	defer { db.close() or {} }
	rows := db.exec_param_many('SELECT code, method, kind, version FROM lambda_versions WHERE slug = ? AND version = ?;', [slug, version.str()]) or { return '' }
	if rows.len == 0 { return '' }
	code := rows[0].vals[0] or { return '' }
	method := rows[0].vals[1] or { return '' }
	kind := rows[0].vals[2] or { 'code' }
	version_str := rows[0].vals[3] or { version.str() }
	return code + '|' + method + '|0|0|' + version_str + '|' + kind
}

@[vc_fn]
pub fn db_delete_lambda(slug string, owner_id int) bool {
	mut db := db_open() or { return false }
	defer { db.close() or {} }
	rows := db.exec_param_many('SELECT owner_id FROM lambdas WHERE slug = ?;', [slug]) or { return false }
	if rows.len == 0 { return false }
	owner := rows[0].vals[0] or { return false }
	if owner.int() != owner_id { return false }
	db.exec_param_many('DELETE FROM lambdas WHERE slug = ?;', [slug]) or { return false }
	// Remove per-lambda state so a future lambda cannot inherit it.
	db.exec_param_many('DELETE FROM lambda_env WHERE slug = ?;', [slug]) or {}
	db.exec_param_many('DELETE FROM lambda_kv WHERE slug = ?;', [slug]) or {}
	db.exec_param_many('DELETE FROM lambda_egress WHERE slug = ?;', [slug]) or {}
	db.exec_param_many('DELETE FROM schedule_runs WHERE slug = ?;', [slug]) or {}
	db.exec_param_many('DELETE FROM schedules WHERE slug = ?;', [slug]) or {}
	db.exec_param_many('DELETE FROM tasks WHERE slug = ?;', [slug]) or {}
	db.exec_param_many('DELETE FROM lambda_versions WHERE slug = ?;', [slug]) or {}
	db.exec_param_many('DELETE FROM lambda_aliases WHERE slug = ?;', [slug]) or {}
	invalidate_lambda_caches(slug)
	return true
}

@[vc_fn]
pub fn db_list_lambdas(owner_id int) string {
	mut db := db_open() or { return '' }
	defer { db.close() or {} }
	rows := db.exec_param('SELECT slug, name, method FROM lambdas WHERE owner_id = ?;', owner_id.str()) or { return '' }
	mut out := ''
	for row in rows {
		slug := row.vals[0] or { continue }
		name := row.vals[1] or { continue }
		method := row.vals[2] or { continue }
		out += slug + '|' + name + '|' + method + '\\n'
	}
	return out
}

@[vc_fn]
pub fn db_count_lambdas(owner_id int) int {
	mut db := db_open() or { return 0 }
	defer { db.close() or {} }
	rows := db.exec_param('SELECT COUNT(*) FROM lambdas WHERE owner_id = ?;', owner_id.str()) or { return 0 }
	if rows.len == 0 { return 0 }
	n := rows[0].vals[0] or { return 0 }
	return n.int()
}

@[vc_fn]
pub fn db_list_lambdas_page(owner_id int, limit int, offset int) string {
	mut db := db_open() or { return '' }
	defer { db.close() or {} }
	mut lim := limit
	mut off := offset
	if lim <= 0 { lim = 50 }
	if off < 0 { off = 0 }
	query := 'SELECT slug, name, method, kind, entrypoint FROM lambdas WHERE owner_id = ? LIMIT ${lim} OFFSET ${off};'

	rows := db.exec_param(query, owner_id.str()) or { return '' }
	mut out := ''
	for row in rows {
		slug := row.vals[0] or { continue }
		name := row.vals[1] or { continue }
		method := row.vals[2] or { continue }
		kind := row.vals[3] or { 'code' }
		entrypoint := row.vals[4] or { '' }
		out += slug + '|' + name + '|' + method + '|' + kind + '|' + entrypoint + '\\n'
	}
	return out
}

@[vc_fn]
pub fn db_list_lambdas_json(owner_id int, limit int, offset int) string {
	mut db := db_open() or { return '[]' }
	defer { db.close() or {} }
	mut lim := limit
	mut off := offset
	if lim <= 0 { lim = 50 }
	if off < 0 { off = 0 }
	query := 'SELECT slug, name, method, kind, entrypoint FROM lambdas WHERE owner_id = ? LIMIT ${lim} OFFSET ${off};'
	rows := db.exec_param(query, owner_id.str()) or { return '[]' }
	mut items := []string{}
	for row in rows {
		slug := row.vals[0] or { continue }
		name := row.vals[1] or { '' }
		method := row.vals[2] or { 'GET' }
		kind := row.vals[3] or { 'code' }
		entrypoint := row.vals[4] or { '' }
		items << '{"slug": ${json_str(slug)}, "name": ${json_str(name)}, "method": ${json_str(method)}, "kind": ${json_str(kind)}, "entrypoint": ${json_str(entrypoint)}}'
	}
	return '[' + items.join(',') + ']'
}

// ---- API token storage (long-lived personal access tokens) ----

@[vc_fn]
pub fn db_create_api_token(user_id int, name string, scopes string) string {
	mut db := db_open() or { return '{"error": "db unavailable"}' }
	defer { db.close() or {} }
	raw := 'vl_' + rand.string(40)
	hash := sha256.sum256(raw.bytes()).hex()
	created := time.now().unix().str()
	db.exec_param_many('INSERT INTO api_tokens (user_id, name, token_hash, scopes, created_at) VALUES (?, ?, ?, ?, ?);',
		[user_id.str(), name, hash, scopes, created]) or {
		return '{"error": "failed to store token"}'
	}
	rows := db.exec_param('SELECT id FROM api_tokens WHERE token_hash = ?;', hash) or { [] }
	id := if rows.len > 0 { rows[0].vals[0] or { '0' } } else { '0' }
	return '{"id": ${id}, "token": ${json_str(raw)}, "name": ${json_str(name)}, "scopes": ${json_str(scopes)}}'
}

@[vc_fn]
pub fn db_list_api_tokens(user_id int) string {
	mut db := db_open() or { return '[]' }
	defer { db.close() or {} }
	rows := db.exec_param('SELECT id, name, scopes, created_at, last_used_at, revoked FROM api_tokens WHERE user_id = ? ORDER BY id DESC;',
		user_id.str()) or { return '[]' }
	mut items := []string{}
	for row in rows {
		id := row.vals[0] or { '0' }
		name := row.vals[1] or { '' }
		scopes := row.vals[2] or { '' }
		created := row.vals[3] or { '' }
		last := row.vals[4] or { '' }
		revoked := row.vals[5] or { '0' }
		items << '{"id": ${id}, "name": ${json_str(name)}, "scopes": ${json_str(scopes)}, "created_at": ${json_str(created)}, "last_used_at": ${json_str(last)}, "revoked": ${revoked == '1'}}'
	}
	return '[' + items.join(',') + ']'
}

@[vc_fn]
pub fn db_revoke_api_token(user_id int, token_id int) bool {
	mut db := db_open() or { return false }
	defer { db.close() or {} }
	db.exec_param_many('UPDATE api_tokens SET revoked = 1 WHERE id = ? AND user_id = ?;',
		[token_id.str(), user_id.str()]) or { return false }
	return true
}

@[vc_fn]
pub fn db_verify_api_token(raw string) string {
	hash := sha256.sum256(raw.bytes()).hex()
	mut db := db_open() or { return '' }
	defer { db.close() or {} }
	rows := db.exec_param('SELECT t.user_id, t.scopes FROM api_tokens t JOIN users u ON u.id = t.user_id WHERE t.token_hash = ? AND t.revoked = 0;',
		hash) or { return '' }
	if rows.len == 0 {
		return ''
	}
	uid := rows[0].vals[0] or { return '' }
	scopes := rows[0].vals[1] or { 'read,write' }
	db.exec_param_many('UPDATE api_tokens SET last_used_at = ? WHERE token_hash = ?;',
		[time.now().unix().str(), hash]) or {}
	return uid + '|' + scopes
}

// run_lambda calls Python to exe@[vc_fn]
fn body_to_py_bytes(body string) vcraft.PyObj {
	if body.len == 0 {
		return vcraft.to_py_bytes('')
	}
	unsafe {
		return vcraft.to_py_bytes_fast(&u8(body.str), body.len)
	}
}

fn lambda_content_type(result string) string {
	trimmed := result.trim_space().to_lower()
	if trimmed.starts_with('<!doctype html') || trimmed.starts_with('<html') {
		return 'text/html; charset=utf-8'
	}
	return 'application/json'
}

pub fn run_lambda(request_id string, slug string, code string, method string, path string, query string, body string, timeout int, memory_mb int, request_meta string, code_key string) string {
	// The result must live on this invocation's stack: V handles requests on
	// several threads, so a package-global result string races under load.
	mut result_str := error_envelope('not started')
	start_time := time.now()
	state := vcraft.gil_ensure()
	defer { vcraft.gil_release(state) }
		if vlambda_executor == unsafe { nil } {
			err := error_envelope('no executor registered')
			result_str = err
			if vlambda_metrics_enabled && vlambda_record_metric != unsafe { nil } && (vlambda_metrics_sample <= 1 || int(vlambda_metric_counter) % vlambda_metrics_sample == 0) {
				elapsed := time.since(start_time).microseconds()
				args := [vcraft.to_py_string(request_id).ptr, vcraft.to_py_string(slug).ptr, vcraft.to_py_string(method).ptr, vcraft.to_py_string(path).ptr, vcraft.to_py_string('${f64(elapsed) / 1000.0}').ptr, vcraft.to_py_string(err).ptr]
				r := vcraft.call(vlambda_record_metric, args)
				for a in args { vcraft.borrow(a).decref() }
				if !r.is_null() { r.decref() }
			}
			return result_str
		}
		args := [vcraft.to_py_string(request_id).ptr, vcraft.to_py_string(slug).ptr, vcraft.to_py_string(code).ptr, vcraft.to_py_string(method).ptr, vcraft.to_py_string(path).ptr, vcraft.to_py_string(query).ptr, body_to_py_bytes(body).ptr, vcraft.to_py_int(timeout).ptr, vcraft.to_py_int(memory_mb).ptr, vcraft.to_py_string(request_meta).ptr, vcraft.to_py_string(code_key).ptr]
		result := vcraft.call(vlambda_executor, args)
		for a in args { vcraft.borrow(a).decref() }
		if result.is_null() {
			vcraft.clear_error()
			err := error_envelope('execution failed')
			result_str = err
			if vlambda_metrics_enabled && vlambda_record_metric != unsafe { nil } && (vlambda_metrics_sample <= 1 || int(vlambda_metric_counter) % vlambda_metrics_sample == 0) {
				elapsed := time.since(start_time).microseconds()
				margs := [vcraft.to_py_string(request_id).ptr, vcraft.to_py_string(slug).ptr, vcraft.to_py_string(method).ptr, vcraft.to_py_string(path).ptr, vcraft.to_py_string('${f64(elapsed) / 1000.0}').ptr, vcraft.to_py_string(err).ptr]
				r := vcraft.call(vlambda_record_metric, margs)
				for a in margs { vcraft.borrow(a).decref() }
				if !r.is_null() { r.decref() }
			}
			return result_str
		}
		if result.type_name() == 'str' {
			ss := py_string_copy(result, 'result', '{"error": "bad result"}')
			result.decref()
			result_str = ss
			if vlambda_metrics_enabled && vlambda_record_metric != unsafe { nil } && (vlambda_metrics_sample <= 1 || int(vlambda_metric_counter) % vlambda_metrics_sample == 0) {
				elapsed := time.since(start_time).microseconds()
				margs := [vcraft.to_py_string(request_id).ptr, vcraft.to_py_string(slug).ptr, vcraft.to_py_string(method).ptr, vcraft.to_py_string(path).ptr, vcraft.to_py_string('${f64(elapsed) / 1000.0}').ptr, vcraft.to_py_string(if ss.contains('error') { ss } else { '' }).ptr]
				r := vcraft.call(vlambda_record_metric, margs)
				for a in margs { vcraft.borrow(a).decref() }
				if !r.is_null() { r.decref() }
			}
			return result_str
		}
		if result.type_name() == 'bytes' {
			ss := py_bytes_copy(result, 'result', '{"error": "bad result"}')
			result.decref()
			result_str = ss
			if vlambda_metrics_enabled && vlambda_record_metric != unsafe { nil } && (vlambda_metrics_sample <= 1 || int(vlambda_metric_counter) % vlambda_metrics_sample == 0) {
				elapsed := time.since(start_time).microseconds()
				margs := [vcraft.to_py_string(request_id).ptr, vcraft.to_py_string(slug).ptr, vcraft.to_py_string(method).ptr, vcraft.to_py_string(path).ptr, vcraft.to_py_string('${f64(elapsed) / 1000.0}').ptr, vcraft.to_py_string(if ss.contains('error') { ss } else { '' }).ptr]
				r := vcraft.call(vlambda_record_metric, margs)
				for a in margs { vcraft.borrow(a).decref() }
				if !r.is_null() { r.decref() }
			}
			return result_str
		}
		ss := result.str()
		result.decref()
		result_str = ss
		if vlambda_metrics_enabled && vlambda_record_metric != unsafe { nil } && (vlambda_metrics_sample <= 1 || int(vlambda_metric_counter) % vlambda_metrics_sample == 0) {
			elapsed := time.since(start_time).microseconds()
			margs := [vcraft.to_py_string(slug).ptr, vcraft.to_py_string(method).ptr, vcraft.to_py_string(path).ptr, vcraft.to_py_string('${f64(elapsed) / 1000.0}').ptr, vcraft.to_py_string(if ss.contains('error') { ss } else { '' }).ptr]
			r := vcraft.call(vlambda_record_metric, margs)
			for a in margs { vcraft.borrow(a).decref() }
			if !r.is_null() { r.decref() }
		}
	return result_str
}

@[vc_fn]
pub fn run_project(project_dir string, entrypoint string, method string, path string, query string, body string, request_id string, slug string, request_meta string) string {
	mut result_str := error_envelope('not started')
	state := vcraft.gil_ensure()
	defer { vcraft.gil_release(state) }
		if vlambda_run_project == unsafe { nil } {
			result_str = error_envelope('no project runner registered')
			return result_str
		}
		args := [vcraft.to_py_string(project_dir).ptr, vcraft.to_py_string(entrypoint).ptr, vcraft.to_py_string(method).ptr, vcraft.to_py_string(path).ptr, vcraft.to_py_string(query).ptr, body_to_py_bytes(body).ptr, vcraft.to_py_string(request_id).ptr, vcraft.to_py_string(slug).ptr, vcraft.to_py_string(request_meta).ptr]
		result := vcraft.call(vlambda_run_project, args)
		for a in args { vcraft.borrow(a).decref() }
		if result.is_null() {
			vcraft.clear_error()
			result_str = error_envelope('execution failed')
			return result_str
		}
		if result.type_name() == 'str' {
			ss := py_string_copy(result, 'result', '{"error": "bad result"}')
			result.decref()
			result_str = ss
			return result_str
		}
		if result.type_name() == 'bytes' {
			ss := py_bytes_copy(result, 'result', '{"error": "bad result"}')
			result.decref()
			result_str = ss
			return result_str
		}
		ss := result.str()
		result.decref()
		result_str = ss
	return result_str
}

@[vc_fn]
pub fn register_auth_register(cb voidptr) {
	vlambda_auth_register = cb
}

@[vc_fn]
pub fn register_auth_login(cb voidptr) {
	vlambda_auth_login = cb
}

@[vc_fn]
pub fn auth_register_user(username string, password string, remote_ip string) int {
	return call_py_int_strs(vlambda_auth_register, [username, password, remote_ip])
}

@[vc_fn]
pub fn auth_login_user(username string, password string, remote_ip string) string {
	return call_py_strs(vlambda_auth_login, [username, password, remote_ip])
}

@[vc_fn]
pub fn configure(max_body_size int, rate_limit int, invoke_timeout int, max_memory_mb int, cors_origin string, cert_path string, key_path string, metrics_enabled bool, metrics_sample int, registration_enabled bool) {
	if max_body_size > 0 { vlambda_max_body_size = max_body_size }
	if rate_limit > 0 { vlambda_rate_limit = rate_limit }
	if invoke_timeout > 0 { vlambda_invoke_timeout = invoke_timeout }
	vlambda_max_memory_mb = max_memory_mb
	vlambda_metrics_enabled = metrics_enabled
	vlambda_registration_enabled = registration_enabled
	if metrics_sample > 0 { vlambda_metrics_sample = metrics_sample }
	if cors_origin != '' { vlambda_cors_origin = cors_origin }
	vlambda_cert_path = cert_path
	vlambda_key_path = key_path
}

@[vc_fn]
pub fn get_favicon() string {
	if vlambda_favicon == unsafe { nil } { return '' }
	body := call_py_bytes_strs(vlambda_favicon, [])
	if body == '' {
		return 'NULL_RESULT'
	}
	return body
}

@[vc_fn]
pub fn register_create_task(cb voidptr) {
	vlambda_create_task = cb
}

@[vc_fn]
pub fn register_get_task(cb voidptr) {
	vlambda_get_task = cb
}

@[vc_fn]
pub fn register_get_dlq(cb voidptr) {
	vlambda_get_dlq = cb
}

@[vc_fn]
pub fn register_record_metric(cb voidptr) {
	vlambda_record_metric = cb
}

@[vc_fn]
pub fn register_query_metrics(cb voidptr) {
	vlambda_query_metrics = cb
}

@[vc_fn]
pub fn register_query_summary(cb voidptr) {
	vlambda_query_summary = cb
}

@[vc_fn]
pub fn register_query_aggregate(cb voidptr) {
	vlambda_query_aggregate = cb
}

@[vc_fn]
pub fn register_query_logs(cb voidptr) {
	vlambda_query_logs = cb
}

@[vc_fn]
pub fn register_delete_metrics(cb voidptr) {
	vlambda_delete_metrics = cb
}

@[vc_fn]
pub fn register_delete_logs(cb voidptr) {
	vlambda_delete_logs = cb
}

@[vc_fn]
pub fn register_export_metrics(cb voidptr) {
	vlambda_export_metrics = cb
}

@[vc_fn]
pub fn register_pool_stats(cb voidptr) {
	vlambda_pool_stats = cb
}

@[vc_fn]
pub fn register_query_stats(cb voidptr) {
	vlambda_query_stats = cb
}
@[vc_fn]
pub fn set_overloaded(flag bool) {
	vlambda_overloaded = flag
}


@[vc_fn]
pub fn register_create_project(cb voidptr) {
	vlambda_create_project = cb
}

@[vc_fn]
pub fn register_run_project(cb voidptr) {
	vlambda_run_project = cb
}

@[vc_fn]
pub fn register_delete_project(cb voidptr) {
	vlambda_delete_project = cb
}

@[vc_fn]
pub fn register_schedule_delete(cb voidptr) {
	vlambda_schedule_delete = cb
}

@[vc_fn]
pub fn register_export_lambda(cb voidptr) {
	vlambda_export_lambda = cb
}

@[vc_fn]
pub fn register_import_lambda(cb voidptr) {
	vlambda_import_lambda = cb
}

@[vc_fn]
pub fn register_duplicate_lambda(cb voidptr) {
	vlambda_duplicate_lambda = cb
}

// call_py_two_str invokes a Python callback with two string arguments and
// returns its string result. Used by export/import/duplicate.
fn call_py_strs(cb voidptr, values []string) string {
	if cb == unsafe { nil } {
		return ''
	}
	state := vcraft.gil_ensure()
	defer { vcraft.gil_release(state) }
	mut args := []voidptr{cap: values.len}
	for value in values {
		args << vcraft.to_py_string(value).ptr
	}
	r := vcraft.call(cb, args)
	for arg in args {
		vcraft.borrow(arg).decref()
	}
	if r.is_null() {
		vcraft.clear_error()
		return ''
	}
	if r.type_name() == 'str' {
		ss := py_string_copy(r, 'result', '')
		r.decref()
		return ss
	}
	r.decref()
	return ''
}

fn call_py_bytes_strs(cb voidptr, values []string) string {
	if cb == unsafe { nil } {
		return ''
	}
	state := vcraft.gil_ensure()
	defer { vcraft.gil_release(state) }
	mut args := []voidptr{cap: values.len}
	for value in values {
		args << vcraft.to_py_string(value).ptr
	}
	r := vcraft.call(cb, args)
	for arg in args {
		vcraft.borrow(arg).decref()
	}
	if r.is_null() {
		vcraft.clear_error()
		return ''
	}
	if r.type_name() == 'bytes' {
		ss := py_bytes_copy(r, 'result', '')
		r.decref()
		return ss
	}
	if r.type_name() == 'str' {
		ss := py_string_copy(r, 'result', '')
		r.decref()
		return ss
	}
	r.decref()
	return ''
}

fn call_py_int_strs(cb voidptr, values []string) int {
	if cb == unsafe { nil } {
		return -1
	}
	state := vcraft.gil_ensure()
	defer { vcraft.gil_release(state) }
	mut args := []voidptr{cap: values.len}
	for value in values {
		args << vcraft.to_py_string(value).ptr
	}
	r := vcraft.call(cb, args)
	for arg in args {
		vcraft.borrow(arg).decref()
	}
	if r.is_null() {
		vcraft.clear_error()
		return -1
	}
	result := int(vcraft.from_py_int(r, 'result') or { -1 })
	r.decref()
	return result
}

fn call_py_two_str(cb voidptr, first string, second string) string {
	return call_py_strs(cb, [first, second])
}

@[vc_fn]
pub fn register_favicon(cb voidptr) {
	vlambda_favicon = cb
}

@[vc_fn]
pub fn register_get_asset(cb voidptr) {
	vlambda_get_asset = cb
}

@[vc_fn]
pub fn register_admin_reset(cb voidptr) {
	vlambda_admin_reset = cb
}

@[vc_fn]
pub fn register_admin_create_user(cb voidptr) {
	vlambda_admin_create_user = cb
}

@[vc_fn]
pub fn register_list_packages(cb voidptr) { vlambda_list_packages = cb }

@[vc_fn]
pub fn register_list_env(cb voidptr) { vlambda_list_env = cb }

@[vc_fn]
pub fn register_save_env(cb voidptr) { vlambda_save_env = cb }

@[vc_fn]
pub fn register_kv(cb voidptr) { vlambda_kv = cb }

@[vc_fn]
pub fn register_egress(cb voidptr) { vlambda_egress = cb }

@[vc_fn]
pub fn register_schedules(cb voidptr) { vlambda_schedules = cb }

@[vc_fn]
pub fn register_meta(cb voidptr) { vlambda_meta = cb }

@[vc_fn]
pub fn register_openapi(cb voidptr) { vlambda_openapi = cb }

@[vc_fn]
pub fn register_shutdown(cb voidptr) { vlambda_shutdown = cb }

@[vc_fn]
pub fn register_install_packages(cb voidptr) { vlambda_install_packages = cb }


@[vc_fn]
pub fn register_get_project_json(cb voidptr) { vlambda_get_project_json = cb }

@[vc_fn]
pub fn register_save_project_json(cb voidptr) { vlambda_save_project_json = cb }

@[vc_fn]
pub fn register_list_versions(cb voidptr) { vlambda_list_versions = cb }

@[vc_fn]
pub fn register_list_aliases(cb voidptr) { vlambda_list_aliases = cb }


@[vc_fn]
pub fn register_ui_html(cb voidptr) {
	vlambda_ui_html = cb
}

@[vc_fn]
pub fn create_task_async(code string, method string, path string, query string, body string, owner_id int, slug string, request_meta string) string {
	if vlambda_create_task == unsafe { nil } { return '{"error": "not configured"}' }
	mut result := ''
	state := vcraft.gil_ensure()
	args := [vcraft.to_py_string(code).ptr, vcraft.to_py_string(method).ptr, vcraft.to_py_string(path).ptr, vcraft.to_py_string(query).ptr, body_to_py_bytes(body).ptr, vcraft.to_py_string(owner_id.str()).ptr, vcraft.to_py_string(slug).ptr, vcraft.to_py_string(request_meta).ptr]
	r := vcraft.call(vlambda_create_task, args)
	for a in args { vcraft.borrow(a).decref() }
	if r.is_null() {
		vcraft.clear_error()
	} else if r.type_name() == 'str' {
		result = py_string_copy(r, 'result', '')
	} else {
		result = 'TYPE_' + r.type_name()
	}
	if !r.is_null() { r.decref() }
	vcraft.gil_release(state)
	return result
}

@[vc_fn]
pub fn get_task_status(task_id string, owner_id int) string {
	if vlambda_get_task == unsafe { nil } { return '{"error": "not configured"}' }
	return call_py_strs(vlambda_get_task, [task_id, owner_id.str()])
}

@[vc_fn]
pub fn get_asset(name string) string {
	if vlambda_get_asset == unsafe { nil } { return 'NIL_CALLBACK' }
	body := call_py_bytes_strs(vlambda_get_asset, [name])
	if body == '' {
		return 'FROM_BYTES_ERR'
	}
	return body
}

@[vc_fn]
pub fn clear_caches() {
	vlambda_code_cache = map[string]string{}
	vlambda_missing_cache = map[string]bool{}
	vlambda_project_cache = map[string]string{}
	vlambda_project_missing = map[string]bool{}
}

@[vc_fn]
pub fn get_ui_html() string {
	mut body := call_py_strs(vlambda_ui_html, [])
	if body == '' {
		body = '<h1>vlambda</h1>'
	}
	return body
}

@[vc_fn]
pub fn register_validate_code(cb voidptr) {
	vlambda_validate_code = cb
}

@[vc_fn]
pub fn validate_code(code string) string {
	return call_py_strs(vlambda_validate_code, [code])
}

@[vc_fn]
pub fn start_server(port int, db_path string, num_coroutines int) int {
	vlambda_db_path = db_path
	mut db := init_db(db_path) or {
		println('Failed to init DB: ${err}')
		return 1
	}
	db.close() or {}
	println('vlambda server starting on port ${port}')
	println('DB: ${db_path}, coroutines: ${num_coroutines}')

	// Release the GIL so V's HTTP handler threads can acquire it to run Python.
	state := vcraft.allow_threads()
	raw_gc_every := os.getenv('VLAMBDA_GC_EVERY')
	if raw_gc_every != '' {
		vlambda_gc_every = raw_gc_every.int()
	}

	mut server := http.Server{
		addr: ':${port}'
		handler: VlambdaHandler{ db_path: db_path }
		max_keep_alive_requests: 10000
		read_timeout: 30 * time.second
		write_timeout: 30 * time.second
	}
	if vlambda_cert_path != '' && vlambda_key_path != '' {
		server.cert = vlambda_cert_path
		server.cert_key = vlambda_key_path
	}
	server.listen_and_serve()

	vcraft.end_allow_threads(state)
	return 0
}

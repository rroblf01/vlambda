module vlambda_native

import vcraft
import db.sqlite
import net.http
import time
import os
import strings

// ---- Global state ----

__global (
	vlambda_db_path = './vlambda.db'
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
	vlambda_export_metrics = voidptr(0)
	vlambda_pool_stats = voidptr(0)
	vlambda_query_stats = voidptr(0)
	vlambda_run_project = voidptr(0)
	vlambda_create_project = voidptr(0)
	vlambda_delete_project = voidptr(0)
	vlambda_schedule_delete = voidptr(0)
	vlambda_result_str = '{"error": "not started"}'
	// Config
	vlambda_max_body_size = 1048576  // 1MB
	vlambda_rate_limit = 100          // req/s per IP
	vlambda_invoke_timeout = 30       // seconds
	vlambda_max_memory_mb = 0         // 0 = unlimited
	vlambda_metrics_enabled = true
	vlambda_metrics_sample = 1
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
	// Shutdown
	vlambda_shutdown = false
	vlambda_overloaded = false
	vlambda_resetting = false
	vlambda_admin_reset = voidptr(0)
	vlambda_install_packages = voidptr(0)
	vlambda_list_packages = voidptr(0)
	// Rate limiter (token bucket per IP)
	vlambda_rate_buckets = map[string]TokenBucket{}
)

// ---- SQLite helpers ----

fn init_db(db_path string) !sqlite.DB {
	mut db := sqlite.connect(db_path)!
	db.exec('PRAGMA journal_mode=WAL;')!
	db.exec('PRAGMA busy_timeout=5000;')!
	db.exec('CREATE TABLE IF NOT EXISTS users (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		username TEXT UNIQUE NOT NULL,
		password_hash TEXT NOT NULL
	);')!
	db.exec('CREATE TABLE IF NOT EXISTS lambdas (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		slug TEXT UNIQUE NOT NULL,
		name TEXT NOT NULL DEFAULT "",
		code TEXT NOT NULL,
		method TEXT NOT NULL DEFAULT "GET",
		owner_id INTEGER NOT NULL DEFAULT 0,
		created_at TEXT NOT NULL
	);')!
	db.exec('ALTER TABLE lambdas ADD COLUMN name TEXT NOT NULL DEFAULT "";') or {}
	db.exec('ALTER TABLE lambdas ADD COLUMN kind TEXT NOT NULL DEFAULT "code";') or {}
	db.exec('ALTER TABLE lambdas ADD COLUMN entrypoint TEXT NOT NULL DEFAULT "";') or {}
	db.exec('ALTER TABLE lambdas ADD COLUMN project_dir TEXT NOT NULL DEFAULT "";') or {}
	db.exec('ALTER TABLE lambdas ADD COLUMN timeout INTEGER NOT NULL DEFAULT 0;') or {}
	db.exec('ALTER TABLE lambdas ADD COLUMN memory_mb INTEGER NOT NULL DEFAULT 0;') or {}
	db.exec('ALTER TABLE lambdas ADD COLUMN version INTEGER NOT NULL DEFAULT 1;') or {}
	db.exec('CREATE TABLE IF NOT EXISTS lambda_versions (id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT NOT NULL, version INTEGER NOT NULL, code TEXT NOT NULL, method TEXT NOT NULL DEFAULT "GET", entrypoint TEXT NOT NULL DEFAULT "", project_dir TEXT NOT NULL DEFAULT "", kind TEXT NOT NULL DEFAULT "code", created_at TEXT NOT NULL);')!
	db.exec('CREATE TABLE IF NOT EXISTS lambda_aliases (alias TEXT PRIMARY KEY, slug TEXT NOT NULL, version INTEGER NOT NULL, owner_id INTEGER NOT NULL);')!
	db.exec('ALTER TABLE lambdas ADD COLUMN kind TEXT NOT NULL DEFAULT "code";') or {}
	db.exec('ALTER TABLE lambdas ADD COLUMN entrypoint TEXT NOT NULL DEFAULT "";') or {}
	db.exec('ALTER TABLE lambdas ADD COLUMN project_dir TEXT NOT NULL DEFAULT "";') or {}
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
	h = h ^ (vlambda_slug_counter * 2654435761)
	return 'l${h:016x}'
}

struct TokenBucket {
mut:
	tokens f64
	last   f64
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
		return http.Response{
			status_code: 200
			body: get_ui_html()
			header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'text/html' })
		}
	}

	// Route: /async/<slug> (fire-and-forget)
	if req.url.starts_with('/async/') {
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
		parts := code_method.split('|')
		code := if parts.len > 0 { parts[0] } else { '' }
		method := if parts.len > 1 { parts[1] } else { 'GET' }
		timeout := if parts.len > 2 { parts[2].int() } else { 0 }
		// memory quota stored but global enforcement only for now.
		mut q := ''
		if qi2 := req.url.index('?') {
			q = req.url[qi2+1..]
		}
		task_id := create_task_async(code, method, req.url, q, req.data)
		return http.Response{
			status_code: 202
			body: task_id
			header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
		}
	}

	// Route: /task/<id>
	if req.url.starts_with('/task/') {
		task_id := req.url[6..]
		result := get_task_status(task_id)
		return http.Response{
			status_code: 200
			body: result
			header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
		}
	}

	// Extract auth token from header
	mut auth_token := ''
	if auth_hdr := req.header.get(.authorization) {
		if auth_hdr.starts_with('Bearer ') {
			auth_token = auth_hdr[7..]
		}
	}
	auth_uid := if auth_token.len > 0 { auth_verify_token(auth_token) } else { -1 }

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
		mut body := '[]'
		ptr := &body
		vcraft.with_gil(fn [ptr] () {
			args := []voidptr{}
			r := vcraft.call(vlambda_get_dlq, args)
			if r.is_null() {
				vcraft.clear_error()
				return
			}
			if r.type_name() == 'str' {
				ss := vcraft.from_py_string(r, 'result') or { '[]' }
				r.decref()
				unsafe { *ptr = ss + '' }
				return
			}
			r.decref()
		})
		return http.Response{ status_code: 200, body: body, header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
	}

	// Route: /health
	if req.url == '/health' {
		return http.Response{
			status_code: 200
			body: '{"status": "ok"}'
			header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
		}
	}

	// Route: /ready
	if req.url == '/ready' {
		if vlambda_executor == unsafe { nil } {
			return http.Response{
				status_code: 503
				body: '{"status": "not ready"}'
				header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
			}
		}
		return http.Response{
			status_code: 200
			body: '{"status": "ready"}'
			header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
		}
	}

	// Route: /shutdown (POST)
	if req.url == '/shutdown' && req.method == .post {
		spawn fn () {
			time.sleep(200 * time.millisecond)
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
		return http.Response{
			status_code: 200
			body: get_ui_html()
			header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'text/html' })
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
		return http.Response{
			status_code: 200
			body: body
			header: http.new_header(http.HeaderConfig{ key: .content_type, value: ctype })
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
		mut days := 1
		mut slug := ''
		if req.url.contains('?') {
			qs := req.url.all_after('?')
			for part in qs.split('&') {
				if part.starts_with('days=') { days = part.all_after('=').int() }
				if part.starts_with('slug=') { slug = part.all_after('=') }
			}
		}
		mut body := '{"days": ${days}, "count": 0, "items": []}'
		ptr := &body
		if vlambda_query_logs != unsafe { nil } {
			vcraft.with_gil(fn [days, slug, ptr] () {
				args := [vcraft.to_py_string(days.str()).ptr, vcraft.to_py_string(slug).ptr]
				r := vcraft.call(vlambda_query_logs, args)
				vcraft.borrow(args[0]).decref()
				vcraft.borrow(args[1]).decref()
				if r.is_null() { vcraft.clear_error(); return }
				if r.type_name() == 'str' {
					ss := vcraft.from_py_string(r, 'result') or { '{"days": ${days}, "count": 0, "items": []}' }
					r.decref()
					unsafe { *ptr = ss + '' }
					return
				}
				r.decref()
			})
		}
		return http.Response{ status_code: 200, body: body, header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
	}

	// Route: /metrics/json (JSON format for UI charts)
	if req.url == '/metrics/json' {
		if auth_uid < 0 {
			return http.Response{
				status_code: 401
				body: '{"error": "unauthorized"}'
				header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
			}
		}
		avg := if vlambda_latency_count > 0 { vlambda_latency_sum / f64(vlambda_latency_count) } else { 0.0 }
		mut body := '{"requests_total": ${vlambda_req_total}, "errors_total": ${vlambda_req_errors}, "lambdas_created": ${vlambda_lambdas_created}, "latency_avg_ms": ${avg}, "latency_count": ${vlambda_latency_count}, "per_lambda": ['
		mut first := true
		for slug, count in vlambda_lambda_invocations {
			errs := vlambda_lambda_errors[slug] or { u64(0) }
			lat := vlambda_lambda_latency[slug] or { f64(0) }
			avg_lat := if count > 0 { lat / f64(count) } else { 0.0 }
			if !first { body += ',' }
			first = false
			body += '{"slug": "${slug}", "invocations": ${count}, "errors": ${errs}, "latency_avg_ms": ${avg_lat}}'
		}
		body += ']}'
		return http.Response{
			status_code: 200
			body: body
			header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
		}
	}

	// Route: /metrics
	if req.url == '/metrics' {
		avg := if vlambda_latency_count > 0 { vlambda_latency_sum / f64(vlambda_latency_count) } else { 0.0 }
		mut body := 'vlambda_requests_total ${vlambda_req_total}\nvlambda_errors_total ${vlambda_req_errors}\nvlambda_lambdas_created_total ${vlambda_lambdas_created}\nvlambda_latency_avg_ms ${avg}\nvlambda_latency_count ${vlambda_latency_count}\n'
		// Per-lambda metrics
		for slug, count in vlambda_lambda_invocations {
			errs := vlambda_lambda_errors[slug] or { u64(0) }
			lat := vlambda_lambda_latency[slug] or { f64(0) }
			avg_lat := if count > 0 { lat / f64(count) } else { 0.0 }
			body += 'vlambda_lambda_invocations{slug="${slug}"} ${count}\n'
			body += 'vlambda_lambda_errors{slug="${slug}"} ${errs}\n'
			body += 'vlambda_lambda_latency_avg_ms{slug="${slug}"} ${avg_lat}\n'
		}
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
		mut days := 7
		if req.url.contains('?') {
			qs := req.url.all_after('?')
			for part in qs.split('&') {
				if part.starts_with('days=') {
					days = part.all_after('=').int()
				}
			}
		}
		mut body := '{"days": ${days}, "items": []}'
		ptr := &body
		if vlambda_query_metrics != unsafe { nil } {
			vcraft.with_gil(fn [days, ptr] () {
				args := [vcraft.to_py_string(days.str()).ptr]
				r := vcraft.call(vlambda_query_metrics, args)
				vcraft.borrow(args[0]).decref()
				if r.is_null() {
					vcraft.clear_error()
					return
				}
				if r.type_name() == 'str' {
					ss := vcraft.from_py_string(r, 'result') or { '{"days": ${days}, "items": []}' }
					r.decref()
					unsafe { *ptr = ss }
					return
				}
				r.decref()
			})
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
				if part.starts_with('slug=') { slug = part.all_after('=') }
			}
		}
		mut body := '{"days": ${days}, "count": 0, "avg_ms": 0, "p50_ms": 0, "p95_ms": 0, "p99_ms": 0}'
		ptr := &body
		if vlambda_query_stats != unsafe { nil } {
			vcraft.with_gil(fn [days, slug, ptr] () {
				args := [vcraft.to_py_string(days.str()).ptr, vcraft.to_py_string(slug).ptr]
				r := vcraft.call(vlambda_query_stats, args)
				vcraft.borrow(args[0]).decref()
				vcraft.borrow(args[1]).decref()
				if r.is_null() {
					vcraft.clear_error()
					return
				}
				if r.type_name() == 'str' {
					ss := vcraft.from_py_string(r, 'result') or { '[]' }
					r.decref()
					unsafe { *ptr = ss + '' }
					return
				}
				r.decref()
			})
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
				if part.starts_with('slug=') { slug = part.all_after('=') }
			}
		}
		mut body := '{"days": ${days}, "requests_total": 0, "errors_total": 0, "cold_starts": 0, "per_lambda": []}'
		ptr := &body
		if vlambda_query_aggregate != unsafe { nil } {
			vcraft.with_gil(fn [days, slug, ptr] () {
				args := [vcraft.to_py_string(days.str()).ptr, vcraft.to_py_string(slug).ptr]
				r := vcraft.call(vlambda_query_aggregate, args)
				vcraft.borrow(args[0]).decref()
				vcraft.borrow(args[1]).decref()
				if r.is_null() { vcraft.clear_error(); return }
				if r.type_name() == 'str' {
					ss := vcraft.from_py_string(r, 'result') or { '{"days": ${days}, "requests_total": 0, "errors_total": 0, "cold_starts": 0, "per_lambda": []}' }
					r.decref()
					unsafe { *ptr = ss + '' }
					return
				}
				r.decref()
			})
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
				if part.starts_with('slug=') { slug = part.all_after('=') }
			}
		}
		mut body := '{"days": ${days}, "items": []}'
		ptr := &body
		if vlambda_query_summary != unsafe { nil } {
			vcraft.with_gil(fn [days, slug, ptr] () {
				args := [vcraft.to_py_string(days.str()).ptr, vcraft.to_py_string(slug).ptr]
				r := vcraft.call(vlambda_query_summary, args)
				vcraft.borrow(args[0]).decref()
				vcraft.borrow(args[1]).decref()
				if r.is_null() {
					vcraft.clear_error()
					return
				}
				if r.type_name() == 'str' {
					ss := vcraft.from_py_string(r, 'result') or { '{"days": ${days}, "items": []}' }
					r.decref()
					unsafe { *ptr = ss + '' }
					return
				}
				r.decref()
			})
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
				if part.starts_with('slug=') { slug = part.all_after('=') }
			}
		}
		mut body := ''
		ptr := &body
		if vlambda_export_metrics != unsafe { nil } {
			vcraft.with_gil(fn [days, slug, ptr] () {
				args := [vcraft.to_py_string(days.str()).ptr, vcraft.to_py_string(slug).ptr]
				r := vcraft.call(vlambda_export_metrics, args)
				vcraft.borrow(args[0]).decref()
				vcraft.borrow(args[1]).decref()
				if r.is_null() {
					vcraft.clear_error()
					return
				}
				if r.type_name() == 'str' {
					ss := vcraft.from_py_string(r, 'result') or { '' }
					r.decref()
					unsafe { *ptr = ss + '' }
					return
				}
				r.decref()
			})
		}
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
		mut body := '{}'
		ptr := &body
		if vlambda_pool_stats != unsafe { nil } {
			vcraft.with_gil(fn [ptr] () {
				args := []voidptr{}
				r := vcraft.call(vlambda_pool_stats, args)
				if r.is_null() {
					vcraft.clear_error()
					return
				}
				if r.type_name() == 'str' {
					ss := vcraft.from_py_string(r, 'result') or { '{}' }
					r.decref()
					unsafe { *ptr = ss + '' }
					return
				}
				r.decref()
			})
		}
		return http.Response{
			status_code: 200
			body: body
			header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
		}
	}

	// Route: /admin/reset (POST, token protected)
	if req.url == '/admin/reset' && req.method == .post {
		token := req.header.get_custom('X-Admin-Token', http.HeaderQueryConfig{}) or { '' }
		vlambda_resetting = true
		mut body := '{"error": "not configured"}'
		ptr := &body
		if vlambda_admin_reset != unsafe { nil } {
			vcraft.with_gil(fn [token, ptr] () {
				args := [vcraft.to_py_string(token).ptr]
				r := vcraft.call(vlambda_admin_reset, args)
				vcraft.borrow(args[0]).decref()
				if r.is_null() {
					vcraft.clear_error()
					return
				}
				if r.type_name() == 'str' {
					ss := vcraft.from_py_string(r, 'result') or { '{}' }
					r.decref()
					unsafe { *ptr = ss + '' }
					return
				}
				r.decref()
			})
		}
		vlambda_resetting = false
		return http.Response{
			status_code: 200
			body: body
			header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
		}
	}

	// Route: /register (POST)
	if req.url == '/register' && req.method == .post {
		// Parse username:password from body
		parts := req.data.split(':')
		if parts.len < 2 {
			return http.Response{
				status_code: 400
				body: '{"error": "expected username:password"}'
				header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
			}
		}
		uid := auth_register_user(parts[0], parts[1])
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
		token := auth_login_user(parts[0], parts[1])
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
		project_info := db_get_project(slug)
		if project_info == '' {
			return http.Response{ status_code: 404, body: '{"error": "project not found"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		project_dir := project_info.all_before('|')
		rest_info := project_info.all_after('|')
		entrypoint := rest_info.all_before('|')
		method := rest_info.all_after('|')
		if req.method == .get {
			mut body := '{}'
			ptr := &body
			if vlambda_get_project_json != unsafe { nil } {
				vcraft.with_gil(fn [project_dir, entrypoint, slug, method, ptr] () {
					args := [vcraft.to_py_string(project_dir).ptr, vcraft.to_py_string(entrypoint).ptr, vcraft.to_py_string(slug).ptr, vcraft.to_py_string(method).ptr]
					r := vcraft.call(vlambda_get_project_json, args)
					for a in args { vcraft.borrow(a).decref() }
					if r.is_null() { vcraft.clear_error(); return }
					if r.type_name() == 'str' {
						ss := vcraft.from_py_string(r, 'result') or { '{}' }
						r.decref()
						unsafe { *ptr = ss + '' }
						return
					}
					r.decref()
				})
			}
			return http.Response{ status_code: 200, body: body, header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		if req.method == .put {
			if req.data.len > vlambda_max_body_size * 2 {
				return http.Response{ status_code: 413, body: '{"error": "project too large"}', header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
			}
			mut body := '{}'
			ptr := &body
			request_body := req.data
			if vlambda_save_project_json != unsafe { nil } {
				vcraft.with_gil(fn [project_dir, entrypoint, ptr, request_body] () {
					args := [vcraft.to_py_string(project_dir).ptr, vcraft.to_py_string(request_body).ptr, vcraft.to_py_string(entrypoint).ptr]
					r := vcraft.call(vlambda_save_project_json, args)
					for a in args { vcraft.borrow(a).decref() }
					if r.is_null() { vcraft.clear_error(); return }
					if r.type_name() == 'str' {
						ss := vcraft.from_py_string(r, 'result') or { '{}' }
						r.decref()
						unsafe { *ptr = ss + '' }
						return
					}
					r.decref()
				})
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
				mut entrypoint := 'main.py'
				mut name := ''
				mut method := 'GET'
				if req.url.contains('?') {
					qs := req.url.all_after('?')
					for part in qs.split('&') {
						if part.starts_with('entrypoint=') || part.starts_with('entry=') {
							entrypoint = part.all_after('=')
						}
						if part.starts_with('name=') {
							name = part.all_after('=')
						}
						if part.starts_with('method=') {
							method = part.all_after('=')
						}
					}
				}
				if vlambda_create_project == unsafe { nil } {
					return http.Response{
						status_code: 500
						body: '{"error": "project creation not configured"}'
						header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
					}
				}
				mut result := '{"error": "not started"}'
				ptr := &result
				vcraft.with_gil(fn [req, entrypoint, name, method, auth_uid, ptr] () {
					args := [vcraft.to_py_string(req.data).ptr, vcraft.to_py_string(entrypoint).ptr, vcraft.to_py_string(name).ptr, vcraft.to_py_string(method).ptr, vcraft.to_py_int(auth_uid).ptr]
					r := vcraft.call(vlambda_create_project, args)
					for a in args { vcraft.borrow(a).decref() }
					if r.is_null() {
						vcraft.clear_error()
						return
					}
					if r.type_name() == 'str' {
						ss := vcraft.from_py_string(r, 'result') or { '{"error": "bad result"}' }
						r.decref()
						unsafe { *ptr = ss + '' }
						return
					}
					r.decref()
				})
				// result may be empty if pointer assignment failed; fallback
				if result == '{"error": "not started"}' {
					result = '{"error": "project creation failed"}'
				}
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
						method = part.all_after('=')
					}
					if part.starts_with('name=') {
						name = part.all_after('=')
					}
					if part.starts_with('timeout=') {
						timeout = part.all_after('=').int()
					}
					if part.starts_with('memory_mb=') {
						memory_mb = part.all_after('=').int()
					}
				}
			}
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
			if req.url.contains('?') {
				qs := req.url.all_after('?')
				for part in qs.split('&') {
					if part.starts_with('limit=') {
						limit = part.all_after('=').int()
					}
					if part.starts_with('offset=') {
						offset = part.all_after('=').int()
					}
				}
			}
			lambdas := db_list_lambdas_page(auth_uid, limit, offset)
			count := db_count_lambdas(auth_uid)
			return http.Response{
				status_code: 200
				body: '{"lambdas": "${lambdas}", "count": ${count}, "limit": ${limit}, "offset": ${offset}}'
				header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
			}
		}
		return http.Response{
			status_code: 405
			body: 'method not allowed'
			header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'text/plain' })
		}
	}

	// Route: /f/<slug> (invoke lambda)
	if req.url.starts_with('/f/') {
		// strip query string
		mut url_path := req.url
		qi := url_path.index('?') or { url_path.len }
		url_path = url_path[..qi]
		slug := url_path[3..]
		mut alias := ''
		if req.url.contains('?') {
			qs := req.url.all_after('?')
			for part in qs.split('&') {
				if part.starts_with('alias=') {
					alias = part.all_after('=')
				}
			}
		}
		code_method := if alias != '' {
			resolved := db_resolve_alias(alias)
			if resolved == '' { '' } else {
				aparts := resolved.split('|')
				aslug := if aparts.len > 0 { aparts[0] } else { '' }
				aver := if aparts.len > 1 { aparts[1].int() } else { 0 }
				if aslug != slug { '' } else { db_get_version_code(slug, aver) }
			}
		} else { db_get_lambda_code(slug) }
		project_info := db_get_project(slug)
		if code_method == '' && project_info == '' {
			vlambda_req_errors++
			return http.Response{
				status_code: 404
				body: '{"error": "lambda not found"}'
				header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
			}
		}
		if project_info != '' {
			pparts := project_info.split('|')
			project_dir := if pparts.len > 0 { pparts[0] } else { '' }
			entrypoint := if pparts.len > 1 { pparts[1] } else { 'main.py' }
			method := if pparts.len > 2 { pparts[2] } else { 'GET' }
			if method != req.method.str() {
				return http.Response{
					status_code: 405
					body: '{"error": "method not allowed, expected ${method}"}'
					header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
				}
			}
			mut q := ''
			if qi2 := req.url.index('?') {
				q = req.url[qi2+1..]
			}
			result := run_project(project_dir, entrypoint, method, req.url, q, req.data)
			elapsed := time.since(start_time).microseconds()
			vlambda_latency_sum += f64(elapsed) / 1000.0
			vlambda_latency_count++
			vlambda_lambda_invocations[slug] = (vlambda_lambda_invocations[slug] or { u64(0) }) + 1
			vlambda_lambda_latency[slug] = (vlambda_lambda_latency[slug] or { f64(0) }) + f64(elapsed) / 1000.0
			if result.contains('error') {
				vlambda_lambda_errors[slug] = (vlambda_lambda_errors[slug] or { u64(0) }) + 1
			}
			return http.Response{
				status_code: 200
				body: result
				header: http.new_header(http.HeaderConfig{ key: .content_type, value: lambda_content_type(result) })
			}
		}
		// Split code and method
		parts := code_method.split('|')
		code := if parts.len > 0 { parts[0] } else { '' }
		method := if parts.len > 1 { parts[1] } else { 'GET' }
		timeout := if parts.len > 2 { parts[2].int() } else { 0 }
		memory_mb := if parts.len > 3 { parts[3].int() } else { 0 }
		// Check method
		if method != req.method.str() {
			return http.Response{
				status_code: 405
				body: '{"error": "method not allowed, expected ${method}"}'
				header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
			}
		}
		// Run lambda via Python bridge
		// extract query string from url
		mut q := ''
		if qi2 := req.url.index('?') {
			q = req.url[qi2+1..]
		}
		vlambda_request_counter++
		request_id := 'req-${vlambda_request_counter}'
		result := run_lambda(request_id, slug, code, method, req.url, q, req.data, timeout, memory_mb)
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
		mut resp_header := http.new_header(http.HeaderConfig{ key: .content_type, value: lambda_content_type(result) })
		resp_header.add_custom('X-Request-Id', request_id) or {}
		return http.Response{
			status_code: 200
			body: result
			header: resp_header
		}
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
			mut body := '{"packages": []}'
			ptr := &body
			if vlambda_list_packages != unsafe { nil } {
				vcraft.with_gil(fn [slug, kind, project_dir, ptr] () {
					args := [vcraft.to_py_string(slug).ptr, vcraft.to_py_string(kind).ptr, vcraft.to_py_string(project_dir).ptr]
					r := vcraft.call(vlambda_list_packages, args)
					for a in args { vcraft.borrow(a).decref() }
					if r.is_null() { vcraft.clear_error(); return }
					if r.type_name() == 'str' {
						ss := vcraft.from_py_string(r, 'result') or { '{"packages": []}' }
						r.decref()
						unsafe { *ptr = ss + '' }
						return
					}
					r.decref()
				})
			}
			return http.Response{ status_code: 200, body: body, header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
		}
		if req.method == .post {
			request_body := req.data
			mut body := '{"error": "install failed"}'
			ptr := &body
			if vlambda_install_packages != unsafe { nil } {
				vcraft.with_gil(fn [slug, kind, project_dir, request_body, ptr] () {
					args := [vcraft.to_py_string(slug).ptr, vcraft.to_py_string(kind).ptr, vcraft.to_py_string(project_dir).ptr, vcraft.to_py_string(request_body).ptr]
					r := vcraft.call(vlambda_install_packages, args)
					for a in args { vcraft.borrow(a).decref() }
					if r.is_null() { vcraft.clear_error(); return }
					if r.type_name() == 'str' {
						ss := vcraft.from_py_string(r, 'result') or { '{"error": "install failed"}' }
						r.decref()
						unsafe { *ptr = ss + '' }
						return
					}
					r.decref()
				})
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
		mut body := '{"versions": []}'
		ptr := &body
		if vlambda_list_versions != unsafe { nil } {
			vcraft.with_gil(fn [slug, ptr] () {
				args := [vcraft.to_py_string(slug).ptr]
				r := vcraft.call(vlambda_list_versions, args)
				vcraft.borrow(args[0]).decref()
				if r.is_null() { vcraft.clear_error(); return }
				if r.type_name() == 'str' {
					ss := vcraft.from_py_string(r, 'result') or { '{"versions": []}' }
					r.decref()
					unsafe { *ptr = ss + '' }
					return
				}
				r.decref()
			})
		}
		return http.Response{ status_code: 200, body: body, header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' }) }
	}

	// Route: /vlambda/<slug>/aliases
	if req.url.starts_with('/vlambda/') && req.url.contains('/aliases') {
		mut url_path := req.url
		qi := url_path.index('?') or { url_path.len }
		url_path = url_path[..qi]
		slug := url_path[9..url_path.index('/aliases') or { url_path.len }]
		mut body := '{"aliases": []}'
		ptr := &body
		if vlambda_list_aliases != unsafe { nil } {
			vcraft.with_gil(fn [slug, ptr] () {
				args := [vcraft.to_py_string(slug).ptr]
				r := vcraft.call(vlambda_list_aliases, args)
				vcraft.borrow(args[0]).decref()
				if r.is_null() { vcraft.clear_error(); return }
				if r.type_name() == 'str' {
					ss := vcraft.from_py_string(r, 'result') or { '{"aliases": []}' }
					r.decref()
					unsafe { *ptr = ss + '' }
					return
				}
				r.decref()
			})
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
						alias = part.all_after('=')
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
			mut name := ''
			mut timeout := 0
			mut memory_mb := 0
			if req.url.contains('?') {
				qs := req.url.all_after('?')
				for part in qs.split('&') {
					if part.starts_with('method=') {
						method = part.all_after('=')
					}
					if part.starts_with('name=') {
						name = part.all_after('=')
					}
					if part.starts_with('timeout=') {
						timeout = part.all_after('=').int()
					}
					if part.starts_with('memory_mb=') {
						memory_mb = part.all_after('=').int()
					}
				}
			}
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
pub fn db_init(db_path string) bool {
	vlambda_db_path = db_path
	mut db := init_db(db_path) or {
		return false
	}
	db.close() or {}
	return true
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
	mut cell := -1
	ptr := &cell
	vcraft.with_gil(fn [token, ptr] () {
		args := [vcraft.to_py_string(token).ptr]
		r := vcraft.call(vlambda_auth_verifier, args)
		vcraft.borrow(args[0]).decref()
		if r.is_null() {
			vcraft.clear_error()
			unsafe { *ptr = -1 }
			return
		}
		uid := int(vcraft.from_py_int(r, 'uid') or { -1 })
		r.decref()
		unsafe { *ptr = uid }
	})
	return cell
}

@[vc_fn]
pub fn db_create_lambda(slug string, name string, code string, method string, owner_id int, timeout int, memory_mb int) bool {
	mut db := init_db(vlambda_db_path) or { return false }
	defer { db.close() or {} }
	db.exec_param_many('INSERT INTO lambdas (slug, name, code, method, owner_id, created_at, timeout, memory_mb, version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1);',
		[slug, name, code, method, owner_id.str(), '2026-01-01T00:00:00Z', timeout.str(), memory_mb.str()]) or { return false }
	db.exec_param_many('INSERT INTO lambda_versions (slug, version, code, method, entrypoint, project_dir, kind, created_at) VALUES (?, 1, ?, ?, "", "", "code", ?);',
		[slug, code, method, '2026-01-01T00:00:00Z']) or {}
	vlambda_missing_cache.delete(slug)
	return true
}

@[vc_fn]
pub fn db_update_lambda(slug string, name string, code string, method string, owner_id int, timeout int, memory_mb int) bool {
	mut db := init_db(vlambda_db_path) or { return false }
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
	vlambda_code_cache.delete(slug)
	return true
}


@[vc_fn]
pub fn prewarm_lambda(code string) {
	if vlambda_executor == unsafe { nil } { return }
	vcraft.with_gil(fn [code] () {
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
	})
}

@[vc_fn]
pub fn db_get_lambda_code(slug string) string {
	// Check in-memory cache first
	if cached := vlambda_code_cache[slug] {
		return cached
	}
	mut db := init_db(vlambda_db_path) or { return '' }
	defer { db.close() or {} }
	if vlambda_missing_cache[slug] {
		return ''
	}
	rows := db.exec_param('SELECT code, method, timeout, memory_mb FROM lambdas WHERE slug = ?;', slug) or { return '' }
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
	result := code + '|' + method + '|' + timeout + '|' + memory_mb
	vlambda_code_cache[slug] = result
	return result
}

@[vc_fn]
pub fn db_get_lambda_name(slug string) string {
	mut db := init_db(vlambda_db_path) or { return '' }
	defer { db.close() or {} }
	rows := db.exec_param('SELECT name FROM lambdas WHERE slug = ?;', slug) or { return '' }
	if rows.len == 0 { return '' }
	return rows[0].vals[0] or { return '' }
}

@[vc_fn]
pub fn db_create_project(slug string, name string, method string, entrypoint string, project_dir string, owner_id int) bool {
	if db_count_lambdas(owner_id) >= max_lambdas_per_user() { return false }
	mut db := init_db(vlambda_db_path) or { return false }
	defer { db.close() or {} }
	db.exec_param_many('INSERT INTO lambdas (slug, name, code, method, owner_id, created_at, kind, entrypoint, project_dir, timeout, memory_mb, version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 1);',
		[slug, name, '', method, owner_id.str(), '2026-01-01T00:00:00Z', 'project', entrypoint, project_dir]) or { return false }
	db.exec_param_many('INSERT INTO lambda_versions (slug, version, code, method, entrypoint, project_dir, kind, created_at) VALUES (?, 1, ?, ?, ?, ?, "project", ?);',
		[slug, '', method, entrypoint, project_dir, '2026-01-01T00:00:00Z']) or {}
	return true
}

@[vc_fn]
pub fn db_get_project(slug string) string {
	mut db := init_db(vlambda_db_path) or { return '' }
	defer { db.close() or {} }
	rows := db.exec_param('SELECT project_dir, entrypoint, method FROM lambdas WHERE slug = ? AND kind = "project";', slug) or { return '' }
	if rows.len == 0 { return '' }
	project_dir := rows[0].vals[0] or { return '' }
	entrypoint := rows[0].vals[1] or { return '' }
	method := rows[0].vals[2] or { return '' }
	return project_dir + '|' + entrypoint + '|' + method
}

@[vc_fn]
pub fn db_get_lambda_owner(slug string) int {
	mut db := init_db(vlambda_db_path) or { return -1 }
	defer { db.close() or {} }
	rows := db.exec_param('SELECT owner_id FROM lambdas WHERE slug = ?;', slug) or { return -1 }
	if rows.len == 0 { return -1 }
	n := rows[0].vals[0] or { return -1 }
	return n.int()
}

@[vc_fn]
pub fn db_create_alias(alias string, slug string, version int, owner_id int) bool {
	mut db := init_db(vlambda_db_path) or { return false }
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
		[alias, slug, ver.str(), owner_id.str()]) or { return false }
	return true
}

@[vc_fn]
pub fn db_resolve_alias(alias string) string {
	mut db := init_db(vlambda_db_path) or { return '' }
	defer { db.close() or {} }
	rows := db.exec_param('SELECT slug, version FROM lambda_aliases WHERE alias = ?;', alias) or { return '' }
	if rows.len == 0 { return '' }
	slug := rows[0].vals[0] or { return '' }
	version := rows[0].vals[1] or { return '' }
	return slug + '|' + version
}

@[vc_fn]
pub fn db_get_version_code(slug string, version int) string {
	mut db := init_db(vlambda_db_path) or { return '' }
	defer { db.close() or {} }
	rows := db.exec_param_many('SELECT code, method FROM lambda_versions WHERE slug = ? AND version = ?;', [slug, version.str()]) or { return '' }
	if rows.len == 0 { return '' }
	code := rows[0].vals[0] or { return '' }
	method := rows[0].vals[1] or { return '' }
	return code + '|' + method + '|0|0' 
}

@[vc_fn]
pub fn db_delete_lambda(slug string, owner_id int) bool {
	mut db := init_db(vlambda_db_path) or { return false }
	defer { db.close() or {} }
	db.exec_param_many('DELETE FROM lambdas WHERE slug = ? AND owner_id = ?;', [slug, owner_id.str()]) or { return false }
	vlambda_code_cache.delete(slug)
	vlambda_missing_cache.delete(slug)
	return true
}

@[vc_fn]
pub fn db_list_lambdas(owner_id int) string {
	mut db := init_db(vlambda_db_path) or { return '' }
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
	mut db := init_db(vlambda_db_path) or { return 0 }
	defer { db.close() or {} }
	rows := db.exec_param('SELECT COUNT(*) FROM lambdas WHERE owner_id = ?;', owner_id.str()) or { return 0 }
	if rows.len == 0 { return 0 }
	n := rows[0].vals[0] or { return 0 }
	return n.int()
}

@[vc_fn]
pub fn db_list_lambdas_page(owner_id int, limit int, offset int) string {
	mut db := init_db(vlambda_db_path) or { return '' }
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

pub fn run_lambda(request_id string, slug string, code string, method string, path string, query string, body string, timeout int, memory_mb int) string {
	vlambda_result_str = '{"error": "not started"}'
	start_time := time.now()
	vcraft.with_gil(fn [request_id, slug, code, method, path, query, body, timeout, memory_mb, start_time] () {
		if vlambda_executor == unsafe { nil } {
			vlambda_result_str = '{"error": "no executor registered"}'
			if vlambda_metrics_enabled && vlambda_record_metric != unsafe { nil } && (vlambda_metrics_sample <= 1 || int(vlambda_metric_counter) % vlambda_metrics_sample == 0) {
				elapsed := time.since(start_time).microseconds()
				args := [vcraft.to_py_string(request_id).ptr, vcraft.to_py_string(slug).ptr, vcraft.to_py_string(method).ptr, vcraft.to_py_string(path).ptr, vcraft.to_py_string('${f64(elapsed) / 1000.0}').ptr, vcraft.to_py_string(vlambda_result_str).ptr]
				r := vcraft.call(vlambda_record_metric, args)
				for a in args { vcraft.borrow(a).decref() }
				if !r.is_null() { r.decref() }
			}
			return
		}
		args := [vcraft.to_py_string(request_id).ptr, vcraft.to_py_string(slug).ptr, vcraft.to_py_string(code).ptr, vcraft.to_py_string(method).ptr, vcraft.to_py_string(path).ptr, vcraft.to_py_string(query).ptr, body_to_py_bytes(body).ptr, vcraft.to_py_int(timeout).ptr, vcraft.to_py_int(memory_mb).ptr]
		result := vcraft.call(vlambda_executor, args)
		for a in args { vcraft.borrow(a).decref() }
		if result.is_null() {
			vcraft.clear_error()
			vlambda_result_str = '{"error": "execution failed"}'
			if vlambda_metrics_enabled && vlambda_record_metric != unsafe { nil } && (vlambda_metrics_sample <= 1 || int(vlambda_metric_counter) % vlambda_metrics_sample == 0) {
				elapsed := time.since(start_time).microseconds()
				margs := [vcraft.to_py_string(request_id).ptr, vcraft.to_py_string(slug).ptr, vcraft.to_py_string(method).ptr, vcraft.to_py_string(path).ptr, vcraft.to_py_string('${f64(elapsed) / 1000.0}').ptr, vcraft.to_py_string(vlambda_result_str).ptr]
				r := vcraft.call(vlambda_record_metric, margs)
				for a in margs { vcraft.borrow(a).decref() }
				if !r.is_null() { r.decref() }
			}
			return
		}
		if result.type_name() == 'str' {
			ss := vcraft.from_py_string(result, 'result') or { '{"error": "bad result"}' }
			result.decref()
			vlambda_result_str = ss + ''
			if vlambda_metrics_enabled && vlambda_record_metric != unsafe { nil } && (vlambda_metrics_sample <= 1 || int(vlambda_metric_counter) % vlambda_metrics_sample == 0) {
				elapsed := time.since(start_time).microseconds()
				margs := [vcraft.to_py_string(request_id).ptr, vcraft.to_py_string(slug).ptr, vcraft.to_py_string(method).ptr, vcraft.to_py_string(path).ptr, vcraft.to_py_string('${f64(elapsed) / 1000.0}').ptr, vcraft.to_py_string(if ss.contains('error') { ss } else { '' }).ptr]
				r := vcraft.call(vlambda_record_metric, margs)
				for a in margs { vcraft.borrow(a).decref() }
				if !r.is_null() { r.decref() }
			}
			return
		}
		if result.type_name() == 'bytes' {
			ss := vcraft.from_py_bytes(result, 'result') or { '{"error": "bad result"}' }
			result.decref()
			vlambda_result_str = ss + ''
			if vlambda_metrics_enabled && vlambda_record_metric != unsafe { nil } && (vlambda_metrics_sample <= 1 || int(vlambda_metric_counter) % vlambda_metrics_sample == 0) {
				elapsed := time.since(start_time).microseconds()
				margs := [vcraft.to_py_string(request_id).ptr, vcraft.to_py_string(slug).ptr, vcraft.to_py_string(method).ptr, vcraft.to_py_string(path).ptr, vcraft.to_py_string('${f64(elapsed) / 1000.0}').ptr, vcraft.to_py_string(if ss.contains('error') { ss } else { '' }).ptr]
				r := vcraft.call(vlambda_record_metric, margs)
				for a in margs { vcraft.borrow(a).decref() }
				if !r.is_null() { r.decref() }
			}
			return
		}
		ss := result.str()
		result.decref()
		vlambda_result_str = ss + ''
		if vlambda_metrics_enabled && vlambda_record_metric != unsafe { nil } && (vlambda_metrics_sample <= 1 || int(vlambda_metric_counter) % vlambda_metrics_sample == 0) {
			elapsed := time.since(start_time).microseconds()
			margs := [vcraft.to_py_string(slug).ptr, vcraft.to_py_string(method).ptr, vcraft.to_py_string(path).ptr, vcraft.to_py_string('${f64(elapsed) / 1000.0}').ptr, vcraft.to_py_string(if ss.contains('error') { ss } else { '' }).ptr]
			r := vcraft.call(vlambda_record_metric, margs)
			for a in margs { vcraft.borrow(a).decref() }
			if !r.is_null() { r.decref() }
		}
	})
	return vlambda_result_str
}

@[vc_fn]
pub fn run_project(project_dir string, entrypoint string, method string, path string, query string, body string) string {
	vlambda_result_str = '{"error": "not started"}'
	vcraft.with_gil(fn [project_dir, entrypoint, method, path, query, body] () {
		mut req_items := map[string]vcraft.PyObj{}
		req_items['method'] = vcraft.to_py_string(method)
		req_items['path'] = vcraft.to_py_string(path)
		req_items['query_string'] = vcraft.to_py_string(query)
		req_items['body'] = body_to_py_bytes(body)
		req_obj := vcraft.to_py_obj_dict(req_items)

		if vlambda_run_project == unsafe { nil } {
			req_obj.decref()
			vlambda_result_str = '{"error": "no project runner registered"}'
			return
		}
		args := [vcraft.to_py_string(project_dir).ptr, vcraft.to_py_string(entrypoint).ptr, req_obj.ptr]
		result := vcraft.call(vlambda_run_project, args)
		req_obj.decref()
		for a in args { vcraft.borrow(a).decref() }
		if result.is_null() {
			vcraft.clear_error()
			vlambda_result_str = '{"error": "execution failed"}'
			return
		}
		if result.type_name() == 'str' {
			ss := vcraft.from_py_string(result, 'result') or { '{"error": "bad result"}' }
			result.decref()
			vlambda_result_str = ss + ''
			return
		}
		if result.type_name() == 'bytes' {
			ss := vcraft.from_py_bytes(result, 'result') or { '{"error": "bad result"}' }
			result.decref()
			vlambda_result_str = ss + ''
			return
		}
		ss := result.str()
		result.decref()
		vlambda_result_str = ss + ''
	})
	return vlambda_result_str
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
pub fn auth_register_user(username string, password string) int {
	if vlambda_auth_register == unsafe { nil } { return -1 }
	mut cell := -1
	ptr := &cell
	vcraft.with_gil(fn [username, password, ptr] () {
		args := [vcraft.to_py_string(username).ptr, vcraft.to_py_string(password).ptr]
		r := vcraft.call(vlambda_auth_register, args)
		vcraft.borrow(args[0]).decref()
		vcraft.borrow(args[1]).decref()
		if r.is_null() {
			vcraft.clear_error()
			unsafe { *ptr = -1 }
			return
		}
		uid := int(vcraft.from_py_int(r, 'uid') or { -1 })
		r.decref()
		unsafe { *ptr = uid }
	})
	return cell
}

@[vc_fn]
pub fn auth_login_user(username string, password string) string {
	if vlambda_auth_login == unsafe { nil } { return '' }
	mut cell := ''
	ptr := &cell
	vcraft.with_gil(fn [username, password, ptr] () {
		args := [vcraft.to_py_string(username).ptr, vcraft.to_py_string(password).ptr]
		r := vcraft.call(vlambda_auth_login, args)
		vcraft.borrow(args[0]).decref()
		vcraft.borrow(args[1]).decref()
		if r.is_null() {
			vcraft.clear_error()
			unsafe { *ptr = '' }
			return
		}
		if r.type_name() == 'str' {
			s := vcraft.from_py_string(r, 'token') or { '' }
			r.decref()
			unsafe { *ptr = s }
			return
		}
		r.decref()
		unsafe { *ptr = '' }
	})
	return cell
}

@[vc_fn]
pub fn configure(max_body_size int, rate_limit int, invoke_timeout int, max_memory_mb int, cors_origin string, cert_path string, key_path string, metrics_enabled bool, metrics_sample int) {
	if max_body_size > 0 { vlambda_max_body_size = max_body_size }
	if rate_limit > 0 { vlambda_rate_limit = rate_limit }
	if invoke_timeout > 0 { vlambda_invoke_timeout = invoke_timeout }
	vlambda_max_memory_mb = max_memory_mb
	vlambda_metrics_enabled = metrics_enabled
	if metrics_sample > 0 { vlambda_metrics_sample = metrics_sample }
	if cors_origin != '' { vlambda_cors_origin = cors_origin }
	vlambda_cert_path = cert_path
	vlambda_key_path = key_path
}

@[vc_fn]
pub fn get_favicon() string {
	if vlambda_favicon == unsafe { nil } { return '' }
	mut ptr_cell := ''
	ptr := &ptr_cell
	vcraft.with_gil(fn [ptr] () {
		args := []voidptr{}
		r := vcraft.call(vlambda_favicon, args)
		if r.is_null() {
			vcraft.clear_error()
			unsafe { *ptr = 'NULL_RESULT' }
			return
		}
		if r.type_name() == 'bytes' {
			ss := vcraft.from_py_bytes(r, 'favicon') or { '' }
			r.decref()
			unsafe { *ptr = ss }
			return
		}
		r.decref()
	})
	return ptr_cell
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
pub fn register_list_packages(cb voidptr) { vlambda_list_packages = cb }

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
pub fn create_task_async(code string, method string, path string, query string, body string) string {
	if vlambda_create_task == unsafe { nil } { return '{"error": "not configured"}' }
	mut cell := ''
	ptr := &cell
	vcraft.with_gil(fn [code, method, path, query, body, ptr] () {
		args := [vcraft.to_py_string(code).ptr, vcraft.to_py_string(method).ptr, vcraft.to_py_string(path).ptr, vcraft.to_py_string(query).ptr, body_to_py_bytes(body).ptr]
		r := vcraft.call(vlambda_create_task, args)
		for a in args { vcraft.borrow(a).decref() }
		if r.is_null() {
			vcraft.clear_error()
			return
		}
		if r.type_name() == 'str' {
			ss := vcraft.from_py_string(r, 'result') or { '' }
			r.decref()
			unsafe { *ptr = ss }
			return
		}
		unsafe { *ptr = 'TYPE_' + r.type_name() }
		r.decref()
	})
	return cell
}

@[vc_fn]
pub fn get_task_status(task_id string) string {
	if vlambda_get_task == unsafe { nil } { return '{"error": "not configured"}' }
	mut cell := ''
	ptr := &cell
	vcraft.with_gil(fn [task_id, ptr] () {
		args := [vcraft.to_py_string(task_id).ptr]
		r := vcraft.call(vlambda_get_task, args)
		vcraft.borrow(args[0]).decref()
		if r.is_null() {
			vcraft.clear_error()
			return
		}
		if r.type_name() == 'str' {
			ss := vcraft.from_py_string(r, 'result') or { '' }
			r.decref()
			unsafe { *ptr = ss }
			return
		}
		r.decref()
	})
	return cell
}

@[vc_fn]
pub fn get_asset(name string) string {
	if vlambda_get_asset == unsafe { nil } { return 'NIL_CALLBACK' }
	mut cell := ''
	ptr := &cell
	vcraft.with_gil(fn [name, ptr] () {
		args := [vcraft.to_py_string(name).ptr]
		r := vcraft.call(vlambda_get_asset, args)
		vcraft.borrow(args[0]).decref()
		if r.is_null() {
			vcraft.clear_error()
			return
		}
		if r.type_name() == 'bytes' {
			ss := vcraft.from_py_bytes(r, 'asset') or { 'FROM_BYTES_ERR' }
			r.decref()
			unsafe { *ptr = ss + '' }
			return
		}
		if r.type_name() == 'str' {
			ss := vcraft.from_py_string(r, 'asset') or { 'FROM_STR_ERR' }
			r.decref()
			unsafe { *ptr = ss + '' }
			return
		}
		unsafe { *ptr = 'TYPE_' + r.type_name() }
		r.decref()
	})
	return cell
}

@[vc_fn]
pub fn clear_caches() {
	vlambda_code_cache = map[string]string{}
	vlambda_missing_cache = map[string]bool{}
}

@[vc_fn]
pub fn get_ui_html() string {
	if vlambda_ui_html == unsafe { nil } { return '<h1>vlambda</h1>' }
	mut ptr_cell := '<h1>vlambda</h1>'
	ptr := &ptr_cell
	vcraft.with_gil(fn [ptr] () {
		args := []voidptr{}
		r := vcraft.call(vlambda_ui_html, args)
		if r.is_null() {
			vcraft.clear_error()
			return
		}
		if r.type_name() == 'str' {
			ss := vcraft.from_py_string(r, 'html') or { '<h1>vlambda</h1>' }
			r.decref()
			unsafe { *ptr = ss }
			return
		}
		r.decref()
	})
	return ptr_cell
}

@[vc_fn]
pub fn register_validate_code(cb voidptr) {
	vlambda_validate_code = cb
}

@[vc_fn]
pub fn validate_code(code string) string {
	if vlambda_validate_code == unsafe { nil } { return '' }
	mut cell := ''
	ptr := &cell
	vcraft.with_gil(fn [code, ptr] () {
		args := [vcraft.to_py_string(code).ptr]
		r := vcraft.call(vlambda_validate_code, args)
		vcraft.borrow(args[0]).decref()
		if r.is_null() {
			vcraft.clear_error()
			unsafe { *ptr = '' }
			return
		}
		if r.type_name() == 'str' {
			ss := vcraft.from_py_string(r, 'result') or { '' }
			r.decref()
			unsafe { *ptr = ss }
			return
		}
		r.decref()
		unsafe { *ptr = '' }
	})
	return cell
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

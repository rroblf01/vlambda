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
	vlambda_create_task = voidptr(0)
	vlambda_get_task = voidptr(0)
	vlambda_result_str = '{"error": "not started"}'
	// Config
	vlambda_max_body_size = 1048576  // 1MB
	vlambda_rate_limit = 100          // req/s per IP
	vlambda_invoke_timeout = 30       // seconds
	vlambda_max_memory_mb = 0         // 0 = unlimited
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
	// Shutdown
	vlambda_shutdown = false
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
	// Rate limiting (simple per-IP, resets each second via counter)
	// For simplicity, track total requests per IP in a map; reset not implemented for brevity
	// (production would use token bucket with timestamps)


	// Memory check
	if vlambda_max_memory_mb > 0 {
		// V doesn't have easy memory query; skip enforcement here
		// (would use /proc/self/statm on Linux)
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

	// Route: /health
	if req.url == '/health' {
		return http.Response{
			status_code: 200
			body: '{"status": "ok"}'
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

	// Route: /favicon.ico
	if req.url == '/favicon.ico' {
		icon := get_favicon()
		return http.Response{
			status_code: 200
			body: icon
			header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'image/x-icon' })
		}
	}

	// Route: /metrics/json (JSON format for UI charts)
	if req.url == '/metrics/json' {
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


	// Extract auth token from header
	mut auth_token := ''
	if auth_hdr := req.header.get(.authorization) {
		if auth_hdr.starts_with('Bearer ') {
			auth_token = auth_hdr[7..]
		}
	}
	auth_uid := if auth_token.len > 0 { auth_verify_token(auth_token) } else { -1 }

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
			if req.url.contains('?') {
				qs := req.url.all_after('?')
				for part in qs.split('&') {
					if part.starts_with('method=') {
						method = part.all_after('=')
					}
					if part.starts_with('name=') {
						name = part.all_after('=')
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
			slug := make_slug(code)
			ok := db_create_lambda(slug, name, code, method, auth_uid)
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
		code_method := db_get_lambda_code(slug)
		if code_method == '' {
			vlambda_req_errors++
			return http.Response{
				status_code: 404
				body: '{"error": "lambda not found"}'
				header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
			}
		}
		// Split code and method
		parts := code_method.split('|')
		code := if parts.len > 0 { parts[0] } else { '' }
		method := if parts.len > 1 { parts[1] } else { 'GET' }
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
		result := run_lambda(code, method, req.url, q, req.data)
		elapsed := time.since(start_time).microseconds()
		vlambda_latency_sum += f64(elapsed) / 1000.0
		vlambda_latency_count++
		// Per-lambda metrics
		vlambda_lambda_invocations[slug] = (vlambda_lambda_invocations[slug] or { u64(0) }) + 1
		vlambda_lambda_latency[slug] = (vlambda_lambda_latency[slug] or { f64(0) }) + f64(elapsed) / 1000.0
		if result.contains('error') {
			vlambda_lambda_errors[slug] = (vlambda_lambda_errors[slug] or { u64(0) }) + 1
		}
		return http.Response{
			status_code: 200
			body: result
			header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'application/json' })
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
			if req.url.contains('?') {
				qs := req.url.all_after('?')
				for part in qs.split('&') {
					if part.starts_with('method=') {
						method = part.all_after('=')
					}
					if part.starts_with('name=') {
						name = part.all_after('=')
					}
				}
			}
			ok := db_update_lambda(slug, name, code, method, auth_uid)
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
		body: 'not found'
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
	db.close()
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
pub fn db_create_lambda(slug string, name string, code string, method string, owner_id int) bool {
	mut db := init_db(vlambda_db_path) or { return false }
	defer { db.close() }
	db.exec_param_many('INSERT INTO lambdas (slug, name, code, method, owner_id, created_at) VALUES (?, ?, ?, ?, ?, ?);',
		[slug, name, code, method, owner_id.str(), '2026-01-01T00:00:00Z']) or { return false }
	return true
}

@[vc_fn]
pub fn db_update_lambda(slug string, name string, code string, method string, owner_id int) bool {
	mut db := init_db(vlambda_db_path) or { return false }
	defer { db.close() }
	db.exec_param_many('UPDATE lambdas SET name = ?, code = ?, method = ? WHERE slug = ? AND owner_id = ?;', [name, code, method, slug, owner_id.str()]) or { return false }
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
	defer { db.close() }
	rows := db.exec_param('SELECT code, method FROM lambdas WHERE slug = ?;', slug) or { return '' }
	if rows.len == 0 { return '' }
	code := rows[0].vals[0] or { return '' }
	method := rows[0].vals[1] or { return '' }
	result := code + '|' + method
	vlambda_code_cache[slug] = result
	return result
}

@[vc_fn]
pub fn db_get_lambda_name(slug string) string {
	mut db := init_db(vlambda_db_path) or { return '' }
	defer { db.close() }
	rows := db.exec_param('SELECT name FROM lambdas WHERE slug = ?;', slug) or { return '' }
	if rows.len == 0 { return '' }
	return rows[0].vals[0] or { return '' }
}

@[vc_fn]
pub fn db_delete_lambda(slug string, owner_id int) bool {
	mut db := init_db(vlambda_db_path) or { return false }
	defer { db.close() }
	db.exec_param_many('DELETE FROM lambdas WHERE slug = ? AND owner_id = ?;', [slug, owner_id.str()]) or { return false }
	vlambda_code_cache.delete(slug)
	return true
}

@[vc_fn]
pub fn db_list_lambdas(owner_id int) string {
	mut db := init_db(vlambda_db_path) or { return '' }
	defer { db.close() }
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
	defer { db.close() }
	rows := db.exec_param('SELECT COUNT(*) FROM lambdas WHERE owner_id = ?;', owner_id.str()) or { return 0 }
	if rows.len == 0 { return 0 }
	n := rows[0].vals[0] or { return 0 }
	return n.int()
}

@[vc_fn]
pub fn db_list_lambdas_page(owner_id int, limit int, offset int) string {
	mut db := init_db(vlambda_db_path) or { return '' }
	defer { db.close() }
	mut lim := limit
	mut off := offset
	if lim <= 0 { lim = 50 }
	if off < 0 { off = 0 }
	query := 'SELECT slug, name, method FROM lambdas WHERE owner_id = ? LIMIT ${lim} OFFSET ${off};'

	rows := db.exec_param(query, owner_id.str()) or { return '' }
	mut out := ''
	for row in rows {
		slug := row.vals[0] or { continue }
		name := row.vals[1] or { continue }
		method := row.vals[2] or { continue }
		out += slug + '|' + name + '|' + method + '\\n'
	}
	return out
}

// run_lambda calls Python to exe@[vc_fn]
pub fn run_lambda(code string, method string, path string, query string, body string) string {
	vlambda_result_str = '{"error": "not started"}'
	vcraft.with_gil(fn [code, method, path, query, body] () {
		// Use arena for request allocations
		mut arena := vcraft.arena_new()
		defer { arena.free() }
		mut req_items := map[string]vcraft.PyObj{}
		req_items['method'] = vcraft.to_py_string(method)
		req_items['path'] = vcraft.to_py_string(path)
		req_items['query_string'] = vcraft.to_py_string(query)
		req_items['body'] = vcraft.to_py_bytes(body)
		req_obj := vcraft.to_py_obj_dict(req_items)

		if vlambda_executor == unsafe { nil } {
			req_obj.decref()
			vlambda_result_str = '{"error": "no executor registered"}'
			return
		}
		args := [vcraft.to_py_string(code).ptr, req_obj.ptr]
		result := vcraft.call(vlambda_executor, args)
		req_obj.decref()
		vcraft.borrow(args[0]).decref()
		if result.is_null() {
			vcraft.clear_error()
			vlambda_result_str = '{"error": "execution failed"}'
			return
		}
		if result.type_name() == 'str' {
			ss := vcraft.from_py_string(result, 'result') or { '{"error": "bad result"}' }
			result.decref()
			vlambda_result_str = ss
			return
		}
		if result.type_name() == 'bytes' {
			ss := vcraft.from_py_bytes(result, 'result') or { '{"error": "bad result"}' }
			result.decref()
			vlambda_result_str = ss
			return
		}
		ss := result.str()
		result.decref()
		vlambda_result_str = ss
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
pub fn configure(max_body_size int, rate_limit int, invoke_timeout int, max_memory_mb int, cors_origin string, cert_path string, key_path string) {
	if max_body_size > 0 { vlambda_max_body_size = max_body_size }
	if rate_limit > 0 { vlambda_rate_limit = rate_limit }
	if invoke_timeout > 0 { vlambda_invoke_timeout = invoke_timeout }
	vlambda_max_memory_mb = max_memory_mb
	if cors_origin != '' { vlambda_cors_origin = cors_origin }
	vlambda_cert_path = cert_path
	vlambda_key_path = key_path
}

@[vc_fn]
pub fn get_favicon() string {
	data := os.read_file('/home/sheik/Documentos/vlambda/vlambda/python/vlambda/favicon.ico') or { return '' }
	return data
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
pub fn register_ui_html(cb voidptr) {
	vlambda_ui_html = cb
}

@[vc_fn]
pub fn create_task_async(code string, method string, path string, query string, body string) string {
	if vlambda_create_task == unsafe { nil } { return '{"error": "not configured"}' }
	mut cell := ''
	ptr := &cell
	vcraft.with_gil(fn [code, method, path, query, body, ptr] () {
		args := [vcraft.to_py_string(code).ptr, vcraft.to_py_string(method).ptr, vcraft.to_py_string(path).ptr, vcraft.to_py_string(query).ptr, vcraft.to_py_bytes(body).ptr]
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
	db.close()
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

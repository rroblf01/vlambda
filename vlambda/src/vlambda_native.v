module vlambda_native

import vcraft
import db.sqlite
import net.http
import os
import strings

// ---- Global state ----

__global (
	vlambda_db_path = './vlambda.db'
	vlambda_executor = voidptr(0)
	vlambda_result_str = '{"error": "not started"}'
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
		code TEXT NOT NULL,
		method TEXT NOT NULL DEFAULT "GET",
		owner_id INTEGER NOT NULL DEFAULT 0,
		created_at TEXT NOT NULL
	);')!
	return db
}

// ---- Slug generation ----

fn make_slug(code string) string {
	// Simple slug: first 8 chars of a hash-like string
	mut h := u64(5381)
	for c in code {
		h = ((h << 5) + h) + u64(c)
	}
	return 'l${h:08x}'
}

// ---- HTTP Handler ----

struct VlambdaHandler {
mut:
	db_path string
}

pub fn (mut h VlambdaHandler) handle(req http.Request) http.Response {
	// Route: /
	if req.url == '/' || req.url == '' {
		return http.Response{
			status_code: 200
			body: 'vlambda server running'
			header: http.new_header(http.HeaderConfig{ key: .content_type, value: 'text/plain' })
		}
	}

	// Route: /vlambda (POST = create, GET = list)
	if req.url == '/vlambda' {
		if req.method == .post {
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
			slug := make_slug(code)
			method := 'GET'
			ok := db_create_lambda(slug, code, method, 0)
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
			lambdas := db_list_lambdas(0)
			return http.Response{
				status_code: 200
				body: '{"lambdas": "${lambdas}"}'
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
		if req.method == .delete {
			ok := db_delete_lambda(slug, 0)
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
pub fn db_create_lambda(slug string, code string, method string, owner_id int) bool {
	mut db := init_db(vlambda_db_path) or { return false }
	defer { db.close() }
	db.exec_param_many('INSERT INTO lambdas (slug, code, method, owner_id, created_at) VALUES (?, ?, ?, ?, ?);',
		[slug, code, method, owner_id.str(), '2026-01-01T00:00:00Z']) or { return false }
	return true
}

@[vc_fn]
pub fn db_get_lambda_code(slug string) string {
	mut db := init_db(vlambda_db_path) or { return '' }
	defer { db.close() }
	rows := db.exec_param('SELECT code, method FROM lambdas WHERE slug = ?;', slug) or { return '' }
	if rows.len == 0 { return '' }
	code := rows[0].vals[0] or { return '' }
	method := rows[0].vals[1] or { return '' }
	return code + '|' + method
}

@[vc_fn]
pub fn db_delete_lambda(slug string, owner_id int) bool {
	mut db := init_db(vlambda_db_path) or { return false }
	defer { db.close() }
	db.exec_param_many('DELETE FROM lambdas WHERE slug = ? AND owner_id = ?;', [slug, owner_id.str()]) or { return false }
	return true
}

@[vc_fn]
pub fn db_list_lambdas(owner_id int) string {
	mut db := init_db(vlambda_db_path) or { return '' }
	defer { db.close() }
	rows := db.exec_param('SELECT slug, method FROM lambdas WHERE owner_id = ?;', owner_id.str()) or { return '' }
	mut out := ''
	for row in rows {
		slug := row.vals[0] or { continue }
		method := row.vals[1] or { continue }
		out += slug + ':' + method + '\\n'
	}
	return out
}

// run_lambda calls Python to exe@[vc_fn]
pub fn run_lambda(code string, method string, path string, query string, body string) string {
	vlambda_result_str = '{"error": "not started"}'
	vcraft.with_gil(fn [code, method, path, query, body] () {
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
	}
	server.listen_and_serve()

	vcraft.end_allow_threads(state)
	return 0
}

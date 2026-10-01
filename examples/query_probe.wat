;; ABI probe for the sandbox_queries host calls. Exits 0 when all cases
;; produce the expected distinct status codes; exits 60+case on mismatch.
;; Illegal guest pointers must trap nothing in the host: they map to
;; READ_FAULT/BAD_ARG results and never reach the network.
(module
  (import "wasi_snapshot_preview1" "proc_exit" (func $proc_exit (param i32)))
  (import "sandbox_queries" "query_fetch"
    (func $query_fetch (param i32 i32 i32 i32 i32 i32) (result i64)))
  (import "sandbox_queries" "query_read"
    (func $query_read (param i32 i32 i32 i32) (result i64)))
  (memory (export "memory") 2)
  (data (i32.const 16) "device_specs")  ;; 12 bytes
  (data (i32.const 40) "alpha-1")       ;; 7 bytes
  (data (i32.const 90) "other")        ;; 5 bytes

  (func $check (param $res i64) (param $want i32) (param $code i32)
    local.get $res
    i64.const 32 i64.shr_u i32.wrap_i64
    local.get $want i32.ne
    if local.get $code call $proc_exit end)

  (func (export "_start")
    (local $res i64) (local $token i32)

    ;; Case 1: source pointer far past memory -> READ_FAULT(6).
    i32.const 0x70000000 i32.const 4
    i32.const 40 i32.const 7
    i32.const 128 i32.const 8
    call $query_fetch
    i32.const 6 i32.const 60 call $check

    ;; Case 2: invalid key UTF-8 (0xFF at 64) -> BAD_ARG(1).
    i32.const 64 i32.const 255 i32.store8
    i32.const 16 i32.const 12
    i32.const 64 i32.const 1
    i32.const 128 i32.const 8
    call $query_fetch
    i32.const 1 i32.const 61 call $check

    ;; Case 3: source id "other" not authorized -> SOURCE_DENIED(2).
    i32.const 90 i32.const 5
    i32.const 40 i32.const 7
    i32.const 128 i32.const 8
    call $query_fetch
    i32.const 2 i32.const 62 call $check

    ;; Case 4: valid args but output pointer out of range. The range check
    ;; precedes any outbound request -> READ_FAULT(6).
    i32.const 16 i32.const 12
    i32.const 40 i32.const 7
    i32.const 0x70000000 i32.const 8
    call $query_fetch
    i32.const 6 i32.const 63 call $check

    ;; Case 5: real fetch into a 1-byte buffer -> BUFFER_TOO_SMALL(3).
    i32.const 16 i32.const 12
    i32.const 40 i32.const 7
    i32.const 128 i32.const 1
    call $query_fetch local.set $res
    local.get $res i32.const 3 i32.const 64 call $check
    local.get $res i32.wrap_i64 local.set $token

    ;; Case 6: bogus token to query_read -> BAD_ARG(1).
    i32.const 9999 i32.const 0 i32.const 200 i32.const 8
    call $query_read
    i32.const 1 i32.const 65 call $check

    ;; Case 7: valid token, invalid output pointer -> READ_FAULT(6).
    local.get $token i32.const 0 i32.const 0x70000000 i32.const 8
    call $query_read
    i32.const 6 i32.const 66 call $check

    ;; Case 8: read the cached body (token, offset 0, valid 32-byte buffer).
    local.get $token i32.const 0 i32.const 200 i32.const 32
    call $query_read local.set $res
    local.get $res i32.const 0 i32.const 67 call $check
    local.get $res i32.wrap_i64 i32.eqz
    if i32.const 68 call $proc_exit end

    i32.const 0 call $proc_exit)
)
